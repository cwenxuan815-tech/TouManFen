#!/usr/bin/env python
"""阶段 3 轻量验收脚本（不依赖 pytest）。

前提：
- 网关已启动（默认 http://127.0.0.1:8000，取自 serving/config/processes.yaml，
  可用 TMF_GATEWAY_HOST / --base-url 覆盖）；
- 4 个本地 worker（rf/fasttext/bert/distill）已启动；
- LLM 模式：
  - `--llm-mode skip`：不测试 LLM，默认 compare 不执行；
  - `--llm-mode mock`：8105 必须是 mock_worker.py（display_name=mock-llm），
    不访问真实 LLM provider，不请求真实 LLM worker 的 /predict_api。

覆盖：
- /classify 成功、默认/显式模型、标题边界、未知模型、request ID、分数/拒识字段；
- /compare 显式顺序、部分失败、全部失败、超时、非法列表；默认 compare 集合
  **不含 LLM**（B9 opt-in），显式传 models 仍可请求 LLM；
- LLM mock 的成功/错误/非法输出/超时映射；
- 类别来自 class.txt；禁用模型（离线单元检查）；
- 日志/统计之外的阶段边界（无 confidence、批量端点预检、mock 统计排除）。

B10：新增 ``--isolate-mock``（必须配合 ``--llm-mode mock``）：本轮自行启动
**临时端口**的 5 个 ``mock_worker.py`` + 临时端口网关，然后以子进程方式运行本
脚本的 HTTP 断言；结束后停止全部进程、断言端口释放与临时 DB 删除。不启动/
不占用用户 8000/810x，不访问真实 LLM Provider。

    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/verify_stage3.py --llm-mode mock --isolate-mock
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tmf_common.config import probe_host, resolve_worker_url  # noqa: E402

from serving.gateway.errors import ApiError  # noqa: E402
from serving.gateway.settings import load_settings  # noqa: E402
from serving.gateway.stats import StatsCollector  # noqa: E402
from serving.models.registry import ModelRegistry  # noqa: E402
from serving.services.classification import ClassificationService  # noqa: E402

PROCESSES_FILE = PROJECT_ROOT / "serving" / "config" / "processes.yaml"
MODELS_FILE = PROJECT_ROOT / "serving" / "config" / "models.yaml"
CLASSES_FILE = PROJECT_ROOT / "00-data" / "data" / "class.txt"

FAILURES: list[str] = []
CLIENT: httpx.Client | None = None


def load_classes() -> list[str]:
    return [
        line.strip()
        for line in CLASSES_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


def request(method: str, url: str, **kwargs):
    assert CLIENT is not None
    try:
        return CLIENT.request(method, url, **kwargs)
    except Exception as exc:
        print(f"FAIL request_error {method} {url} {type(exc).__name__}")
        return None


def error_code(response) -> str | None:
    if response is None:
        return None
    try:
        return response.json().get("error", {}).get("code")
    except Exception:
        return None


def expect_error(name: str, response, status: int, code: str) -> None:
    check(
        name,
        response is not None
        and response.status_code == status
        and error_code(response) == code,
        f"status={getattr(response, 'status_code', 'none')} code={error_code(response)}",
    )


def unit_checks() -> None:
    settings = load_settings()
    classes = load_classes()
    check("class source is class.txt", list(settings.classes) == classes)
    check("class whitelist 10 unique", len(classes) == 10 and len(set(classes)) == 10)

    with tempfile.TemporaryDirectory() as temp_dir:
        bad_class = Path(temp_dir) / "class9.txt"
        bad_class.write_text("\n".join(classes[:9]) + "\n", encoding="utf-8")
        try:
            load_settings(class_file=bad_class)
            check("invalid class file rejected", False)
        except Exception as exc:
            check("invalid class file rejected", "类别数量错误" in str(exc))

    with tempfile.TemporaryDirectory() as temp_dir:
        raw = yaml.safe_load(MODELS_FILE.read_text(encoding="utf-8"))
        raw = copy.deepcopy(raw)
        raw["models"]["rf"]["enabled"] = False
        disabled_file = Path(temp_dir) / "models_disabled.yaml"
        disabled_file.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        disabled_settings = load_settings(models_file=disabled_file)
        registry = ModelRegistry(disabled_settings)
        service = ClassificationService(
            registry=registry,
            adapters={},  # 禁用模型在调用适配器之前就会拒绝
            classes=disabled_settings.classes,
            max_title_chars=disabled_settings.max_title_chars,
            stats=StatsCollector(),
            logger=logging.getLogger("stage3-unit"),
        )
        try:
            asyncio.run(service.classify("x", "rf", "unit"))
            check("disabled model rejected", False)
        except ApiError as exc:
            check(
                "disabled model rejected",
                exc.code == "MODEL_DISABLED" and exc.status_code == 400,
            )
        except Exception as exc:
            check("disabled model rejected", False, type(exc).__name__)


# ---------------------------------------------------------------------------
# B10：隔离 mock 全链路（本轮自启临时端口 mock_worker + 临时端口网关）
# ---------------------------------------------------------------------------
MOCK_WORKER = PROJECT_ROOT / "serving" / "scripts" / "mock_worker.py"
MOCK_MODEL_IDS = ("rf", "fasttext", "bert", "distill", "llm")
#: 用户 stage2 服务端口：本轮不得启动/占用。
USER_RESERVED_PORTS = {8000, 8101, 8102, 8103, 8104, 8105, 8106}


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _port_closed(port: int, timeout: float = 0.5) -> bool:
    try:
        connection = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError:
        return True
    connection.close()
    return False


class IsolatedMockStack:
    """5 个 mock worker + 网关（全部临时端口），供 verify_stage3 子进程使用。"""

    def __init__(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="tmf_b10_stage3_")
        self.root = Path(self.temp_dir.name)
        self.db_path = self.root / "stage3_mock.db"
        self.worker_ports: dict[str, int] = {}
        self.workers: dict[str, subprocess.Popen] = {}
        self.gateway_port = 0
        self.gateway: subprocess.Popen | None = None
        self._gateway_log_handle = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.gateway_port}"

    def ports(self) -> list[int]:
        return [*self.worker_ports.values(), self.gateway_port]

    def child_env(self) -> dict[str, str]:
        env = os.environ.copy()
        for model_id, port in self.worker_ports.items():
            env[f"TMF_WORKER_URL_{model_id.upper()}"] = f"http://127.0.0.1:{port}"
        env["TMF_DB_PATH"] = str(self.db_path)
        env["TMF_GATEWAY_HOST"] = "127.0.0.1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _next_port(self) -> int:
        while True:
            port = _free_port()
            if port in USER_RESERVED_PORTS or port in self.ports():
                continue
            return port

    def _wait_http(
        self,
        url: str,
        proc: subprocess.Popen,
        label: str,
        timeout: float = 90.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(f"{label} 提前退出（rc={proc.returncode}）")
                try:
                    if client.get(url).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
        raise RuntimeError(f"{label} 启动超时（{timeout}s）")

    def start(self) -> "IsolatedMockStack":
        print("B10 隔离 mock 全链路：本轮自启 5 个 mock worker + 临时端口网关")
        for model_id in MOCK_MODEL_IDS:
            port = self._next_port()
            command = [
                sys.executable,
                str(MOCK_WORKER),
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--model-id",
                model_id,
                "--display-name",
                f"mock-{model_id}",
            ]
            if model_id == "llm":
                command.append("--special-titles")
            proc = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self.worker_ports[model_id] = port
            self.workers[model_id] = proc
            self._wait_http(
                f"http://127.0.0.1:{port}/metadata", proc, f"mock worker {model_id}"
            )
        self.gateway_port = self._next_port()
        self._gateway_log_handle = (self.root / "gateway.log").open(
            "w", encoding="utf-8"
        )
        self.gateway = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "serving.gateway.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.gateway_port),
                "--log-level",
                "warning",
            ],
            cwd=str(PROJECT_ROOT),
            env=self.child_env(),
            stdout=self._gateway_log_handle,
            stderr=subprocess.STDOUT,
        )
        try:
            self._wait_http(
                f"{self.base_url}/api/v1/healthz", self.gateway, "临时网关"
            )
        except Exception:
            self._dump_gateway_log()
            raise
        print(
            f"临时网关 {self.base_url}；mock worker 端口 "
            f"{sorted(self.worker_ports.values())}"
        )
        return self

    def _dump_gateway_log(self) -> None:
        try:
            text = (self.root / "gateway.log").read_text(
                encoding="utf-8", errors="replace"
            )
            print("---------- temporary gateway log (tail) ----------")
            print(text[-3000:])
        except OSError:
            pass

    def stop(self) -> None:
        processes = [self.gateway, *self.workers.values()]
        for proc in processes:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
        if self._gateway_log_handle is not None:
            self._gateway_log_handle.close()
            self._gateway_log_handle = None
        try:
            self.temp_dir.cleanup()
        except OSError:
            shutil.rmtree(self.root, ignore_errors=True)


def run_isolated_mock_checks(args) -> int:  # noqa: ANN001
    """B10：自启临时 mock 全链路并运行本脚本的 HTTP 断言子进程。"""
    stack = IsolatedMockStack()
    child_rc = 1
    try:
        stack.start()
        ports = stack.ports()
        check(
            "isolated stack uses temporary ports (not user 8000/810x)",
            len(set(ports)) == len(ports)
            and all(port not in USER_RESERVED_PORTS for port in ports),
            str(ports),
        )
        with httpx.Client(timeout=3.0, trust_env=False) as client:
            for model_id in MOCK_MODEL_IDS:
                metadata = client.get(
                    f"http://127.0.0.1:{stack.worker_ports[model_id]}/metadata"
                ).json()
                check(
                    f"isolated mock {model_id} display_name=mock-{model_id}",
                    metadata.get("display_name") == f"mock-{model_id}",
                    str(metadata),
                )
            health = client.get(f"{stack.base_url}/api/v1/healthz")
            check(
                "isolated gateway healthz ok",
                health.status_code == 200
                and health.json().get("status") == "ok",
                f"{health.status_code} {health.text[:200]}",
            )

        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--llm-mode",
            "mock",
            "--base-url",
            stack.base_url,
        ]
        proc = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            env=stack.child_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
        print("---------- verify_stage3 child output ----------")
        print(proc.stdout)
        if proc.returncode != 0:
            print("---------- child stderr ----------")
            print(proc.stderr[-4000:])
        child_pass = sum(
            1 for line in proc.stdout.splitlines() if line.startswith("PASS ")
        )
        child_fail = sum(
            1 for line in proc.stdout.splitlines() if line.startswith("FAIL ")
        )
        check(
            "verify_stage3 --llm-mode mock (isolated child) exit 0",
            proc.returncode == 0,
            f"rc={proc.returncode}",
        )
        check(
            "verify_stage3 child RESULT PASS and zero FAIL",
            "RESULT PASS" in proc.stdout and child_fail == 0,
            f"child PASS={child_pass} FAIL={child_fail}",
        )
        print(f"      child verify_stage3: PASS={child_pass} FAIL={child_fail}")
        child_rc = proc.returncode
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        check(
            "isolated mock full chain completed",
            False,
            f"{type(exc).__name__}: {exc}",
        )
    finally:
        ports = stack.ports()
        stack.stop()
        time.sleep(0.3)
        if ports:
            closed = {port: _port_closed(port) for port in ports}
            check(
                "all isolated ports released after stop (workers + gateway)",
                all(closed.values()),
                str(closed),
            )
        check(
            "isolated temp DB removed after cleanup",
            not stack.db_path.exists(),
            str(stack.db_path),
        )

    if FAILURES or child_rc != 0:
        print(f"RESULT FAIL ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("RESULT PASS")
    return 0


def main() -> int:
    global CLIENT

    parser = argparse.ArgumentParser()
    parser.add_argument("--llm-mode", choices=("skip", "mock"), default="skip")
    parser.add_argument("--base-url", default=None)
    parser.add_argument(
        "--isolate-mock",
        action="store_true",
        help=(
            "B10：本轮自启临时端口 mock worker（5 个）+ 临时端口网关后运行断言；"
            "必须配合 --llm-mode mock，不接触用户 8000/810x"
        ),
    )
    args = parser.parse_args()

    if args.isolate_mock:
        if args.llm_mode != "mock":
            parser.error("--isolate-mock 必须与 --llm-mode mock 一起使用")
        return run_isolated_mock_checks(args)

    processes = yaml.safe_load(PROCESSES_FILE.read_text(encoding="utf-8"))
    models_config = yaml.safe_load(MODELS_FILE.read_text(encoding="utf-8"))
    gateway = processes["gateway"]
    gateway_host = probe_host(os.environ.get("TMF_GATEWAY_HOST") or gateway["host"])
    base_url = args.base_url or f"http://{gateway_host}:{gateway['port']}"
    llm_base_url = resolve_worker_url(
        f"http://{gateway['host']}:{processes['workers']['llm']['port']}", "llm"
    )
    classes = load_classes()
    default_compare_order = [
        model_id
        for model_id, model in models_config["models"].items()
        if model.get("enabled", True) and model.get("compare_by_default", True)
    ]

    unit_checks()
    # B9：LLM 是 opt-in——默认 compare 集合不含 llm（避免默认请求触发外部 Provider）；
    # 位于配置层断言，与 gateway 是否启动无关。
    check(
        "default compare set excludes llm (B9 opt-in)",
        "llm" not in default_compare_order and len(default_compare_order) >= 4,
        str(default_compare_order),
    )

    with httpx.Client(timeout=40.0, trust_env=False) as client:
        CLIENT = client

        response = request("GET", f"{base_url}/api/v1/healthz")
        check(
            "gateway healthz",
            response is not None
            and response.status_code == 200
            and response.json().get("status") == "ok",
        )

        response = request("GET", f"{base_url}/api/v1/models")
        body = response.json() if response is not None and response.status_code == 200 else {}
        check(
            "models registry",
            response is not None
            and response.status_code == 200
            and len(body.get("models", [])) == 5
            and body.get("default_model") == "fasttext"
            and len(body.get("classes", [])) == 10,
        )
        # B9：LLM 仅从默认 compare 移出，仍在 /models 中且 enabled → 可显式选择。
        llm_registered = next(
            (
                model
                for model in body.get("models", [])
                if model.get("model_id") == "llm"
            ),
            {},
        )
        check(
            "models: llm still registered and selectable (enabled)",
            llm_registered.get("enabled") is True,
            str(llm_registered),
        )

        title = "北京二手房成交量创年内新高"
        observed_sources: dict[str, str | None] = {}
        for model_id in ("rf", "fasttext", "bert", "distill"):
            response = request(
                "POST",
                f"{base_url}/api/v1/classify",
                json={"title": title, "model": model_id},
            )
            body = response.json() if response is not None and response.status_code == 200 else {}
            observed_sources[model_id] = body.get("data_source")
            scores = body.get("scores")
            top_k = body.get("top_k")
            threshold_meta = body.get("threshold_meta")
            # B10：替换过期的「无 confidence」断言——校验 B1/B3 新字段契约：
            # 无分数（如 mock worker）时 score/scores_kind/top_k/threshold_meta 均为 null；
            # 有分数时 scores 完整、score_kind 合法、top_k 配对、threshold_meta 非空。
            score_fields_ok = (
                "confidence" not in body
                and {"scores", "score_kind", "top_k"} <= set(body)
                and isinstance(body.get("rejected"), bool)
                and body.get("reject_reason")
                in {None, "low_max_score", "high_entropy"}
                and (
                    (
                        scores is None
                        and body.get("score_kind") is None
                        and top_k is None
                        and threshold_meta is None
                    )
                    or (
                        isinstance(scores, dict)
                        and len(scores) == len(classes)
                        and body.get("score_kind")
                        in {"raw_proba", "raw_fasttext", "raw_softmax"}
                        and isinstance(top_k, list)
                        and 1 <= len(top_k) <= 3
                        and all(
                            set(entry) == {"class_id", "score"}
                            for entry in top_k
                        )
                        and isinstance(threshold_meta, dict)
                        and {
                            "source",
                            "calibrated",
                            "method",
                            "max_score_threshold",
                            "entropy_threshold",
                            "scope_note",
                            "sample_size",
                        }
                        <= set(threshold_meta)
                    )
                )
            )
            check(
                f"classify {model_id}",
                response is not None
                and response.status_code == 200
                and body.get("model_id") == model_id
                and body.get("pred_class") in classes
                and isinstance(body.get("latency_ms"), int)
                # B11：真实/演示来源必须由服务端给出，不得缺失或伪造 live。
                and body.get("data_source") in {"live", "mock", "unknown"}
                and score_fields_ok,
                f"status={getattr(response, 'status_code', 'none')} "
                f"scores={type(scores).__name__} rejected={body.get('rejected')}",
            )

        response = request(
            "POST", f"{base_url}/api/v1/classify", json={"title": title}
        )
        body = response.json() if response is not None and response.status_code == 200 else {}
        check(
            "classify default model",
            response is not None
            and response.status_code == 200
            and body.get("model_id") == "fasttext"
            and body.get("pred_class") in classes,
        )

        response = request(
            "POST",
            f"{base_url}/api/v1/classify",
            json={"title": f"  {title}  "},
        )
        check(
            "title trim accepted",
            response is not None
            and response.status_code == 200
            and response.json().get("pred_class") in classes,
        )

        response = request(
            "POST",
            f"{base_url}/api/v1/classify",
            headers={"X-Request-ID": "stage3-verify-001"},
            json={"title": title, "model": "fasttext"},
        )
        body = response.json() if response is not None and response.status_code == 200 else {}
        check(
            "request id custom echo",
            response is not None
            and response.status_code == 200
            and response.headers.get("X-Request-ID") == "stage3-verify-001"
            and body.get("request_id") == "stage3-verify-001",
        )

        response = request(
            "GET",
            f"{base_url}/api/v1/healthz",
            headers={"X-Request-ID": "bad id!"},
        )
        expect_error("invalid request id rejected", response, 400, "INVALID_REQUEST_ID")

        expect_error(
            "empty title rejected",
            request("POST", f"{base_url}/api/v1/classify", json={"title": "   "}),
            400,
            "INVALID_TITLE",
        )
        expect_error(
            "missing title rejected",
            request("POST", f"{base_url}/api/v1/classify", json={"model": "fasttext"}),
            422,
            "INVALID_REQUEST",
        )
        expect_error(
            "non-string title rejected",
            request("POST", f"{base_url}/api/v1/classify", json={"title": 123}),
            422,
            "INVALID_REQUEST",
        )
        expect_error(
            "overlong title rejected",
            request(
                "POST",
                f"{base_url}/api/v1/classify",
                json={"title": "新" * 201},
            ),
            400,
            "TITLE_TOO_LONG",
        )
        expect_error(
            "unknown model rejected",
            request(
                "POST",
                f"{base_url}/api/v1/classify",
                json={"title": title, "model": "not_a_model"},
            ),
            404,
            "UNKNOWN_MODEL",
        )
        expect_error(
            "empty model rejected",
            request(
                "POST",
                f"{base_url}/api/v1/classify",
                json={"title": title, "model": ""},
            ),
            404,
            "UNKNOWN_MODEL",
        )

        response = request(
            "POST",
            f"{base_url}/api/v1/compare",
            json={"title": title, "models": ["bert", "rf"]},
        )
        body = response.json() if response is not None and response.status_code == 200 else {}
        check(
            "compare explicit order",
            response is not None
            and response.status_code == 200
            and [item.get("model_id") for item in body.get("results", [])]
            == ["bert", "rf"]
            and body.get("summary", {}).get("total") == 2
            and body.get("summary", {}).get("ok") == 2
            and body.get("summary", {}).get("status") == "ok"
            # B10：compare 结果不返回分数/拒识字段（避免跨模型比分数），但必须有来源。
            and all(
                item.get("pred_class") in classes
                and "confidence" not in item
                and "scores" not in item
                and "rejected" not in item
                and item.get("data_source") in {"live", "mock", "unknown"}
                for item in body.get("results", [])
            ),
            str(body.get("results"))[:300],
        )

        expect_error(
            "compare empty models rejected",
            request(
                "POST",
                f"{base_url}/api/v1/compare",
                json={"title": title, "models": []},
            ),
            400,
            "INVALID_MODELS",
        )
        expect_error(
            "compare duplicate models rejected",
            request(
                "POST",
                f"{base_url}/api/v1/compare",
                json={"title": title, "models": ["rf", "rf"]},
            ),
            400,
            "INVALID_MODELS",
        )
        expect_error(
            "compare unknown model rejected",
            request(
                "POST",
                f"{base_url}/api/v1/compare",
                json={"title": title, "models": ["not_a_model"]},
            ),
            404,
            "UNKNOWN_MODEL",
        )

        response = request("GET", f"{base_url}/api/v1/stats")
        body = response.json() if response is not None and response.status_code == 200 else {}
        live_models = {
            model_id
            for model_id, source in observed_sources.items()
            if source == "live"
        }
        # B11：model_calls 只统计 live 调用；mock/unknown 不计入（mock 全链路下应为空）。
        check(
            "stats model_calls follows data_source (B11: only live counted)",
            response is not None
            and response.status_code == 200
            and isinstance(body.get("model_calls"), dict)
            and set(body.get("model_calls", {})) <= live_models
            and (("rf" in body.get("model_calls", {})) == ("rf" in live_models)),
            f"live={sorted(live_models)} model_calls={body.get('model_calls')}",
        )

        # 阶段 2A：批量端点已实现；空 items 必须是请求级 400，且不触发 worker。
        response = request(
            "POST",
            f"{base_url}/api/v1/classify/batch",
            json={"model": "fasttext", "items": []},
        )
        check(
            "batch endpoint rejects empty items (BATCH_INVALID, no worker)",
            response is not None
            and response.status_code == 400
            and response.json().get("error", {}).get("code") == "BATCH_INVALID",
            f"{getattr(response, 'status_code', None)} "
            f"{getattr(response, 'text', '')[:200]}",
        )

        if args.llm_mode == "mock":
            metadata = request("GET", f"{llm_base_url}/metadata")
            mock_ready = (
                metadata is not None
                and metadata.status_code == 200
                and metadata.json().get("display_name") == "mock-llm"
            )
            check(
                "mock llm detected",
                mock_ready,
                "8105 不是 mock worker；请先停止真实 LLM worker 并启动 mock_worker.py",
            )
            if mock_ready:
                llm_cases = [
                    ("MOCK_OK", 200, None),
                    ("MOCK_ERROR", 502, "MODEL_ERROR"),
                    ("MOCK_MISSING_CLASS", 502, "WORKER_BAD_RESPONSE"),
                    ("MOCK_INVALID_CLASS", 502, "INVALID_MODEL_OUTPUT"),
                    ("MOCK_EMPTY_CLASS", 502, "WORKER_BAD_RESPONSE"),
                    ("MOCK_INVALID_JSON", 502, "WORKER_BAD_RESPONSE"),
                    ("MOCK_HTTP_500", 502, "WORKER_HTTP_ERROR"),
                    ("MOCK_SLOW", 504, "WORKER_TIMEOUT"),
                ]
                for mock_title, status, code in llm_cases:
                    response = request(
                        "POST",
                        f"{base_url}/api/v1/classify",
                        json={"title": mock_title, "model": "llm"},
                    )
                    if code is None:
                        check(
                            "llm mock success",
                            response is not None
                            and response.status_code == 200
                            and response.json().get("pred_class") == "sports"
                            # B11：mock worker 的 display_name=mock-llm → data_source=mock。
                            and response.json().get("data_source") == "mock",
                            f"{getattr(response, 'status_code', 'none')} "
                            f"{response.text[:200] if response is not None else ''}",
                        )
                        continue
                    expect_error(f"llm {mock_title}", response, status, code)
                    if response is not None:
                        check(
                            f"llm {mock_title} no raw error",
                            "mock error" not in response.text
                            and "not-json" not in response.text
                            and "Traceback" not in response.text,
                        )

                response = request(
                    "POST",
                    f"{base_url}/api/v1/compare",
                    json={"title": "MOCK_OK"},
                )
                body = response.json() if response is not None and response.status_code == 200 else {}
                results = body.get("results", [])
                check(
                    "compare default excludes llm (B9 opt-in) and 4 local ok",
                    response is not None
                    and response.status_code == 200
                    and [item.get("model_id") for item in results]
                    == default_compare_order
                    and "llm" not in [item.get("model_id") for item in results]
                    and body.get("summary", {}).get("status") == "ok"
                    and body.get("summary", {}).get("ok") == len(default_compare_order)
                    and all(item.get("pred_class") in classes for item in results)
                    # B10：compare 结果不返回分数/拒识字段，但必须带来源。
                    and all(
                        "confidence" not in item
                        and "scores" not in item
                        and item.get("data_source") in {"live", "mock", "unknown"}
                        for item in results
                    ),
                    f"{[item.get('model_id') for item in results]} "
                    f"summary={body.get('summary')}",
                )

                response = request(
                    "POST",
                    f"{base_url}/api/v1/compare",
                    json={
                        "title": "MOCK_ERROR",
                        # B9：LLM 默认缺席，显式列出才能验证 partial 降级。
                        "models": [*default_compare_order, "llm"],
                    },
                )
                body = response.json() if response is not None and response.status_code == 200 else {}
                results = body.get("results", [])
                llm_result = next(
                    (item for item in results if item.get("model_id") == "llm"), {}
                )
                check(
                    "compare partial llm failure",
                    response is not None
                    and response.status_code == 200
                    and body.get("summary", {}).get("status") == "partial"
                    and body.get("summary", {}).get("ok") == 4
                    and body.get("summary", {}).get("failed") == 1
                    and llm_result.get("status") == "error"
                    and llm_result.get("error", {}).get("code") == "MODEL_ERROR"
                    and "pred_class" not in llm_result,
                )

                response = request(
                    "POST",
                    f"{base_url}/api/v1/compare",
                    json={"title": "MOCK_ERROR", "models": ["llm"]},
                )
                body = response.json() if response is not None and response.status_code == 200 else {}
                results = body.get("results", [])
                check(
                    "compare all_failed",
                    response is not None
                    and response.status_code == 200
                    and body.get("summary", {}).get("status") == "all_failed"
                    and body.get("summary", {}).get("ok") == 0
                    and len(results) == 1
                    and results[0].get("status") == "error"
                    and "pred_class" not in results[0],
                )

                response = request(
                    "POST",
                    f"{base_url}/api/v1/compare",
                    json={"title": "MOCK_SLOW", "models": ["llm"]},
                )
                body = response.json() if response is not None and response.status_code == 200 else {}
                results = body.get("results", [])
                llm_result = results[0] if results else {}
                check(
                    "compare llm timeout",
                    response is not None
                    and response.status_code == 200
                    and body.get("summary", {}).get("status") == "all_failed"
                    and llm_result.get("error", {}).get("code") == "WORKER_TIMEOUT"
                    and "pred_class" not in llm_result
                    and llm_result.get("latency_ms", 0) >= 14000,
                )
        else:
            print("SKIP LLM tests (--llm-mode skip)")

    CLIENT = None
    if FAILURES:
        print(f"RESULT FAIL ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
