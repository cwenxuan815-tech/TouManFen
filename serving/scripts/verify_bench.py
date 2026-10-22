#!/usr/bin/env python
"""B8 离线基准产物与 PK API 验收（离线/TestClient；不加载模型、不跑全量推理）。

运行环境（与项目其它 verify 脚本一致，通过 PYTHONPATH 注入网关依赖）::

    $env:PYTHONPATH='E:\\AIModels\\test\\TouManFen\\venv-gateway\\Lib\\site-packages'
    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/verify_bench.py

覆盖点（对应 docs/工程实施工作清单_75工时.md §6.8 / B8 / B13）：

1. 产物存在、JSON 合法，且通过 ``tools/validate_meta.py`` 的 §6.8 契约校验；
2. meta / dataset 口径：dataset=test.txt、split=test、sample_size=10,000、
   data_kind=offline、带时区 generated_at、code_version、metric_def 键齐全；
3. 模型覆盖：rf/fasttext/bert/distill=ok + llm=missing（外部 Provider 未授权，
   无 metrics、无 weight），missing_models=["llm"]；各指标范围、10x10 混淆矩阵
   形状与行和、模型体积、延迟样本数；
4. 派生指标：weighted_vote / oracle 存在且标注「派生/非独立/参考上界」，
   oracle 不低于任一单模型；权重和为 1；
5. 确定性复核（**不重复推理**）：从 ``artifacts/bench/_pred_*.jsonl`` 复算
   预测摘要（sha256）、macro-F1/accuracy/混淆矩阵、dev 权重、加权投票与
   oracle，与产物逐项对拍；并对 test 前 200 条逐条延迟均值做一致性检查；
6. API ``GET /api/v1/bench/pk``：返回 request_id + 相对 artifact_path + 产物；
   不返回绝对路径、不接受路径参数；缺失产物 404、非法产物 503 统一错误体；
   不写默认 ``serving/run/tmf.db``；
7. 若 ``serving/gateway/main.py`` 尚未 include bench 路由，打印 Lead 需要的
   两行集成代码（本任务不修改 main.py，不作为失败项）。

输出：逐项 PASS/FAIL，最后 RESULT PASS|FAIL，退出码 0/1。
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

try:  # 让中文输出在 cp936 控制台也不至于抛 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

FAILURES: list[str] = []
TOTAL = 0
INFOS: list[str] = []

BENCH_DIR = PROJECT_ROOT / "artifacts" / "bench"
CLASS_FILE = PROJECT_ROOT / "00-data" / "data" / "class.txt"
TEST_FILE = PROJECT_ROOT / "00-data" / "data" / "test.txt"
DEV_FILE = PROJECT_ROOT / "00-data" / "data" / "dev.txt"
DEFAULT_DB = PROJECT_ROOT / "serving" / "run" / "tmf.db"
ARTIFACT_PATTERN = re.compile(r"^pk_[0-9A-Za-z_-]+\.json$")
LOCAL_MODEL_IDS = ["rf", "fasttext", "bert", "distill"]
MODEL_ORDER = [*LOCAL_MODEL_IDS, "llm"]
REQUIRED_METRIC_DEF_KEYS = [
    "macro_f1",
    "accuracy",
    "confusion_matrix",
    "latency",
    "size_bytes",
    "weight",
    "vote",
    "oracle",
]


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def check(name: str, condition: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


def info(name: str, message: str) -> None:
    INFOS.append(f"{name}: {message}")
    print(f"INFO {name}: {message}")


def relpath(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def load_classes() -> list[str]:
    return [line.strip() for line in CLASS_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]


def read_rows(path: Path) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        title, label = line.rsplit("\t", 1)
        rows.append((title, label.strip()))
    return rows


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def find_latest_artifact(directory: Path) -> Path | None:
    if not directory.is_dir():
        return None
    best: Path | None = None
    best_mtime = float("-inf")
    for candidate in directory.iterdir():
        if candidate.is_symlink() or not candidate.is_file():
            continue
        if not ARTIFACT_PATTERN.match(candidate.name):
            continue
        try:
            mtime = candidate.stat().st_mtime
        except OSError:
            continue
        if mtime > best_mtime:
            best, best_mtime = candidate, mtime
    return best


def load_meta_validator() -> Any:
    path = PROJECT_ROOT / "tools" / "validate_meta.py"
    spec = importlib.util.spec_from_file_location("_verify_validate_meta", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise RuntimeError(f"无法加载 meta 校验器: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def prediction_digest(records: Sequence[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for index, record in enumerate(records):
        digest.update(
            f"{index}\t{record['true_label']}\t{record['pred_class']}\n".encode(
                "utf-8"
            )
        )
    return digest.hexdigest()


def indices_of(
    records: Sequence[dict[str, Any]], classes: Sequence[str]
) -> tuple[list[int], list[int]]:
    index_of = {name: index for index, name in enumerate(classes)}
    true_indices = [index_of[record["true_label"]] for record in records]
    pred_indices = [index_of[record["pred_class"]] for record in records]
    return true_indices, pred_indices


def recompute_metrics(
    records: Sequence[dict[str, Any]], classes: Sequence[str]
) -> dict[str, Any]:
    from sklearn.metrics import accuracy_score, confusion_matrix, f1_score

    true_indices, pred_indices = indices_of(records, classes)
    labels = list(range(len(classes)))
    return {
        "macro_f1": round(
            float(
                f1_score(
                    true_indices,
                    pred_indices,
                    labels=labels,
                    average="macro",
                    zero_division=0,
                )
            ),
            6,
        ),
        "accuracy": round(float(accuracy_score(true_indices, pred_indices)), 6),
        "confusion_matrix": confusion_matrix(
            true_indices, pred_indices, labels=labels
        ).astype(int).tolist(),
    }


def normalize_weights(dev_macro_f1: dict[str, float]) -> dict[str, float]:
    """与 tools/bench_signature.py 相同的归一化/取整规则（独立实现用于对拍）。"""
    total = sum(max(value, 0.0) for value in dev_macro_f1.values())
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
    model_ids = list(predictions_by_model)
    size = len(predictions_by_model[model_ids[0]])
    index_of = {name: index for index, name in enumerate(classes)}
    results: list[str] = []
    for position in range(size):
        scores = [0.0] * len(classes)
        counts = [0] * len(classes)
        for model_id in model_ids:
            index = index_of[predictions_by_model[model_id][position]]
            scores[index] += float(weights[model_id])
            counts[index] += 1
        best = max(scores)
        tied = [i for i, value in enumerate(scores) if value >= best - 1e-12]
        if len(tied) > 1:
            best_count = max(counts[i] for i in tied)
            tied = [i for i in tied if counts[i] == best_count]
        results.append(classes[tied[0]])
    return results


def oracle_vote(
    predictions_by_model: dict[str, Sequence[str]],
    truth: Sequence[str],
) -> list[str]:
    model_ids = list(predictions_by_model)
    results: list[str] = []
    for position, label in enumerate(truth):
        if any(
            predictions_by_model[model_id][position] == label
            for model_id in model_ids
        ):
            results.append(label)
        else:
            results.append(predictions_by_model[model_ids[0]][position])
    return results


def error_code(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    error = body.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None


# ---------------------------------------------------------------------------
# 1. 产物 / meta 契约
# ---------------------------------------------------------------------------
def artifact_checks(artifact_path: Path) -> dict[str, Any]:
    check("artifact: 文件存在", artifact_path.is_file(), relpath(artifact_path))
    document = json.loads(artifact_path.read_text(encoding="utf-8"))
    check("artifact: 顶层是 JSON 对象", isinstance(document, dict))

    validator = load_meta_validator()
    meta_error = None
    try:
        meta = validator.validate_document(document)
    except Exception as exc:
        meta_error = f"{type(exc).__name__}: {exc}"
        meta = None
    check("meta: 通过 B13 tools/validate_meta.py 契约校验", meta is not None, meta_error or "")

    classes = load_classes()
    test_rows = read_rows(TEST_FILE)
    dev_rows = read_rows(DEV_FILE)

    top_missing = [
        key
        for key in (
            "meta",
            "dataset",
            "models",
            "derived",
            "missing_models",
            "reproduction",
        )
        if key not in document
    ]
    check("artifact: 必需顶层字段齐全", not top_missing, str(top_missing))
    check(
        "artifact: schema_version/generated_by 正确",
        document.get("schema_version") == 1
        and document.get("generated_by") == "tools/bench_signature.py",
        f"{document.get('schema_version')}/{document.get('generated_by')}",
    )

    raw_meta = document.get("meta", {})
    if isinstance(raw_meta, dict):
        check(
            "meta.dataset/split/data_kind 口径",
            raw_meta.get("dataset") == "00-data/data/test.txt"
            and raw_meta.get("split") == "test"
            and raw_meta.get("data_kind") == "offline",
            f"{raw_meta.get('dataset')}/{raw_meta.get('split')}/{raw_meta.get('data_kind')}",
        )
        check(
            "meta.sample_size == test.txt 实际行数",
            raw_meta.get("sample_size") == len(test_rows),
            f"{raw_meta.get('sample_size')} vs {len(test_rows)}",
        )
        check(
            "meta.generated_at 带显式时区",
            isinstance(raw_meta.get("generated_at"), str)
            and re.search(r"(Z|[+-]\d{2}:\d{2})$", raw_meta["generated_at"]) is not None,
            str(raw_meta.get("generated_at")),
        )
        code_version = str(raw_meta.get("code_version", ""))
        check(
            "meta.code_version 为 uncommitted 或 git:<sha>",
            code_version == "uncommitted"
            or re.fullmatch(r"git:[0-9a-f]{7,40}", code_version) is not None,
            code_version,
        )
        versions = raw_meta.get("model_versions")
        check(
            "meta.model_versions 覆盖 5 个模型且值非空",
            isinstance(versions, dict)
            and set(versions) == set(MODEL_ORDER)
            and all(isinstance(value, str) and value.strip() for value in versions.values()),
            str(versions),
        )
        metric_def = raw_meta.get("metric_def")
        check(
            "meta.metric_def 口径键齐全且为字符串",
            isinstance(metric_def, dict)
            and all(
                key in metric_def and isinstance(metric_def[key], str) and metric_def[key].strip()
                for key in REQUIRED_METRIC_DEF_KEYS
            ),
            str(sorted(metric_def) if isinstance(metric_def, dict) else metric_def),
        )

    dataset = document.get("dataset", {})
    check(
        "dataset: classes 与 class.txt 一致（10 类）",
        isinstance(dataset, dict)
        and dataset.get("classes") == classes
        and dataset.get("class_count") == len(classes),
        str(dataset.get("classes") if isinstance(dataset, dict) else dataset),
    )
    dev_sample = dataset.get("dev_weight_sample", {}) if isinstance(dataset, dict) else {}
    check(
        "dataset.dev_weight_sample: 只来自 dev、固定种子、未用 test",
        isinstance(dev_sample, dict)
        and str(dev_sample.get("source", "")).endswith("00-data/data/dev.txt")
        and int(dev_sample.get("seed", -1))
        == int(document.get("reproduction", {}).get("seed", -2))
        and int(dev_sample.get("sample_size", 0)) == 2000
        and "未使用 test" in str(dev_sample.get("test_usage", "")),
        str(dev_sample)[:300],
    )
    check(
        "dataset: 10,000 条 test、10 类、dev 样本量 = 每类 200",
        len(test_rows) == 10000
        and len(classes) == 10
        and int(dev_sample.get("sample_size", 0)) == 2000
        and int(dev_sample.get("per_class", 0)) == 200,
        f"test={len(test_rows)} classes={len(classes)} dev_sample={dev_sample.get('sample_size')}",
    )

    # 保存上下文给后续 section
    document["_verify_context"] = {
        "classes": classes,
        "test_rows": test_rows,
        "dev_rows": dev_rows,
    }
    return document


# ---------------------------------------------------------------------------
# 2. 模型覆盖 / 指标 / 混淆矩阵
# ---------------------------------------------------------------------------
def model_checks(document: dict[str, Any]) -> dict[str, Any]:
    context = document["_verify_context"]
    classes: list[str] = context["classes"]
    test_rows: list[tuple[str, str]] = context["test_rows"]
    models = document.get("models", [])
    check(
        "models: 恰好 5 个模型（4 本地 + llm）",
        isinstance(models, list) and len(models) == 5,
        str(len(models) if isinstance(models, list) else models),
    )
    check(
        "models: 顺序 rf/fasttext/bert/distill/llm",
        isinstance(models, list)
        and [entry.get("model_id") for entry in models] == MODEL_ORDER,
        str([entry.get("model_id") for entry in models]),
    )
    by_id = {entry.get("model_id"): entry for entry in models if isinstance(entry, dict)}

    for model_id in LOCAL_MODEL_IDS:
        entry = by_id.get(model_id, {})
        metrics = entry.get("metrics") or {}
        matrix = entry.get("confusion_matrix")
        matrix_ok = (
            isinstance(matrix, list)
            and len(matrix) == len(classes)
            and all(isinstance(row, list) and len(row) == len(classes) for row in matrix)
            and all(
                isinstance(value, int) and value >= 0
                for row in matrix
                for value in row
            )
        )
        row_sums = [sum(row) for row in matrix] if matrix_ok else []
        supports = [
            item.get("support") for item in (entry.get("per_class") or [])
        ] if isinstance(entry.get("per_class"), list) else []
        check(
            f"models.{model_id}: status=ok 且有指标/权重/dev 指标",
            entry.get("status") == "ok"
            and all(
                key in metrics
                for key in (
                    "macro_f1",
                    "accuracy",
                    "avg_latency_ms",
                    "size_bytes",
                    "latency_sample_size",
                )
            )
            and isinstance(entry.get("dev_metrics"), dict)
            and isinstance(entry.get("weight"), (int, float)),
            str(metrics)[:300],
        )
        check(
            f"models.{model_id}: macro-F1/accuracy ∈ [0,1]",
            isinstance(metrics.get("macro_f1"), (int, float))
            and isinstance(metrics.get("accuracy"), (int, float))
            and 0.0 <= float(metrics["macro_f1"]) <= 1.0
            and 0.0 <= float(metrics["accuracy"]) <= 1.0,
            f"{metrics.get('macro_f1')}/{metrics.get('accuracy')}",
        )
        check(
            f"models.{model_id}: 10x10 混淆矩阵形状/行和=10000/总计=10000",
            matrix_ok
            and sum(row_sums) == len(test_rows)
            and row_sums == supports,
            f"shape={len(matrix) if isinstance(matrix, list) else None} rows={row_sums[:3]}...",
        )
        check(
            f"models.{model_id}: 平均延迟/体积/延迟样本数合法",
            isinstance(metrics.get("avg_latency_ms"), (int, float))
            and float(metrics["avg_latency_ms"]) > 0
            and isinstance(metrics.get("size_bytes"), int)
            and int(metrics["size_bytes"]) > 1_000_000
            and isinstance(metrics.get("latency_sample_size"), int)
            and int(metrics["latency_sample_size"]) >= 100,
            f"latency={metrics.get('avg_latency_ms')} size={metrics.get('size_bytes')} "
            f"n={metrics.get('latency_sample_size')}",
        )
        test_digest = (entry.get("prediction_digest") or {}).get("test")
        check(
            f"models.{model_id}: 记录 test 预测摘要（便于确定性复核）",
            isinstance(test_digest, str) and len(test_digest) == 64,
            str(test_digest),
        )

    llm = by_id.get("llm", {})
    check(
        "models.llm: status=missing + reason=external_provider_not_authorized",
        llm.get("status") == "missing"
        and llm.get("reason") == "external_provider_not_authorized"
        and isinstance(llm.get("detail"), str)
        and bool(llm["detail"].strip()),
        str(llm)[:300],
    )
    check(
        "models.llm: 未伪造 metrics/weight/confusion_matrix",
        isinstance(llm, dict)
        and "metrics" not in llm
        and "weight" not in llm
        and "confusion_matrix" not in llm,
        str(sorted(llm)),
    )
    check(
        "missing_models 恰为 ['llm']",
        document.get("missing_models") == ["llm"],
        str(document.get("missing_models")),
    )

    weights = {
        model_id: float(by_id[model_id]["weight"])
        for model_id in LOCAL_MODEL_IDS
        if isinstance(by_id.get(model_id), dict) and "weight" in by_id[model_id]
    }
    check(
        "weights: 4 个本地模型权重和为 1",
        len(weights) == 4 and abs(sum(weights.values()) - 1.0) < 1e-6,
        f"{weights} sum={sum(weights.values())}",
    )
    context["weights"] = weights
    return document


# ---------------------------------------------------------------------------
# 3. 派生指标（集成 / oracle）
# ---------------------------------------------------------------------------
def derived_checks(document: dict[str, Any]) -> dict[str, Any]:
    context = document["_verify_context"]
    classes: list[str] = context["classes"]
    derived = document.get("derived")
    check("derived: 是非空数组", isinstance(derived, list) and len(derived) >= 2, str(type(derived)))
    by_kind = {
        entry.get("kind"): entry for entry in derived if isinstance(entry, dict)
    } if isinstance(derived, list) else {}
    check(
        "derived: 同时含 weighted_vote 与 oracle",
        {"weighted_vote", "oracle"} <= set(by_kind),
        str(sorted(by_kind)),
    )

    vote = by_kind.get("weighted_vote", {})
    vote_metrics = vote.get("metrics") or {}
    check(
        "derived.weighted_vote: status=ok 且标注派生/非独立/权重来自 dev",
        vote.get("status") == "ok"
        and isinstance(vote_metrics.get("macro_f1"), (int, float))
        and 0.0 <= float(vote_metrics["macro_f1"]) <= 1.0
        and "派生" in str(vote.get("note", ""))
        and "非独立" in str(vote.get("note", ""))
        and "dev" in str(vote.get("note", ""))
        and "test" in str(vote.get("note", "")),
        str(vote.get("note", ""))[:300],
    )
    vote_weights = vote.get("weights") or {}
    check(
        "derived.weighted_vote: weights 与模型权重一致且和为 1",
        isinstance(vote_weights, dict)
        and set(vote_weights) == set(LOCAL_MODEL_IDS)
        and all(
            abs(float(vote_weights[model_id]) - context["weights"][model_id]) < 1e-12
            for model_id in LOCAL_MODEL_IDS
        )
        and abs(sum(float(value) for value in vote_weights.values()) - 1.0) < 1e-6,
        str(vote_weights),
    )

    oracle = by_kind.get("oracle", {})
    oracle_metrics = oracle.get("metrics") or {}
    check(
        "derived.oracle: status=ok 且标注参考上界/非独立",
        oracle.get("status") == "ok"
        and isinstance(oracle_metrics.get("macro_f1"), (int, float))
        and "上界" in str(oracle.get("note", ""))
        and "非独立" in str(oracle.get("note", "")),
        str(oracle.get("note", ""))[:300],
    )
    check(
        "derived.oracle: accuracy 不低于任一单模型（并集覆盖的必然性质）",
        isinstance(oracle_metrics.get("macro_f1"), (int, float))
        and isinstance(oracle_metrics.get("accuracy"), (int, float))
        and 0.0 <= float(oracle_metrics["macro_f1"]) <= 1.0
        and float(oracle_metrics["accuracy"])
        >= max(
            float(entry["metrics"]["accuracy"])
            for entry in document.get("models", [])
            if isinstance(entry, dict) and entry.get("status") == "ok"
        )
        - 1e-9,
        f"oracle={oracle_metrics.get('accuracy')} max_model="
        f"{max(float(entry['metrics']['accuracy']) for entry in document.get('models', []) if isinstance(entry, dict) and entry.get('status') == 'ok')}",
    )
    context["derived"] = by_kind
    return document


# ---------------------------------------------------------------------------
# 4. 确定性复核（从中间预测产物复算，不做推理）
# ---------------------------------------------------------------------------
def reproduction_checks(document: dict[str, Any]) -> dict[str, Any]:
    context = document["_verify_context"]
    classes: list[str] = context["classes"]
    test_rows: list[tuple[str, str]] = context["test_rows"]
    models = {
        entry["model_id"]: entry
        for entry in document.get("models", [])
        if isinstance(entry, dict) and entry.get("status") == "ok"
    }
    reproduction = document.get("reproduction", {})
    check(
        "reproduction: command/seed/source/test 口径完整",
        isinstance(reproduction, dict)
        and "tools/bench_signature.py" in str(reproduction.get("command", ""))
        and isinstance(reproduction.get("seed"), int)
        and reproduction.get("source") == "test.txt",
        str(reproduction)[:300],
    )
    intermediate_dir = PROJECT_ROOT / str(reproduction.get("intermediate_dir", ""))
    check(
        "reproduction: 中间预测目录存在",
        intermediate_dir.is_dir(),
        relpath(intermediate_dir),
    )

    artifacts_models = {
        entry["model_id"]: entry
        for entry in document.get("models", [])
        if isinstance(entry, dict) and entry.get("status") == "ok"
    }
    test_predictions: dict[str, list[str]] = {}
    dev_predictions: dict[str, list[str]] = {}
    dev_f1: dict[str, float] = {}
    digest_ok = True
    metrics_ok = True
    detail = ""
    for model_id in LOCAL_MODEL_IDS:
        test_records = read_jsonl(intermediate_dir / f"_pred_{model_id}_test.jsonl")
        dev_records = read_jsonl(intermediate_dir / f"_pred_{model_id}_dev.jsonl")
        entry = artifacts_models[model_id]
        expected_digest = (entry.get("prediction_digest") or {}).get("test")
        if prediction_digest(test_records) != expected_digest:
            digest_ok = False
            detail = f"{model_id} test digest mismatch"
        recomputed = recompute_metrics(test_records, classes)
        if (
            recomputed["macro_f1"] != entry["metrics"]["macro_f1"]
            or recomputed["accuracy"] != entry["metrics"]["accuracy"]
            or recomputed["confusion_matrix"] != entry["confusion_matrix"]
        ):
            metrics_ok = False
            detail = f"{model_id} metrics mismatch"
        if len(test_records) != len(test_rows):
            metrics_ok = False
            detail = f"{model_id} test rows {len(test_records)} != {len(test_rows)}"
        latency_values = [
            float(record["latency_ms"])
            for record in test_records
            if record.get("latency_ms") is not None
        ]
        expected_latency = round(sum(latency_values) / len(latency_values), 3)
        if expected_latency != entry["metrics"]["avg_latency_ms"]:
            metrics_ok = False
            detail = (
                f"{model_id} latency mismatch {expected_latency} vs "
                f"{entry['metrics']['avg_latency_ms']}"
            )
        dev_metrics = recompute_metrics(dev_records, classes)
        dev_f1[model_id] = dev_metrics["macro_f1"]
        if dev_metrics["macro_f1"] != entry["dev_metrics"]["macro_f1"]:
            metrics_ok = False
            detail = f"{model_id} dev macro_f1 mismatch"
        test_predictions[model_id] = [record["pred_class"] for record in test_records]
        dev_predictions[model_id] = [record["pred_class"] for record in dev_records]

    check(
        "确定性: 中间预测 sha256 摘要与产物一致（test 10,000 条 x 4 模型）",
        digest_ok,
        detail,
    )
    check(
        "确定性: 从中间产物复算的 macro-F1/accuracy/混淆矩阵/延迟/dev-F1 与产物一致",
        metrics_ok,
        detail,
    )

    recomputed_weights = normalize_weights(dev_f1)
    check(
        "确定性: dev macro-F1 归一化权重与产物 weight 完全一致",
        all(
            abs(recomputed_weights[model_id] - context["weights"][model_id]) < 1e-12
            for model_id in LOCAL_MODEL_IDS
        ),
        f"recomputed={recomputed_weights}",
    )

    truth = [record["true_label"] for record in read_jsonl(intermediate_dir / "_pred_rf_test.jsonl")]
    vote_derived = context["derived"]["weighted_vote"]
    vote_predictions = weighted_vote(test_predictions, context["weights"], classes)
    vote_metrics = recompute_metrics(
        [
            {"true_label": label, "pred_class": prediction}
            for label, prediction in zip(truth, vote_predictions)
        ],
        classes,
    )
    check(
        "确定性: 加权投票集成可复算且与产物一致",
        vote_metrics["macro_f1"] == vote_derived["metrics"]["macro_f1"]
        and vote_metrics["accuracy"] == vote_derived["metrics"]["accuracy"]
        and vote_metrics["confusion_matrix"] == vote_derived["confusion_matrix"],
        f"recomputed={vote_metrics['macro_f1']} artifact={vote_derived['metrics']['macro_f1']}",
    )

    oracle_derived = context["derived"]["oracle"]
    oracle_predictions = oracle_vote(test_predictions, truth)
    oracle_metrics = recompute_metrics(
        [
            {"true_label": label, "pred_class": prediction}
            for label, prediction in zip(truth, oracle_predictions)
        ],
        classes,
    )
    check(
        "确定性: oracle 上界可复算且与产物一致",
        oracle_metrics["macro_f1"] == oracle_derived["metrics"]["macro_f1"]
        and oracle_metrics["accuracy"] == oracle_derived["metrics"]["accuracy"]
        and oracle_metrics["confusion_matrix"] == oracle_derived["confusion_matrix"],
        f"recomputed={oracle_metrics['macro_f1']} artifact={oracle_derived['metrics']['macro_f1']}",
    )

    dev_sample_file = PROJECT_ROOT / str(
        (document.get("dataset", {}).get("dev_weight_sample") or {}).get("file", "")
    )
    check(
        "确定性: dev 抽样文件存在且与 dev 中间预测样本量一致",
        dev_sample_file.is_file()
        and len(read_rows(dev_sample_file)) == 2000
        and all(len(records) == 2000 for records in dev_predictions.values()),
        relpath(dev_sample_file),
    )
    return document


# ---------------------------------------------------------------------------
# 5. API 契约（TestClient，不监听端口）
# ---------------------------------------------------------------------------
def api_checks(document: dict[str, Any], artifact_path: Path) -> None:
    from fastapi.testclient import TestClient

    from serving.gateway.main import create_app
    from serving.gateway.routers import bench as bench_router

    default_db_before = (
        (DEFAULT_DB.stat().st_mtime_ns, DEFAULT_DB.stat().st_size)
        if DEFAULT_DB.exists()
        else None
    )
    tmp_context = tempfile.TemporaryDirectory(prefix="tmf-bench-verify-")
    tmp = Path(tmp_context.name)
    os.environ["TMF_DB_PATH"] = str(tmp / "lifespan.db")
    os.environ.pop("TMF_BENCH_DIR", None)
    try:
        app = create_app()
        already_integrated = any(
            getattr(route, "path", None) == "/api/v1/bench/pk" for route in app.routes
        )
        if not already_integrated:
            app.include_router(bench_router.router)
            info(
                "main.py 集成待办",
                "serving/gateway/main.py 需新增："
                "`from .routers import bench as bench_router` 与 "
                "`app.include_router(bench_router.router)`（本任务未修改 main.py）",
            )
        else:
            info("main.py 集成", "已 include bench 路由")

        with TestClient(app) as client:
            expected_artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
            response = client.get("/api/v1/bench/pk")
            body = response.json()
            check(
                "api: GET /api/v1/bench/pk -> 200",
                response.status_code == 200,
                f"{response.status_code} {str(body)[:200]}",
            )
            check(
                "api: 顶层字段恰为 request_id/artifact_path/artifact",
                isinstance(body, dict)
                and set(body) == {"request_id", "artifact_path", "artifact"},
                str(sorted(body) if isinstance(body, dict) else body),
            )
            check(
                "api: request_id 与 X-Request-ID 响应头一致",
                isinstance(body, dict)
                and response.headers.get("X-Request-ID") == body.get("request_id"),
                f"{response.headers.get('X-Request-ID')} vs {body.get('request_id')}",
            )
            api_path = body.get("artifact_path", "") if isinstance(body, dict) else ""
            check(
                "api: artifact_path 为 bench 下 pk_*.json 的工程相对路径",
                isinstance(api_path, str)
                and not Path(api_path).is_absolute()
                and ".." not in api_path
                and "\\" not in api_path
                and api_path == relpath(artifact_path),
                api_path,
            )
            check(
                "api: artifact 内容与磁盘产物一致",
                body.get("artifact") == expected_artifact,
            )
            check(
                "api: 传任意 path/名字参数不改变读取目标（不接受任意路径）",
                client.get(
                    "/api/v1/bench/pk",
                    params={"path": "../../00-data/data/test.txt", "name": "x.json"},
                ).json().get("artifact_path")
                == relpath(artifact_path),
            )
            check(
                "api: 响应不含绝对路径/工程根路径",
                str(PROJECT_ROOT) not in response.text
                and str(PROJECT_ROOT).replace("\\", "\\\\") not in response.text,
            )

            # 缺失产物：目录存在但无 pk_*.json -> 404
            empty_dir = tmp / "empty_bench"
            empty_dir.mkdir()
            (empty_dir / "snapshot.json").write_text("{}", encoding="utf-8")
            os.environ["TMF_BENCH_DIR"] = str(empty_dir)
            missing = client.get("/api/v1/bench/pk")
            missing_body = missing.json()
            check(
                "api: 缺失产物（仅有非 bench 文件）-> 404 BENCH_ARTIFACT_NOT_FOUND",
                missing.status_code == 404
                and error_code(missing_body) == "BENCH_ARTIFACT_NOT_FOUND"
                and isinstance(missing_body.get("request_id"), str),
                f"{missing.status_code}/{error_code(missing_body)}",
            )

            # 非法产物（meta 不完整）-> 503
            invalid_dir = tmp / "invalid_bench"
            invalid_dir.mkdir()
            (invalid_dir / "pk_2020-01-01.json").write_text(
                json.dumps({"meta": {}, "dataset": {}, "models": [], "derived": [], "missing_models": []}),
                encoding="utf-8",
            )
            os.environ["TMF_BENCH_DIR"] = str(invalid_dir)
            invalid = client.get("/api/v1/bench/pk")
            invalid_body = invalid.json()
            check(
                "api: 非法产物 -> 503 BENCH_ARTIFACT_INVALID（不泄露堆栈）",
                invalid.status_code == 503
                and error_code(invalid_body) == "BENCH_ARTIFACT_INVALID"
                and "Traceback" not in invalid.text,
                f"{invalid.status_code}/{error_code(invalid_body)}",
            )

            # 坏 JSON -> 503
            broken_dir = tmp / "broken_bench"
            broken_dir.mkdir()
            (broken_dir / "pk_2020-01-02.json").write_text("{not-json", encoding="utf-8")
            os.environ["TMF_BENCH_DIR"] = str(broken_dir)
            broken = client.get("/api/v1/bench/pk")
            check(
                "api: 坏 JSON -> 503 BENCH_ARTIFACT_INVALID",
                broken.status_code == 503
                and error_code(broken.json()) == "BENCH_ARTIFACT_INVALID",
                f"{broken.status_code}/{error_code(broken.json())}",
            )
    finally:
        os.environ.pop("TMF_BENCH_DIR", None)
        os.environ.pop("TMF_DB_PATH", None)
        tmp_context.cleanup()

    default_db_after = (
        (DEFAULT_DB.stat().st_mtime_ns, DEFAULT_DB.stat().st_size)
        if DEFAULT_DB.exists()
        else None
    )
    check(
        "api: 未写入默认 serving/run/tmf.db",
        default_db_after == default_db_before,
        f"before={default_db_before} after={default_db_after}",
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="B8 离线基准产物与 PK API 验收")
    parser.add_argument("--artifact", default=None, help="指定产物路径（默认最新 pk_*.json）")
    args = parser.parse_args(argv)

    if args.artifact:
        artifact_path = Path(args.artifact)
        if not artifact_path.is_absolute():
            artifact_path = PROJECT_ROOT / artifact_path
    else:
        artifact_path = find_latest_artifact(BENCH_DIR)
        if artifact_path is None:
            print(f"FAIL artifact: 未找到 {relpath(BENCH_DIR)}/pk_*.json")
            print("RESULT FAIL (1 failed / 1 checks)")
            return 1

    print(f"[verify-bench] artifact: {relpath(artifact_path)}")
    try:
        document = artifact_checks(artifact_path)
        model_checks(document)
        derived_checks(document)
        reproduction_checks(document)
    except Exception as exc:
        check(
            "artifact/models/derived/reproduction: section 执行无异常",
            False,
            f"{type(exc).__name__}: {exc}",
        )
        traceback.print_exc()
        print(f"RESULT FAIL ({len(FAILURES)} failed / {TOTAL} checks)")
        return 1

    try:
        api_checks(document, artifact_path)
    except Exception as exc:
        check("api: section 执行无异常", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()

    if FAILURES:
        print(f"RESULT FAIL ({len(FAILURES)} failed / {TOTAL} checks)")
        for name in FAILURES:
            print(f"  FAILED: {name}")
        return 1
    print(f"RESULT PASS ({TOTAL} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
