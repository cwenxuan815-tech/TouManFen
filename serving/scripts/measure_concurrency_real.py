"""B12 真实模型并发 1/3/5 隔离测量（本地模型文件，不启动/连接 worker 或 LLM）。

只做验证，不改 B12 业务代码：
- 每个 (model, concurrency) 组使用独立子进程加载真实模型；
- 子进程内用 B12 的 _INFER_LOCK 串行推理，测量排队延迟、RSS/CPU、最大活跃推理数；
- 同一固定标题、同一 TMF_TORCH_THREADS（默认 4）；
- 结果写 artifacts/b12/real_concurrency_<date>.json。

用法：
  D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/measure_concurrency_real.py
  ... --models bert,distill --concurrency 1,3,5 --rounds 5
"""

from __future__ import annotations

import argparse
import ctypes
import json
import math
import os
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARTIFACT_DIR = PROJECT_ROOT / "artifacts" / "b12"
TITLE = "北京二手房成交量创年内新高"
MARKER = "B12_MEASURE_RESULT "


# ---------------------------------------------------------------------------
# Windows 资源读取（stdlib ctypes，无 psutil 依赖）
# ---------------------------------------------------------------------------
class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.c_ulong),
        ("PageFaultCount", ctypes.c_ulong),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _free_phys_mb() -> float | None:
    if sys.platform != "win32":
        return None
    status = _MEMORYSTATUSEX()
    status.dwLength = ctypes.sizeof(status)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return round(status.ullAvailPhys / (1024 * 1024), 1)


def _rss_mb() -> tuple[float, float] | None:
    if sys.platform != "win32":
        return None
    counters = _PROCESS_MEMORY_COUNTERS()
    counters.cb = ctypes.sizeof(counters)
    try:
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_PROCESS_MEMORY_COUNTERS),
            ctypes.c_ulong,
        ]
        psapi.GetProcessMemoryInfo.restype = ctypes.c_int
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = ctypes.c_void_p
        handle = kernel32.GetCurrentProcess()
        ok = psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb)
    except Exception:
        return None
    if not ok:
        return None
    return (
        round(counters.WorkingSetSize / (1024 * 1024), 1),
        round(counters.PeakWorkingSetSize / (1024 * 1024), 1),
    )


# ---------------------------------------------------------------------------
# 子进程：加载真实模型，执行指定并发组
# ---------------------------------------------------------------------------
def run_worker(model: str, concurrency: int, rounds: int) -> dict:
    module_dir = PROJECT_ROOT / ("03-bert" if model == "bert" else "05-distill")
    module_name = "bert_predict_service" if model == "bert" else "student_predict_service"
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    if str(module_dir) not in sys.path:
        sys.path.insert(0, str(module_dir))

    free_before = _free_phys_mb()
    rss_samples: list[float] = []
    stop_sampling = threading.Event()

    def sample() -> None:
        while not stop_sampling.is_set():
            current = _rss_mb()
            if current:
                rss_samples.append(current[0])
            stop_sampling.wait(0.05)

    sampler = threading.Thread(target=sample, daemon=True)

    load_started = time.perf_counter()
    imported = __import__(module_name)
    load_seconds = time.perf_counter() - load_started
    module = sys.modules[module_name]
    service = getattr(module, "SERVICE")
    config = getattr(module, "config")
    torch = getattr(module, "torch", None)
    torch_threads_actual = None
    torch_interop_threads_actual = None
    if torch is not None:
        try:
            torch_threads_actual = int(torch.get_num_threads())
            torch_interop_threads_actual = int(torch.get_num_interop_threads())
        except Exception:
            pass
    model_path = str(
        getattr(config, "save_model", None)
        or getattr(config, "student_save_model", None)
        or ""
    )
    rss_after_load = _rss_mb()

    # 用 tracking lock 替换真实 _INFER_LOCK：观测进入临界区的最大活跃数。
    original_lock = getattr(module, "_INFER_LOCK")

    class TrackingLock:
        def __init__(self, inner):
            self._inner = inner
            self._guard = threading.Lock()
            self.active = 0
            self.max_active = 0

        def __enter__(self):
            self._inner.acquire()
            with self._guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            return self

        def __exit__(self, exc_type, exc, tb):
            with self._guard:
                self.active -= 1
            self._inner.release()
            return False

        def locked(self):
            return self._inner.locked()

    tracking = TrackingLock(original_lock)
    setattr(module, "_INFER_LOCK", tracking)

    # 预热：不纳入测量，但记录耗时。
    warmup = []
    for _ in range(2):
        start = time.perf_counter()
        result = service.predict(TITLE)
        warmup.append(round((time.perf_counter() - start) * 1000, 2))
        if not getattr(result, "pred_class", None):
            raise RuntimeError("warmup returned no pred_class")

    sampler.start()
    cpu_start = time.process_time()
    wall_start = time.perf_counter()
    latencies: list[float] = []
    errors: list[str] = []
    timeouts = 0

    def one_call() -> tuple[float | None, str | None]:
        start = time.perf_counter()
        try:
            result = service.predict(TITLE)
            pred = getattr(result, "pred_class", None)
            if not pred:
                return None, "empty_pred_class"
            return (time.perf_counter() - start) * 1000.0, None
        except Exception as exc:  # noqa: BLE001 - 记录失败，不掩盖
            return None, f"{type(exc).__name__}: {exc}"

    for _round in range(rounds):
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(one_call) for _ in range(concurrency)]
            for future in as_completed(futures, timeout=120):
                try:
                    latency, error = future.result(timeout=1)
                except Exception:
                    timeouts += 1
                    continue
                if error is not None:
                    errors.append(error)
                elif latency is not None:
                    latencies.append(latency)

    wall_seconds = time.perf_counter() - wall_start
    cpu_seconds = time.process_time() - cpu_start
    stop_sampling.set()
    sampler.join(timeout=1)
    rss_after = _rss_mb()
    free_after = _free_phys_mb()

    def percentile(values: list[float], p: float) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        # nearest-rank: ceil(p/100 * n)-1；样本少时在结果中报告 samples。
        index = max(0, min(len(ordered) - 1, math.ceil((p / 100) * len(ordered)) - 1))
        return round(ordered[index], 2)

    return {
        "model": model,
        "concurrency": concurrency,
        "rounds": rounds,
        "samples": len(latencies),
        "errors": errors,
        "timeouts": timeouts,
        "oom": False,
        "latency_ms": {
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "min": round(min(latencies), 2) if latencies else None,
            "max": round(max(latencies), 2) if latencies else None,
            "mean": round(statistics.fmean(latencies), 2) if latencies else None,
            "all": [round(value, 2) for value in latencies],
            "percentile_method": "nearest-rank; small sample, sample count reported",
        },
        "cpu": {
            "process_seconds": round(cpu_seconds, 3),
            "wall_seconds": round(wall_seconds, 3),
            "avg_process_percent": round((cpu_seconds / wall_seconds) * 100, 1) if wall_seconds else None,
        },
        "rss_mb": {
            "after_load": round(rss_after_load[0], 1) if rss_after_load else None,
            "peak_sampled": round(max(rss_samples), 1) if rss_samples else None,
            "after": rss_after[0] if rss_after else None,
        },
        "load_seconds": round(load_seconds, 2),
        "warmup_ms": warmup,
        "max_active_inference": tracking.max_active,
        "model_path": model_path,
        "torch_threads": getattr(config, "torch_threads", None),
        "torch_threads_actual": torch_threads_actual,
        "torch_interop_threads_actual": torch_interop_threads_actual,
        "system_free_mb_before": free_before,
        "system_free_mb_after": free_after,
        "title": TITLE,
    }


# ---------------------------------------------------------------------------
# 父进程：资源检查、逐组隔离子进程、汇总 JSON
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models", default="bert,distill")
    parser.add_argument("--concurrency", default="1,3,5")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--min-free-mb", type=float, default=3000)
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    if args.rounds < 1:
        raise SystemExit("--rounds 必须 >=1")
    models = [item.strip() for item in args.models.split(",") if item.strip()]
    concurrency_levels = [int(item) for item in args.concurrency.split(",") if item.strip()]
    if any(level < 1 or level > 10 for level in concurrency_levels):
        raise SystemExit("并发档必须为 1~10")

    free_mb = _free_phys_mb()
    if free_mb is not None and free_mb < args.min_free_mb:
        print(f"ABORT: system free memory {free_mb} MB < {args.min_free_mb} MB", flush=True)
        return 3

    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    date = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    output = Path(args.out) if args.out else ARTIFACT_DIR / f"real_concurrency_{date}.json"

    groups: list[dict] = []
    aborted: list[dict] = []
    for model in models:
        for concurrency in concurrency_levels:
            current_free = _free_phys_mb()
            if current_free is not None and current_free < args.min_free_mb:
                aborted.append(
                    {"model": model, "concurrency": concurrency, "reason": f"free_mb {current_free} < {args.min_free_mb}"}
                )
                continue
            print(f"RUN model={model} concurrency={concurrency} rounds={args.rounds} free_mb={current_free}", flush=True)
            env = os.environ.copy()
            env.setdefault("TMF_TORCH_THREADS", "4")
            env["PYTHONIOENCODING"] = "utf-8"
            started = time.perf_counter()
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                model,
                "--concurrency",
                str(concurrency),
                "--rounds",
                str(args.rounds),
            ]
            proc = subprocess.run(
                command,
                cwd=str(PROJECT_ROOT),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=900,
            )
            elapsed = round(time.perf_counter() - started, 2)
            result = None
            for line in reversed((proc.stdout or "").splitlines()):
                if line.startswith(MARKER):
                    try:
                        result = json.loads(line[len(MARKER) :])
                    except json.JSONDecodeError:
                        result = None
                    break
            if result is None:
                result = {
                    "model": model,
                    "concurrency": concurrency,
                    "error": "worker did not return result",
                    "returncode": proc.returncode,
                    "stdout_tail": (proc.stdout or "")[-1500:],
                    "stderr_tail": (proc.stderr or "")[-1500:],
                }
            result["group_wall_seconds"] = elapsed
            result["returncode"] = proc.returncode
            groups.append(result)
            print(
                f"DONE model={model} c={concurrency} rc={proc.returncode} "
                f"samples={result.get('samples')} p50={result.get('latency_ms', {}).get('p50')} "
                f"p95={result.get('latency_ms', {}).get('p95')} max_active={result.get('max_active_inference')}",
                flush=True,
            )

    document = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "scope": "B12 real local model concurrency measurement; no workers/LLM/ports",
        "title": TITLE,
        "models": models,
        "concurrency_levels": concurrency_levels,
        "rounds": args.rounds,
        "min_free_mb": args.min_free_mb,
        "system_free_mb_at_start": free_mb,
        "system_free_mb_at_end": _free_phys_mb(),
        "environment": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "TMF_TORCH_THREADS": os.environ.get("TMF_TORCH_THREADS", "4 (default)"),
            "compared_with": "B12 config default TMF_TORCH_THREADS=4",
        },
        "groups": groups,
        "aborted": aborted,
        "notes": [
            "每个 (model, concurrency) 组独立子进程加载真实模型；load_seconds 与推理分开。",
            "latencies 为 queue delay + infer（从提交到返回），并发组内锁串行导致排队延迟。",
            "max_active_inference 通过替换真实 _INFER_LOCK 为 tracking lock 观测，预期=1。",
            "CPU 为子进程 process CPU 秒 / wall；RSS 由 50ms 轮询采样，峰值可能略低估。",
            "未调用 B7/B8、未启动用户 worker、未监听端口、未调用 LLM。",
        ],
    }
    output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"WROTE {output}", flush=True)
    failures = [g for g in groups if g.get("returncode") != 0 or g.get("error") or g.get("samples") == 0]
    print(f"RESULT {'PASS' if not failures else 'FAIL'} groups={len(groups)} failures={len(failures)}", flush=True)
    return 0 if not failures else 1


if __name__ == "__main__":
    if "--worker" in sys.argv:
        worker_parser = argparse.ArgumentParser()
        worker_parser.add_argument("--worker", required=True)
        worker_parser.add_argument("--concurrency", type=int, required=True)
        worker_parser.add_argument("--rounds", type=int, required=True)
        worker_args = worker_parser.parse_args()
        result = run_worker(worker_args.worker, worker_args.concurrency, worker_args.rounds)
        print(MARKER + json.dumps(result, ensure_ascii=False), flush=True)
    else:
        raise SystemExit(main())
