#!/usr/bin/env python
"""B8：离线基准产物（PK 榜）生成脚本。

一次运行产出 ``artifacts/bench/pk_<date>.json``，内容：

1. 4 个**可用本地模型**（RF / fastText / BERT / 蒸馏 BiLSTM）在
   ``00-data/data/test.txt``（10,000 条）上的 macro-F1、accuracy、
   10x10 混淆矩阵、平均单条延迟、模型体积；
2. dev 固定种子分层抽样（默认每类 200 条 = 2,000 条）上的 macro-F1 作为
   加权投票的集成权重（**只用 dev，不用 test 调参/调阈值**）；
3. 「加权投票集成」与「oracle 上界」两个**派生指标**（明确标注非独立模型）；
4. LLM：``04-LLM`` 是外部 Provider（OpenAI 兼容），本轮任务禁止调用，
   产物中固定为 ``status=missing``（不 mock、不估算、不填 0 分）；
5. 完整 ``meta``（§6.8 / B13 契约）与复现信息（命令、种子、环境、耗时）。

设计要点（避免一次进程同时加载 RF+BERT 造成内存峰值）：

* 主进程只做编排与指标计算，**每个模型单独子进程**加载/推理；
* 每个模型把逐条预测写入 ``artifacts/bench/_pred_<model>_{test,dev}.jsonl``，
  主进程再合并计算；中间产物同时用于 verify_bench 的确定性复核；
* 全量预测走批量推理（BERT/蒸馏 batch=64、RF batch=256），**指标口径**是
  「逐条预测结果」；延迟口径单独在 test 前 200 条上逐条调用 ``SERVICE.predict``
  测量（含 jieba/tokenizer 预处理，不含 HTTP/网络），并校验批量预测与逐条
  预测在这 200 条上完全一致（不一致直接失败，不发布可疑产物）；
* 不训练、不校准、不读 ``04-LLM/.env``、不启动任何服务。

用法（可重复命令，工作目录=工程根）::

    # 全量生成（只跑一次；本机约数分钟）
    D:\\Miniconda3\\envs\\py3109\\python.exe tools/bench_signature.py

    # 可调参数（默认即交付口径）
    ... tools/bench_signature.py --seed 20261022 --dev-per-class 200 --latency-subset 200

    # 内部 worker（主进程自动调用，勿手工依赖）
    ... tools/bench_signature.py --worker rf --run-dir artifacts/bench \
        --dev-sample-file artifacts/bench/_dev_sample_seed20261022_per_class200.txt

产物自检：写入前调用 ``tools/validate_meta.py`` 的 ``validate_document``；
校验失败则不落盘并以非 0 退出。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import random
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # Windows 控制台/管道统一按 UTF-8 输出中文
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - 非交互环境可能不支持
    pass


# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "00-data" / "data"
CLASS_FILE = DATA_DIR / "class.txt"
TEST_FILE = DATA_DIR / "test.txt"
DEV_FILE = DATA_DIR / "dev.txt"
BENCH_DIR = PROJECT_ROOT / "artifacts" / "bench"

DEFAULT_SEED = 20261022
DEFAULT_DEV_PER_CLASS = 200
DEFAULT_LATENCY_SUBSET = 200
DEFAULT_BATCH_SIZE = 64
RF_BATCH_SIZE = 256

#: 本地模型（顺序即产物 models 中的顺序，与 models.yaml 一致，LLM 单独放在最后）。
MODEL_SPECS: dict[str, dict[str, str]] = {
    "rf": {
        "module_dir": "01-rf",
        "module": "rf_predict_service",
        "display_name": "随机森林",
    },
    "fasttext": {
        "module_dir": "02-fasttext",
        "module": "ft_predict_service",
        "display_name": "fastText",
    },
    "bert": {
        "module_dir": "03-bert",
        "module": "bert_predict_service",
        "display_name": "BERT",
    },
    "distill": {
        "module_dir": "05-distill",
        "module": "student_predict_service",
        "display_name": "蒸馏 BiLSTM",
    },
}
MODEL_ORDER = tuple(MODEL_SPECS)

#: LLM 本轮不调用；产物中必须如实标注缺失原因（不是 0 分、不是 mock）。
LLM_ENTRY: dict[str, Any] = {
    "model_id": "llm",
    "display_name": "大模型",
    "status": "missing",
    "reason": "external_provider_not_authorized",
    "detail": (
        "04-LLM 走外部 Provider（OpenAI 兼容接口），本轮任务禁止调用/计费；"
        "未产生任何预测，也未用 mock 或估算值填充。"
    ),
}


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------
def now_iso() -> str:
    """带显式时区偏移的 ISO 8601 时间（本机时区）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def relpath(path: Path) -> str:
    """工程根相对路径（统一用 /，便于跨平台核对）。"""
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def read_rows(
    path: Path, classes: Sequence[str] | None = None
) -> list[tuple[str, str]]:
    """读取 ``title<TAB>label`` 数据；标题内的 Tab 归入标题，最后一列是标签。

    test/dev.txt 的标签是 class.txt 行序（0..9）的整数索引；传入 ``classes``
    时统一映射为类别名（若原始标签已经是类别名则原样保留）。
    """
    if not path.is_file():
        raise FileNotFoundError(f"数据文件不存在: {path}")
    rows: list[tuple[str, str]] = []
    for line_no, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not line.strip():
            continue
        if "\t" not in line:
            raise ValueError(f"{relpath(path)}:{line_no}: 缺少 TAB 分隔符")
        title, raw_label = line.rsplit("\t", 1)
        label = raw_label.strip()
        if classes is not None:
            if label in classes:
                pass
            elif label.isdigit() and 0 <= int(label) < len(classes):
                label = classes[int(label)]
            else:
                raise ValueError(
                    f"{relpath(path)}:{line_no}: 标签 {label!r} 既不是类别名，"
                    f"也不是 0..{len(classes) - 1} 的 class.txt 索引"
                )
        rows.append((title, label))
    return rows


def load_classes() -> list[str]:
    classes = [line.strip() for line in CLASS_FILE.read_text(encoding="utf-8").splitlines()]
    classes = [item for item in classes if item]
    if not classes:
        raise ValueError(f"类别文件为空: {relpath(CLASS_FILE)}")
    if len(set(classes)) != len(classes):
        raise ValueError(f"类别文件存在重复项: {relpath(CLASS_FILE)}")
    return classes


def percentiles(values: Sequence[float], ps: Iterable[float]) -> dict[str, float | None]:
    if not values:
        return {f"p{int(p)}": None for p in ps}
    ordered = sorted(float(v) for v in values)
    result: dict[str, float | None] = {}
    for p in ps:
        if len(ordered) == 1:
            result[f"p{int(p)}"] = round(ordered[0], 3)
            continue
        k = (len(ordered) - 1) * (p / 100.0)
        lower = int(k)
        upper = min(lower + 1, len(ordered) - 1)
        value = ordered[lower] + (ordered[upper] - ordered[lower]) * (k - lower)
        result[f"p{int(p)}"] = round(value, 3)
    return result


def package_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except Exception:  # pragma: no cover - 仅元数据缺失时
        return "unknown"


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, path)


def write_jsonl_atomic(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.replace(tmp, path)


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"{relpath(path)}:{line_no}: JSONL 解析失败: {exc.msg}"
                ) from exc
    return records


def load_meta_validator() -> Any:
    """按文件路径加载 B13 的 validator，避免 ``tools`` 包名与第三方库冲突。"""
    path = PROJECT_ROOT / "tools" / "validate_meta.py"
    spec = importlib.util.spec_from_file_location("_tmf_validate_meta", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"无法加载 meta 校验器: {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses 等需要模块先注册到 sys.modules 才能解析注解/类型。
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def collect_git_info() -> dict[str, Any]:
    info: dict[str, Any] = {"head_short_sha": None, "dirty": None, "error": None}
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=15,
        )
        if sha.returncode == 0:
            info["head_short_sha"] = sha.stdout.strip() or None
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        if status.returncode == 0:
            info["dirty"] = bool(status.stdout.strip())
    except Exception as exc:  # pragma: no cover - git 不可用时
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info


def total_memory_gb() -> float | None:
    """Windows 物理内存总量（GB，1024^3）；非 Windows 返回 None。"""
    try:  # pragma: no cover - 仅 Windows 有效
        import ctypes

        class MemoryStatusEx(ctypes.Structure):
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

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            return round(status.ullTotalPhys / (1024**3), 2)
    except Exception:
        pass
    return None


def environment_info() -> dict[str, Any]:
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cpu_count": os.cpu_count(),
        "total_memory_gb": total_memory_gb(),
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "packages": {
            "numpy": package_version("numpy"),
            "scikit-learn": package_version("scikit-learn"),
        },
        "os_timezone": datetime.now().astimezone().tzinfo.tzname(None),
    }


# ---------------------------------------------------------------------------
# dev 权重抽样（只用 dev；固定种子；分层每类 N 条）
# ---------------------------------------------------------------------------
def select_dev_sample(
    dev_rows: Sequence[tuple[str, str]],
    classes: Sequence[str],
    seed: int,
    per_class: int,
) -> tuple[list[tuple[int, tuple[str, str]]], dict[str, int]]:
    pools: dict[str, list[int]] = {name: [] for name in classes}
    for index, (_, label) in enumerate(dev_rows):
        if label not in pools:
            raise ValueError(f"dev.txt 第 {index + 1} 行标签不在 class.txt: {label!r}")
        pools[label].append(index)

    rng = random.Random(seed)
    selected: list[int] = []
    actual: dict[str, int] = {}
    for name in classes:
        pool = pools[name]
        take = min(per_class, len(pool))
        actual[name] = take
        if take:
            selected.extend(rng.sample(pool, take))
    selected.sort()

    missing = [name for name in classes if actual[name] == 0]
    if missing:
        raise ValueError(f"dev.txt 缺少类别样本: {missing}")
    return [(index, dev_rows[index]) for index in selected], actual


# ---------------------------------------------------------------------------
# worker：单模型子进程（加载 -> 批量预测 test/dev -> 逐条延迟子集 -> 写中间产物）
# ---------------------------------------------------------------------------
def _model_file_paths(model_id: str, config: Any) -> list[Path]:
    if model_id == "rf":
        return [Path(config.rf_model_path), Path(config.tfidf_model_path)]
    if model_id == "fasttext":
        return [Path(config.model_word_auto_train)]
    if model_id == "bert":
        return [Path(config.save_model)]
    if model_id == "distill":
        return [Path(config.student_save_model)]
    raise ValueError(f"未知模型: {model_id}")


def _describe_model_files(model_id: str, config: Any) -> tuple[str, list[dict[str, Any]], int]:
    files: list[dict[str, Any]] = []
    total = 0
    for path in _model_file_paths(model_id, config):
        if not path.is_file():
            raise FileNotFoundError(f"模型文件不存在: {path}")
        size = path.stat().st_size
        total += size
        files.append({"path": relpath(path), "bytes": int(size)})
    version = "+".join(Path(item["path"]).name for item in files)
    return version, files, total


def _batch_predict(
    model_id: str,
    service: Any,
    classes: Sequence[str],
    titles: Sequence[str],
) -> list[str]:
    """离线批量预测（只用于产出逐条预测结果；延迟另由 SERVICE.predict 测量）。"""
    batch_size = RF_BATCH_SIZE if model_id == "rf" else DEFAULT_BATCH_SIZE

    if model_id == "rf":
        import jieba

        tfidf = service._tfidf
        model = service._model
        predictions: list[str] = []
        for start in range(0, len(titles), batch_size):
            chunk = titles[start : start + batch_size]
            words = [" ".join(jieba.lcut(title)[:30]) for title in chunk]
            labels = model.predict(tfidf.transform(words))
            predictions.extend(classes[int(label)] for label in labels)
        return predictions

    if model_id == "fasttext":
        import jieba

        model = service._model
        predictions = []
        for title in titles:
            words = " ".join(jieba.lcut(title))
            labels, _ = model.predict(words, k=len(classes))
            predictions.append(labels[0].replace("__label__", ""))
        return predictions

    if model_id in {"bert", "distill"}:
        import torch

        tokenizer = service._tokenizer
        model = service._model
        device = service._device
        max_length = service._max_length
        predictions = []
        for start in range(0, len(titles), batch_size):
            chunk = titles[start : start + batch_size]
            if model_id == "bert":
                tokens = tokenizer.batch_encode_plus(
                    chunk,
                    padding="max_length",
                    truncation=True,
                    max_length=max_length,
                )
            else:
                tokens = tokenizer(
                    chunk,
                    padding="max_length",
                    truncation=True,
                    max_length=max_length,
                    return_tensors="pt",
                )
            input_ids = torch.as_tensor(tokens["input_ids"]).to(device)
            attention_mask = torch.as_tensor(tokens["attention_mask"]).to(device)
            with torch.no_grad():
                logits = model(input_ids, attention_mask)
            indices = torch.argmax(logits, dim=-1).tolist()
            predictions.extend(classes[int(index)] for index in indices)
        return predictions

    raise ValueError(f"未知模型: {model_id}")


def _prediction_digest(
    rows: Sequence[tuple[str, str]], predictions: Sequence[str]
) -> str:
    digest = hashlib.sha256()
    for index, ((_, label), prediction) in enumerate(zip(rows, predictions)):
        digest.update(f"{index}\t{label}\t{prediction}\n".encode("utf-8"))
    return digest.hexdigest()


def worker_main(args: argparse.Namespace) -> int:
    model_id = args.worker
    spec = MODEL_SPECS[model_id]
    module_dir = PROJECT_ROOT / spec["module_dir"]
    sys.path.insert(0, str(module_dir))

    print(f"[worker:{model_id}] 加载 {spec['module']} ...", flush=True)
    started = time.perf_counter()
    module = importlib.import_module(spec["module"])
    load_s = time.perf_counter() - started
    service = module.SERVICE
    config = module.config
    classes = list(service.class_list)

    test_rows = read_rows(TEST_FILE, classes)
    dev_rows = read_rows(Path(args.dev_sample_file), classes)
    for split_name, rows in (("test", test_rows), ("dev", dev_rows)):
        bad = sorted({label for _, label in rows} - set(classes))
        if bad:
            raise ValueError(f"{split_name} 存在 class.txt 之外的标签: {bad}")

    print(
        f"[worker:{model_id}] 加载完成 {load_s:.2f}s | test={len(test_rows)} "
        f"dev={len(dev_rows)} | 开始批量预测",
        flush=True,
    )

    start = time.perf_counter()
    test_predictions = _batch_predict(model_id, service, classes, [t for t, _ in test_rows])
    test_predict_s = time.perf_counter() - start
    print(
        f"[worker:{model_id}] test 批量预测 {test_predict_s:.2f}s",
        flush=True,
    )

    start = time.perf_counter()
    dev_predictions = _batch_predict(model_id, service, classes, [t for t, _ in dev_rows])
    dev_predict_s = time.perf_counter() - start
    print(
        f"[worker:{model_id}] dev  批量预测 {dev_predict_s:.2f}s",
        flush=True,
    )

    # 延迟口径：test 前 N 条逐条调用 SERVICE.predict（含预处理，不含 HTTP）。
    latency_count = min(args.latency_subset, len(test_rows))
    latency_values: list[float] = []
    mismatch = 0
    for index in range(latency_count):
        result = service.predict(test_rows[index][0])
        latency_values.append(float(result.latency_ms))
        if result.pred_class != test_predictions[index]:
            mismatch += 1
    if mismatch:
        raise RuntimeError(
            f"批量预测与逐条预测在前 {latency_count} 条存在 {mismatch} 条不一致，"
            "拒绝发布可能错误的产物"
        )

    version, model_files, size_bytes = _describe_model_files(model_id, config)
    device = str(getattr(service, "_device", "cpu"))
    latency_stats = percentiles(latency_values, (50, 95))
    finished_at = now_iso()
    elapsed_s = round(time.perf_counter() - started, 3)

    run_dir = Path(args.run_dir)
    test_jsonl = run_dir / f"_pred_{model_id}_test.jsonl"
    dev_jsonl = run_dir / f"_pred_{model_id}_dev.jsonl"
    meta_json = run_dir / f"_pred_{model_id}_meta.json"

    write_jsonl_atomic(
        test_jsonl,
        (
            {
                "split": "test",
                "index": index,
                "title": title,
                "true_label": label,
                "pred_class": prediction,
                "latency_ms": (
                    round(latency_values[index], 3) if index < latency_count else None
                ),
            }
            for index, ((title, label), prediction) in enumerate(
                zip(test_rows, test_predictions)
            )
        ),
    )
    write_jsonl_atomic(
        dev_jsonl,
        (
            {
                "split": "dev",
                "index": index,
                "title": title,
                "true_label": label,
                "pred_class": prediction,
            }
            for index, ((title, label), prediction) in enumerate(
                zip(dev_rows, dev_predictions)
            )
        ),
    )

    meta_payload = {
        "model_id": model_id,
        "display_name": spec["display_name"],
        "classes": classes,
        "model_version": version,
        "model_files": model_files,
        "size_bytes": size_bytes,
        "load_s": round(load_s, 3),
        "test_batch_predict_s": round(test_predict_s, 3),
        "dev_batch_predict_s": round(dev_predict_s, 3),
        "latency_sample_size": latency_count,
        "latency_ms_mean": round(statistics.fmean(latency_values), 3)
        if latency_values
        else None,
        "latency_ms_p50": latency_stats["p50"],
        "latency_ms_p95": latency_stats["p95"],
        "latency_values_ms": [round(value, 3) for value in latency_values],
        "batch_vs_single_mismatch": mismatch,
        "test_size": len(test_rows),
        "dev_size": len(dev_rows),
        "test_prediction_digest": _prediction_digest(test_rows, test_predictions),
        "dev_prediction_digest": _prediction_digest(dev_rows, dev_predictions),
        "batch_size": RF_BATCH_SIZE if model_id == "rf" else DEFAULT_BATCH_SIZE,
        "device": device,
        "package_versions": {
            name: package_version(name)
            for name in ("numpy", "scikit-learn", "torch", "transformers", "fasttext")
        },
        "finished_at": finished_at,
        "elapsed_s": elapsed_s,
    }
    write_json_atomic(meta_json, meta_payload)
    print(
        f"[worker:{model_id}] 完成 {elapsed_s:.2f}s | latency_mean="
        f"{meta_payload['latency_ms_mean']}ms | size={size_bytes}B",
        flush=True,
    )
    return 0


# ---------------------------------------------------------------------------
# 指标计算
# ---------------------------------------------------------------------------
def label_indices(
    rows: Sequence[tuple[str, str]], predictions: Sequence[str], classes: Sequence[str]
) -> tuple[list[int], list[int]]:
    index_of = {name: index for index, name in enumerate(classes)}
    if len(rows) != len(predictions):
        raise ValueError(f"标签与预测数量不一致: {len(rows)} != {len(predictions)}")
    true_indices: list[int] = []
    pred_indices: list[int] = []
    for (_, label), prediction in zip(rows, predictions):
        true_indices.append(index_of[label])
        pred_indices.append(index_of[prediction])
    return true_indices, pred_indices


def evaluate(
    rows: Sequence[tuple[str, str]],
    predictions: Sequence[str],
    classes: Sequence[str],
) -> dict[str, Any]:
    from sklearn.metrics import (
        accuracy_score,
        confusion_matrix,
        f1_score,
        precision_recall_fscore_support,
    )

    true_indices, pred_indices = label_indices(rows, predictions, classes)
    label_ids = list(range(len(classes)))
    macro_f1 = float(
        f1_score(true_indices, pred_indices, labels=label_ids, average="macro", zero_division=0)
    )
    accuracy = float(accuracy_score(true_indices, pred_indices))
    matrix = confusion_matrix(true_indices, pred_indices, labels=label_ids)
    precision, recall, f1, support = precision_recall_fscore_support(
        true_indices, pred_indices, labels=label_ids, average=None, zero_division=0
    )
    per_class = [
        {
            "class_id": name,
            "precision": round(float(precision[index]), 6),
            "recall": round(float(recall[index]), 6),
            "f1": round(float(f1[index]), 6),
            "support": int(support[index]),
        }
        for index, name in enumerate(classes)
    ]
    return {
        "macro_f1": round(macro_f1, 6),
        "accuracy": round(accuracy, 6),
        "confusion_matrix": matrix.astype(int).tolist(),
        "per_class": per_class,
    }


def normalize_weights(dev_macro_f1: dict[str, float]) -> dict[str, float]:
    """按 dev macro-F1 归一化；四舍五入 6 位后把误差补到最大项，保证和为 1。"""
    if not dev_macro_f1:
        return {}
    total = sum(max(value, 0.0) for value in dev_macro_f1.values())
    if total <= 0:
        equal = 1.0 / len(dev_macro_f1)
        weights = {model_id: equal for model_id in dev_macro_f1}
    else:
        weights = {
            model_id: max(value, 0.0) / total
            for model_id, value in dev_macro_f1.items()
        }
    rounded = {model_id: round(value, 6) for model_id, value in weights.items()}
    largest = max(rounded, key=lambda model_id: rounded[model_id])
    rounded[largest] = round(rounded[largest] + (1.0 - sum(rounded.values())), 6)
    return rounded


def weighted_vote(
    predictions_by_model: dict[str, Sequence[str]],
    weights: dict[str, float],
    classes: Sequence[str],
) -> list[str]:
    """加权投票：平票先比非加权票数，再按 class.txt 类别顺序（索引小者胜）。"""
    model_ids = list(predictions_by_model)
    if not model_ids:
        raise ValueError("没有可用于集成投票的模型")
    size = len(predictions_by_model[model_ids[0]])
    index_of = {name: index for index, name in enumerate(classes)}
    results: list[str] = []
    for position in range(size):
        scores = [0.0] * len(classes)
        counts = [0] * len(classes)
        for model_id in model_ids:
            class_index = index_of[predictions_by_model[model_id][position]]
            scores[class_index] += float(weights[model_id])
            counts[class_index] += 1
        best = max(scores)
        tied = [
            index
            for index, value in enumerate(scores)
            if value >= best - 1e-12
        ]
        if len(tied) > 1:
            best_count = max(counts[index] for index in tied)
            tied = [index for index in tied if counts[index] == best_count]
        results.append(classes[tied[0]])
    return results


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _artifact_target(args: argparse.Namespace) -> Path:
    if args.out:
        path = Path(args.out)
        return path if path.is_absolute() else PROJECT_ROOT / path
    date = datetime.now().strftime("%Y-%m-%d")
    base = BENCH_DIR / f"pk_{date}.json"
    if not base.exists():
        return base
    # 同日重跑：不覆盖历史同名不同内容，追加时间后缀。
    stamp = datetime.now().strftime("%Y-%m-%dT%H%M%S")
    candidate = BENCH_DIR / f"pk_{date}T{stamp}.json"
    counter = 2
    while candidate.exists():
        candidate = BENCH_DIR / f"pk_{date}T{stamp}_{counter}.json"
        counter += 1
    return candidate


def _load_model_result(
    model_id: str,
    run_dir: Path,
    classes: Sequence[str],
) -> dict[str, Any]:
    meta_payload = json.loads(
        (run_dir / f"_pred_{model_id}_meta.json").read_text(encoding="utf-8")
    )
    test_records = read_jsonl(run_dir / f"_pred_{model_id}_test.jsonl")
    dev_records = read_jsonl(run_dir / f"_pred_{model_id}_dev.jsonl")
    test_rows = [(record["title"], record["true_label"]) for record in test_records]
    test_predictions = [record["pred_class"] for record in test_records]
    dev_rows = [(record["title"], record["true_label"]) for record in dev_records]
    dev_predictions = [record["pred_class"] for record in dev_records]

    test_metrics = evaluate(test_rows, test_predictions, classes)
    dev_metrics = evaluate(dev_rows, dev_predictions, classes)

    latency_values = [
        float(record["latency_ms"])
        for record in test_records
        if record.get("latency_ms") is not None
    ]
    latency_stats = percentiles(latency_values, (50, 95))

    spec = MODEL_SPECS[model_id]
    return {
        "model_id": model_id,
        "display_name": spec["display_name"],
        "status": "ok",
        "model_version": meta_payload["model_version"],
        "model_files": meta_payload["model_files"],
        "metrics": {
            "macro_f1": test_metrics["macro_f1"],
            "accuracy": test_metrics["accuracy"],
            "avg_latency_ms": round(statistics.fmean(latency_values), 3)
            if latency_values
            else None,
            "p50_latency_ms": latency_stats["p50"],
            "p95_latency_ms": latency_stats["p95"],
            "latency_sample_size": len(latency_values),
            "size_bytes": int(meta_payload["size_bytes"]),
            "size_mib": round(int(meta_payload["size_bytes"]) / (1024**2), 2),
        },
        "confusion_matrix": test_metrics["confusion_matrix"],
        "per_class": test_metrics["per_class"],
        "dev_metrics": {
            "sample_size": len(dev_rows),
            "macro_f1": dev_metrics["macro_f1"],
            "accuracy": dev_metrics["accuracy"],
            "source": "00-data/data/dev.txt 固定种子分层抽样（仅用于集成权重）",
        },
        "prediction_digest": {
            "test": meta_payload["test_prediction_digest"],
            "dev": meta_payload["dev_prediction_digest"],
        },
        "load_s": meta_payload["load_s"],
        "test_batch_predict_s": meta_payload["test_batch_predict_s"],
        "device": meta_payload["device"],
        "_test_predictions": test_predictions,
        "_dev_predictions": dev_predictions,
        "_test_rows": test_rows,
    }


def _model_error_entry(model_id: str, reason: str, detail: str) -> dict[str, Any]:
    return {
        "model_id": model_id,
        "display_name": MODEL_SPECS[model_id]["display_name"],
        "status": "error",
        "reason": reason,
        "detail": detail,
    }


def run_main(args: argparse.Namespace) -> int:
    classes = load_classes()
    test_rows = read_rows(TEST_FILE, classes)
    dev_rows = read_rows(DEV_FILE, classes)
    if len(test_rows) == 0:
        raise ValueError(f"测试集为空: {relpath(TEST_FILE)}")

    unknown = sorted({label for _, label in test_rows} - set(classes))
    if unknown:
        raise ValueError(f"test.txt 存在 class.txt 之外的标签: {unknown}")

    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    run_dir = BENCH_DIR
    model_ids = [
        item.strip() for item in args.models.split(",") if item.strip()
    ]
    unknown_models = sorted(set(model_ids) - set(MODEL_ORDER))
    if unknown_models:
        raise ValueError(f"--models 含未知模型: {unknown_models}")

    dev_sample, dev_per_class_actual = select_dev_sample(
        dev_rows, classes, args.seed, args.dev_per_class
    )
    dev_sample_file = run_dir / f"_dev_sample_seed{args.seed}_per_class{args.dev_per_class}.txt"
    dev_sample_file.write_text(
        "".join(f"{title}\t{label}\n" for _, (title, label) in dev_sample),
        encoding="utf-8",
    )

    # 清理上次的逐模型中间产物，避免失败重跑时 verify 读到陈旧结果。
    for pattern in ("_pred_*.jsonl", "_pred_*_meta.json"):
        for stale in run_dir.glob(pattern):
            stale.unlink()

    git_info = collect_git_info()
    code_version = (
        "uncommitted"
        if git_info.get("dirty") or git_info.get("head_short_sha") is None
        else f"git:{git_info['head_short_sha']}"
    )
    environment = environment_info()
    started_at = now_iso()
    total_started = time.perf_counter()
    artifact_path = _artifact_target(args)

    model_entries: list[dict[str, Any]] = []
    per_model_elapsed: dict[str, float] = {}
    for model_id in model_ids:
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--worker",
            model_id,
            "--run-dir",
            str(run_dir),
            "--dev-sample-file",
            str(dev_sample_file),
            "--latency-subset",
            str(args.latency_subset),
        ]
        print(
            f"[bench] {model_id}: 启动子进程（test={len(test_rows)} dev={len(dev_sample)}）",
            flush=True,
        )
        model_started = time.perf_counter()
        process = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        elapsed = round(time.perf_counter() - model_started, 3)
        per_model_elapsed[model_id] = elapsed
        if process.returncode != 0:
            tail = (process.stderr or process.stdout or "").strip().splitlines()
            detail = "\n".join(tail[-12:]) if tail else "无 stderr 输出"
            print(f"[bench] {model_id}: 失败（exit={process.returncode}, {elapsed}s）")
            print(detail, flush=True)
            model_entries.append(
                _model_error_entry(
                    model_id,
                    reason=f"worker_exit_{process.returncode}",
                    detail=detail[-2000:],
                )
            )
            continue
        try:
            entry = _load_model_result(model_id, run_dir, classes)
        except Exception as exc:  # 中间产物损坏或指标计算失败
            detail = f"{type(exc).__name__}: {exc}"
            print(f"[bench] {model_id}: 结果合并失败: {detail}", flush=True)
            model_entries.append(
                _model_error_entry(model_id, reason="bench_result_error", detail=detail)
            )
            continue
        model_entries.append(entry)
        metrics = entry["metrics"]
        print(
            f"[bench] {model_id}: ok {elapsed}s | macro_f1={metrics['macro_f1']:.4f} "
            f"acc={metrics['accuracy']:.4f} avg_latency={metrics['avg_latency_ms']}ms "
            f"size={metrics['size_bytes']}B dev_f1={entry['dev_metrics']['macro_f1']:.4f}",
            flush=True,
        )

    ok_models = [entry for entry in model_entries if entry["status"] == "ok"]
    failed_models = [entry for entry in model_entries if entry["status"] == "error"]

    # dev macro-F1 作为集成权重（LLM missing / 失败模型不参与）。
    dev_f1 = {
        entry["model_id"]: float(entry["dev_metrics"]["macro_f1"]) for entry in ok_models
    }
    weights = normalize_weights(dev_f1)
    for entry in ok_models:
        entry["weight"] = weights[entry["model_id"]]
        entry["weight_source"] = "dev_sample_macro_f1"

    # 派生指标：加权投票 + oracle 参考上界。
    derived: list[dict[str, Any]] = []
    if len(ok_models) >= 2:
        predictions_by_model = {
            entry["model_id"]: entry["_test_predictions"] for entry in ok_models
        }
        vote_predictions = weighted_vote(predictions_by_model, weights, classes)
        vote_metrics = evaluate(test_rows, vote_predictions, classes)
        derived.append(
            {
                "kind": "weighted_vote",
                "display_name": "dev 加权投票集成",
                "status": "ok",
                "metrics": {
                    "macro_f1": vote_metrics["macro_f1"],
                    "accuracy": vote_metrics["accuracy"],
                    "avg_latency_ms": None,
                    "latency_sample_size": 0,
                    "note": "集成延迟未测量（派生指标，不参与延迟比较）",
                },
                "confusion_matrix": vote_metrics["confusion_matrix"],
                "weights": dict(weights),
                "participating_models": [entry["model_id"] for entry in ok_models],
                "note": (
                    "派生指标，非独立模型：每个模型 1 票，票重=dev 抽样 macro-F1 归一化；"
                    "平票先比非加权票数、再按 class.txt 类别顺序。权重未使用 test。"
                ),
            }
        )

        # oracle：任一本地模型预测正确即正确（仅参考上界）。
        oracle_predictions_list: list[str] = []
        for position in range(len(test_rows)):
            truth = test_rows[position][1]
            oracle_predictions_list.append(
                truth
                if any(
                    entry["_test_predictions"][position] == truth for entry in ok_models
                )
                else ok_models[0]["_test_predictions"][position]
            )
        oracle_metrics = evaluate(test_rows, oracle_predictions_list, classes)
        derived.append(
            {
                "kind": "oracle",
                "display_name": "Oracle 上界（任一模型正确即正确）",
                "status": "ok",
                "metrics": {
                    "macro_f1": oracle_metrics["macro_f1"],
                    "accuracy": oracle_metrics["accuracy"],
                    "avg_latency_ms": None,
                    "latency_sample_size": 0,
                },
                "confusion_matrix": oracle_metrics["confusion_matrix"],
                "participating_models": [entry["model_id"] for entry in ok_models],
                "note": (
                    "参考上界，非独立模型，不可部署：仅表示 4 个可用本地模型并集"
                    "的覆盖上限；不得当作某模型性能或对外宣称。"
                ),
            }
        )
    else:
        derived.append(
            {
                "kind": "weighted_vote",
                "display_name": "dev 加权投票集成",
                "status": "missing",
                "reason": "insufficient_local_models",
                "detail": f"可用本地模型 {len(ok_models)} 个，少于 2 个，无法形成集成。",
            }
        )
        derived.append(
            {
                "kind": "oracle",
                "display_name": "Oracle 上界（任一模型正确即正确）",
                "status": "missing",
                "reason": "insufficient_local_models",
                "detail": f"可用本地模型 {len(ok_models)} 个，无法计算参考上界。",
            }
        )

    missing_models = ["llm"] + [entry["model_id"] for entry in failed_models]

    # 组装产物（内部字段以 _ 开头，写盘前剔除）。
    public_models: list[dict[str, Any]] = []
    for entry in model_entries:
        public_entry = {key: value for key, value in entry.items() if not key.startswith("_")}
        public_models.append(public_entry)

    # LLM 固定作为第 5 个模型出现：status=missing，明确「未调用、无指标」，
    # 不能从 models 列表里消失（否则前端无法展示缺失原因）。
    public_models.append(dict(LLM_ENTRY))

    model_versions = {
        entry["model_id"]: entry.get("model_version", "unavailable")
        for entry in public_models
    }
    model_versions["llm"] = "external_provider_not_authorized(missing)"

    test_elapsed = round(time.perf_counter() - total_started, 3)
    finished_at = now_iso()
    command_text = (
        f'"{sys.executable}" tools/bench_signature.py --seed {args.seed} '
        f"--dev-per-class {args.dev_per_class} --latency-subset {args.latency_subset}"
    )
    dev_sample_meta = {
        "source": "00-data/data/dev.txt",
        "method": "stratified_by_true_label_per_class",
        "per_class": args.dev_per_class,
        "per_class_actual": dev_per_class_actual,
        "sample_size": len(dev_sample),
        "seed": args.seed,
        "file": relpath(dev_sample_file),
        "purpose": "仅用于加权投票的集成权重（各本地模型 dev macro-F1）",
        "test_usage": "未使用 test 做权重/阈值/调参；test 仅用于最终评测",
    }
    artifact: dict[str, Any] = {
        "schema_version": 1,
        "generated_by": "tools/bench_signature.py",
        "meta": {
            "dataset": "00-data/data/test.txt",
            "split": "test",
            "sample_size": len(test_rows),
            "generated_at": finished_at,
            "code_version": code_version,
            "model_versions": model_versions,
            "metric_def": {
                "macro_f1": (
                    "sklearn f1_score(labels=class.txt 0..9, average='macro', "
                    "zero_division=0)：10 类等权算术平均，不按 support 加权"
                ),
                "accuracy": "top-1 命中率 = 预测正确样本数 / test 样本数（10,000）",
                "confusion_matrix": (
                    "10x10 整数矩阵：行=true label 0..9，列=pred label 0..9，"
                    "类别顺序=00-data/data/class.txt；行和=该类 support"
                ),
                "latency": (
                    "平均单条延迟（ms）：test 前 200 条逐条调用 SERVICE.predict，"
                    "含 jieba/tokenizer 预处理，不含 HTTP/网络/JSON；同时记录 p50/p95。"
                    "指标预测走批量推理，且批量与逐条前 200 条预测一致性校验=0 才发布"
                ),
                "size_bytes": (
                    "模型权重文件磁盘字节数之和（1 B = 1 字节；MiB=bytes/1048576 仅展示）。"
                    "RF=rf.pkl+tfidf.pkl；BERT 不含外部 tokenizer 目录"
                ),
                "weight": (
                    f"集成权重 = dev 抽样（seed={args.seed}，每类 {args.dev_per_class} 条，"
                    f"共 {len(dev_sample)} 条）macro-F1 归一化；四舍五入 6 位后校正最大项使总和=1；"
                    "仅 dev、未使用 test；LLM missing 不参与"
                ),
                "vote": (
                    "加权投票：每模型 1 票，票重=weight；取加权和最大类；"
                    "平票先比非加权票数，再按 class.txt 类别顺序（索引小者胜）"
                ),
                "oracle": (
                    "参考上界：4 个可用本地模型中任一预测正确即算正确；"
                    "非独立模型、不可部署，不得当作对外性能宣称"
                ),
            },
            "data_kind": "offline",
        },
        "build": {
            "git_head_short_sha": git_info.get("head_short_sha"),
            "git_dirty": git_info.get("dirty"),
            "code_version_note": (
                "工作树包含未提交改动（含本脚本），meta.code_version 如实写 uncommitted"
                if code_version == "uncommitted"
                else "工作树干净，meta.code_version=git:<short-sha>"
            ),
        },
        "environment": environment,
        "dataset": {            "path": "00-data/data/test.txt",
            "split": "test",
            "sample_size": len(test_rows),
            "class_count": len(classes),
            "classes": classes,
            "label_format": "title<TAB>label(0..9 索引，按 class.txt 行序映射为类别名)",
            "dev_weight_sample": dev_sample_meta,
        },
        "models": public_models,
        "derived": derived,
        "missing_models": missing_models,
        "reproduction": {
            "command": command_text,
            "cwd": ".",
            "seed": args.seed,
            "dev_sample": dev_sample_meta,
            "latency_subset": args.latency_subset,
            "models": model_ids,
            "source": "test.txt",
            "intermediate_dir": relpath(run_dir),
            "artifact_path": relpath(artifact_path),
            "notes": [
                "LLM 是外部 Provider，本轮禁止调用；产物中 status=missing，无指标",
                "每个模型单独子进程加载/推理，避免 RF+BERT 同时驻留造成内存峰值",
                "只运行一次全量推理；中间预测产物用于 verify_bench 确定性复核",
                "未训练、未校准、未在 test 上调阈值/权重",
            ],
        },
        "run": {
            "started_at": started_at,
            "finished_at": finished_at,
            "total_elapsed_s": test_elapsed,
            "per_model_elapsed_s": per_model_elapsed,
            "artifact_path": relpath(artifact_path),
            "python_executable": sys.executable,
            "cpu_count": os.cpu_count(),
        },
    }

    # B13 契约自检：不通过则不落盘。
    validator = load_meta_validator()
    try:
        validator.validate_document(artifact)
    except Exception as exc:
        print(f"[bench] META_INVALID: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    write_json_atomic(artifact_path, artifact)

    print("")
    print(f"[bench] 产物: {relpath(artifact_path)}")
    print(
        f"[bench] 总耗时 {test_elapsed:.1f}s | 模型: "
        + ", ".join(
            f"{entry['model_id']}={'ok' if entry['status'] == 'ok' else entry['status']}"
            for entry in public_models
        )
        + " | LLM=missing"
    )
    for entry in public_models:
        if entry["status"] != "ok":
            continue
        metrics = entry["metrics"]
        print(
            f"[bench]   {entry['model_id']:<8} macro_f1={metrics['macro_f1']:.4f} "
            f"acc={metrics['accuracy']:.4f} avg_latency={metrics['avg_latency_ms']}ms "
            f"size={metrics['size_bytes']}B weight={entry.get('weight')}"
        )
    for entry in derived:
        if entry["status"] == "ok":
            print(
                f"[bench]   {entry['kind']:<14} macro_f1={entry['metrics']['macro_f1']:.4f} "
                f"acc={entry['metrics']['accuracy']:.4f}"
            )
    print(f"[bench] meta validation: PASS ({relpath(artifact_path)})")

    if failed_models:
        print(
            f"[bench] WARNING: {len(failed_models)} 个本地模型失败，产物为部分结果",
            file=sys.stderr,
        )
        return 1
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="B8：生成离线基准产物 artifacts/bench/pk_<date>.json"
    )
    parser.add_argument(
        "--models",
        default=",".join(MODEL_ORDER),
        help=f"逗号分隔的本地模型（默认 {','.join(MODEL_ORDER)}）",
    )
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help="dev 抽样种子")
    parser.add_argument(
        "--dev-per-class",
        type=int,
        default=DEFAULT_DEV_PER_CLASS,
        help="dev 每类抽样条数（默认 200 -> 2,000 条）",
    )
    parser.add_argument(
        "--latency-subset",
        type=int,
        default=DEFAULT_LATENCY_SUBSET,
        help="逐条延迟测量的 test 前缀样本数（默认 200）",
    )
    parser.add_argument("--out", default=None, help="显式产物路径（默认 pk_<date>.json）")
    parser.add_argument(
        "--worker",
        choices=MODEL_ORDER,
        default=None,
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--run-dir", default=str(BENCH_DIR), help=argparse.SUPPRESS)
    parser.add_argument("--dev-sample-file", default=None, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.worker:
        if not args.dev_sample_file:
            print("--worker 必须配合 --dev-sample-file 使用", file=sys.stderr)
            return 2
        return worker_main(args)
    return run_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
