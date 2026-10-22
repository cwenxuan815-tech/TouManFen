#!/usr/bin/env python
"""B11：生成演示快照 ``artifacts/demo/snapshot_<date>.json``（cached）。

设计约束（工作清单 §6.7 / B11）：
- **复用** B8 当前离线产物与 B8 的 test 集逐条预测中间文件，**不修改 B8 数值**；
- 固定演示标题的预计算响应：标题与每个模型的预期类别在**本工具中显式定义**，
  构建时逐条核对 B8 预测文件，不一致立即失败（避免产物漂移后静默变化）；
- 快照整体与每条结果都标 ``data_source=cached``；
- **LLM 必须保留 missing / 未授权**：不生成任何 LLM 预测、分数或 mock 冒充 live；
- ``live`` / ``mock`` / ``cached`` / ``offline_benchmark`` 分块标注，互不混合；
- 不写入运行时统计（``/stats``）、不写反馈 SQLite、不写基准产物；
- 写入前用 ``tools/validate_meta.py`` 校验 §6.8 ``meta``（``data_kind=demo``）。

用法（不启动任何服务、不加载模型）：
    python tools/make_demo_snapshot.py
    python tools/make_demo_snapshot.py --bench-artifact artifacts/bench/pk_2026-10-22.json
    python tools/make_demo_snapshot.py --out artifacts/demo/snapshot_2026-10-22.json
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BENCH_DIR = PROJECT_ROOT / "artifacts" / "bench"
DEFAULT_DEMO_DIR = PROJECT_ROOT / "artifacts" / "demo"

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class DemoCase:
    """一个固定演示标题及其预期（预期值用于核对 B8 预测产物，防止漂移）。"""

    case_id: str
    title: str
    true_label: str
    expected_predictions: dict[str, str]


#: 固定演示标题：来自 B8 的 test 集逐条预测中间文件（离线、非实时）。
#: 覆盖 4 个类别，含“4 模型一致”与“模型分歧”两类场景；LLM 永远 missing。
DEMO_CASES: tuple[DemoCase, ...] = (
    DemoCase(
        case_id="demo-sports-consensus",
        title="里皮回避卸任问题谈世界杯卫冕：只靠年轻人是不够的",
        true_label="sports",
        expected_predictions={
            "rf": "sports",
            "fasttext": "sports",
            "bert": "sports",
            "distill": "sports",
        },
    ),
    DemoCase(
        case_id="demo-finance-consensus",
        title="泰达荷银基金：管理层维护市场稳定信心坚决",
        true_label="finance",
        expected_predictions={
            "rf": "finance",
            "fasttext": "finance",
            "bert": "finance",
            "distill": "finance",
        },
    ),
    DemoCase(
        case_id="demo-education-disagree",
        title="野鸡大学调查：国外半小时注册 国内无周密审查",
        true_label="education",
        expected_predictions={
            "rf": "finance",
            "fasttext": "society",
            "bert": "education",
            "distill": "education",
        },
    ),
    DemoCase(
        case_id="demo-entertainment-disagree",
        title="电影票房今日冲刺100亿 细数百亿背后的痒与痛",
        true_label="entertainment",
        expected_predictions={
            "rf": "stocks",
            "fasttext": "realty",
            "bert": "entertainment",
            "distill": "entertainment",
        },
    ),
)

#: 快照只包含这 4 个本地模型；LLM 固定 missing（未授权）。
SNAPSHOT_MODELS = ("rf", "fasttext", "bert", "distill")
LLM_MODEL_ID = "llm"
LLM_MISSING_REASON = "external_provider_not_authorized"
LLM_MISSING_NOTE = (
    "LLM 未经授权：快照不生成 LLM 预测/分数，也不使用 mock 结果冒充 live 或 cached 预测。"
)


class SnapshotError(RuntimeError):
    """快照构建失败（产物漂移、元数据缺失、校验失败等）。"""


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.name


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SnapshotError(f"文件不存在: {path}") from exc
    except UnicodeDecodeError as exc:
        raise SnapshotError(f"文件不是 UTF-8: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SnapshotError(
            f"JSON 解析失败（line {exc.lineno}, column {exc.colno}）: {path}"
        ) from exc


def _load_validate_meta():
    """按路径加载 B13 的 tools/validate_meta.py（tools 不是 package）。"""
    path = PROJECT_ROOT / "tools" / "validate_meta.py"
    spec = importlib.util.spec_from_file_location("_tmf_validate_meta_demo", path)
    if spec is None or spec.loader is None:
        raise SnapshotError(f"无法加载 B13 校验模块: {path}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses 需要模块已注册在 sys.modules（validate_meta 用了 dataclass）。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _latest_bench_artifact(directory: Path) -> Path:
    candidates = [
        path
        for path in directory.glob("pk_*.json")
        if path.is_file() and not path.is_symlink()
    ]
    if not candidates:
        raise SnapshotError(f"B8 离线产物不存在（{directory}/pk_*.json）")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _copy_benchmark_summary(document: dict[str, Any], artifact_path: Path) -> dict[str, Any]:
    """原样复制 B8 指标数值（不做任何重算/取整），附可审计引用。"""
    meta = document.get("meta")
    if not isinstance(meta, dict):
        raise SnapshotError("B8 产物缺少 meta")
    missing_meta = [
        field
        for field in ("dataset", "split", "sample_size", "generated_at", "code_version")
        if field not in meta
    ]
    if missing_meta:
        raise SnapshotError(f"B8 meta 缺少字段: {missing_meta}")

    models: list[dict[str, Any]] = []
    for entry in document.get("models", []):
        if not isinstance(entry, dict):
            continue
        models.append(
            {
                "model_id": entry.get("model_id"),
                "display_name": entry.get("display_name"),
                "status": entry.get("status"),
                "model_version": entry.get("model_version"),
                "metrics": entry.get("metrics"),
                "weight": entry.get("weight"),
                "weight_source": entry.get("weight_source"),
                "reason": entry.get("reason"),
            }
        )
    if not models:
        raise SnapshotError("B8 产物 models 为空")

    derived: list[dict[str, Any]] = []
    for entry in document.get("derived", []):
        if not isinstance(entry, dict):
            continue
        derived.append(
            {
                "kind": entry.get("kind"),
                "display_name": entry.get("display_name"),
                "status": entry.get("status"),
                "metrics": entry.get("metrics"),
                "participating_models": entry.get("participating_models"),
                "note": entry.get("note"),
            }
        )

    return {
        "artifact_path": _relative(artifact_path),
        "artifact_sha256": _sha256(artifact_path),
        "artifact_generated_at": meta.get("generated_at"),
        "data_kind": meta.get("data_kind"),
        "dataset": meta.get("dataset"),
        "split": meta.get("split"),
        "sample_size": meta.get("sample_size"),
        "models": models,
        "derived": derived,
        "missing_models": list(document.get("missing_models") or []),
        "values_copied_verbatim": True,
        "note": "指标数值原样复制自 B8 离线产物（未重新计算、未取整）；本快照不是新的离线评估。",
    }


def _model_versions_from_bench(document: dict[str, Any]) -> dict[str, str]:
    meta = document.get("meta") or {}
    versions = meta.get("model_versions")
    if not isinstance(versions, dict) or not versions:
        raise SnapshotError("B8 meta.model_versions 缺失")
    cleaned: dict[str, str] = {}
    for model_id, version in versions.items():
        if not isinstance(model_id, str) or not isinstance(version, str) or not version:
            raise SnapshotError("B8 meta.model_versions 含非法条目")
        cleaned[model_id] = version
    return cleaned


def _bench_model_versions(document: dict[str, Any]) -> dict[str, str]:
    versions: dict[str, str] = {}
    for entry in document.get("models", []):
        if isinstance(entry, dict) and isinstance(entry.get("model_id"), str):
            version = entry.get("model_version")
            if isinstance(version, str) and version:
                versions[entry["model_id"]] = version
    return versions


def _load_predictions(prediction_dir: Path) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Path]]:
    """读取 B8 中间预测文件 ``_pred_<model>_test.jsonl``（只读）。"""
    rows: dict[str, list[dict[str, Any]]] = {}
    files: dict[str, Path] = {}
    for model_id in SNAPSHOT_MODELS:
        path = prediction_dir / f"_pred_{model_id}_test.jsonl"
        if not path.is_file():
            raise SnapshotError(
                f"缺少 B8 逐条预测文件: {_relative(path)}（请先运行 tools/bench_signature.py）"
            )
        parsed: list[dict[str, Any]] = []
        try:
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    if line.strip():
                        parsed.append(json.loads(line))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotError(f"预测文件不可读: {path}") from exc
        rows[model_id] = parsed
        files[model_id] = path
    return rows, files


def _find_row_index(rows: dict[str, list[dict[str, Any]]], case: DemoCase) -> int:
    """按标题定位行号；要求 4 个模型行号/标题/真值一致（产物漂移即失败）。"""
    indices: set[int] = set()
    for model_id, entries in rows.items():
        matches = [i for i, entry in enumerate(entries) if entry.get("title") == case.title]
        if not matches:
            raise SnapshotError(
                f"演示标题在 B8 预测文件中不存在: {case.case_id} ({model_id})"
            )
        if len(matches) > 1:
            raise SnapshotError(
                f"演示标题在 B8 预测文件中出现多次: {case.case_id} ({model_id})"
            )
        index = matches[0]
        if entries[index].get("true_label") != case.true_label:
            raise SnapshotError(
                f"演示标题真值漂移: {case.case_id} ({model_id}) "
                f"{entries[index].get('true_label')!r} != {case.true_label!r}"
            )
        indices.add(index)
    if len(indices) != 1:
        raise SnapshotError(f"演示标题在 4 个模型预测文件中行号不一致: {case.case_id} {indices}")
    return indices.pop()


def _build_demo_case(
    case: DemoCase,
    rows: dict[str, list[dict[str, Any]]],
    files: dict[str, Path],
    bench_versions: dict[str, str],
) -> dict[str, Any]:
    index = _find_row_index(rows, case)
    results: list[dict[str, Any]] = []
    for model_id in SNAPSHOT_MODELS:
        entry = rows[model_id][index]
        actual = entry.get("pred_class")
        expected = case.expected_predictions[model_id]
        if actual != expected:
            raise SnapshotError(
                f"演示标题预测漂移: {case.case_id} ({model_id}) "
                f"{actual!r} != {expected!r}（B8 产物可能已更新，请同步本工具预期）"
            )
        results.append(
            {
                "model_id": model_id,
                "status": "ok",
                "pred_class": actual,
                "model_version": bench_versions.get(model_id),
                "offline_latency_ms": entry.get("latency_ms"),
                "data_source": "cached",
                "provenance": {
                    "prediction_file": _relative(files[model_id]),
                    "split": entry.get("split"),
                    "line_index": index,
                },
            }
        )
    predictions = [item["pred_class"] for item in results]
    majority = max(set(predictions), key=predictions.count)
    return {
        "case_id": case.case_id,
        "title": case.title,
        "split": "test",
        "line_index": index,
        "true_label": case.true_label,
        "data_source": "cached",
        "results": results,
        "missing_models": [LLM_MODEL_ID],
        "summary": {
            "consensus": len(set(predictions)) == 1,
            "majority_pred_class": majority,
            "correct_models": [
                model_id
                for model_id, pred in zip(SNAPSHOT_MODELS, predictions)
                if pred == case.true_label
            ],
        },
        "note": (
            "预计算（离线 test 集）演示响应：不是实时推理；四模型结果来自 B8 逐条预测"
            "中间文件，LLM 未授权不生成任何结果。"
        ),
    }


def build_snapshot(bench_artifact: Path, generated_at: datetime | None = None) -> dict[str, Any]:
    """构建快照文档（纯函数式，不写文件）。"""
    if not bench_artifact.is_file():
        raise SnapshotError(f"B8 离线产物不存在: {bench_artifact}")
    document = _load_json(bench_artifact)
    if not isinstance(document, dict):
        raise SnapshotError("B8 产物顶层必须是 JSON 对象")
    bench_meta = document.get("meta")
    if not isinstance(bench_meta, dict):
        raise SnapshotError("B8 产物缺少 meta")

    prediction_dir = bench_artifact.parent
    rows, files = _load_predictions(prediction_dir)
    bench_versions = _bench_model_versions(document)
    model_versions = _model_versions_from_bench(document)
    benchmark = _copy_benchmark_summary(document, bench_artifact)

    missing_models = [
        model_id
        for model_id in (entry.get("model_id") for entry in document.get("models", []))
        if isinstance(model_id, str) and model_id not in SNAPSHOT_MODELS
    ]
    if LLM_MODEL_ID not in missing_models:
        raise SnapshotError("B8 产物未把 LLM 标记为 missing（契约要求 LLM 未授权）")
    if set(benchmark["missing_models"]) != {LLM_MODEL_ID}:
        raise SnapshotError(
            f"B8 产物 missing_models 预期仅为 llm，实际 {benchmark['missing_models']}"
        )

    timestamp = generated_at or datetime.now().astimezone()
    generated_iso = timestamp.replace(microsecond=0).isoformat()

    demo_cases = [
        _build_demo_case(case, rows, files, bench_versions) for case in DEMO_CASES
    ]
    # 防御：演示结果与 LLM 必须严格分离，禁止任何 LLM 预测/分数。
    for case in demo_cases:
        for result in case["results"]:
            if result["model_id"] == LLM_MODEL_ID:
                raise SnapshotError("演示结果中不得出现 LLM 预测")
            if result["data_source"] != "cached":
                raise SnapshotError("演示结果 data_source 必须为 cached")

    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "generated_by": "tools/make_demo_snapshot.py",
        "generated_at": generated_iso,
        "data_source": "cached",
        "meta": {
            "dataset": bench_meta.get("dataset"),
            "split": "test",
            "sample_size": bench_meta.get("sample_size"),
            "generated_at": generated_iso,
            "code_version": "uncommitted",
            "model_versions": model_versions,
            "metric_def": {
                "purpose": "缓存演示标题的预计算分类结果；本快照不产生新的模型指标",
                "metrics_source": f"{_relative(bench_artifact)}（B8 离线产物，数值不重算）",
            },
            "data_kind": "demo",
        },
        "source_contract": {
            "image_language_note": "快照不伪称实时推理",
            "bench_artifact": _relative(bench_artifact),
            "prediction_files": [
                _relative(path) for path in files.values()
            ],
            "b8_values_modified": False,
        },
        "benchmark": benchmark,
        "demo": {
            "note": (
                "预计算演示响应：来自 B8 test 集逐条预测产物（离线、非实时）；"
                "页面必须标注 cached，不得宣称实时推理。"
            ),
            "title_count": len(demo_cases),
            "titles": demo_cases,
        },
        "llm": {
            "status": "missing",
            "reason": LLM_MISSING_REASON,
            "predictions": None,
            "scores": None,
            "data_source": None,
            "note": LLM_MISSING_NOTE,
        },
        "model_coverage": {
            "available": list(SNAPSHOT_MODELS),
            "missing": [LLM_MODEL_ID],
            "missing_reason": {LLM_MODEL_ID: LLM_MISSING_REASON},
        },
        "sources": {
            "live": {
                "used": False,
                "note": "快照不包含任何实时推理结果（data_source=live 仅由运行中网关返回）",
            },
            "mock": {
                "used": False,
                "note": "快照不包含 mock worker 结果，也不把 mock 冒充 live/cached",
            },
            "cached": {
                "used": True,
                "note": "本文件即离线快照（data_source=cached），断网时只读展示",
            },
            "offline_benchmark": {
                "used": True,
                "data_kind": "offline",
                "artifact_path": _relative(bench_artifact),
                "note": "B8 离线评估指标单独引用，不与运行时统计或演示缓存混合",
            },
        },
        "runtime_stats": {
            "included": False,
            "note": "快照不包含 /stats 运行时统计；mock/cached 不写入统计基线、反馈或基准产物",
        },
    }

    # B13 §6.8：写入前严格校验 meta（data_kind 只能 offline|demo）。
    validate_meta = _load_validate_meta()
    try:
        validate_meta.validate_document(snapshot)
    except Exception as exc:  # MetaValidationError 或加载错误
        raise SnapshotError(f"快照未通过 B13 meta 校验: {exc}") from exc
    return snapshot


def write_snapshot(snapshot: dict[str, Any], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    out_path.write_text(text, encoding="utf-8")
    return out_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="生成 B11 演示快照（复用 B8 离线产物，data_source=cached）"
    )
    parser.add_argument(
        "--bench-artifact",
        default=None,
        help="B8 离线产物路径（默认取 artifacts/bench 下最新 pk_*.json）",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="输出路径（默认 artifacts/demo/snapshot_<date>.json）",
    )
    args = parser.parse_args(argv)

    try:
        if args.bench_artifact:
            bench_artifact = Path(args.bench_artifact)
            if not bench_artifact.is_absolute():
                bench_artifact = PROJECT_ROOT / bench_artifact
        else:
            bench_artifact = _latest_bench_artifact(DEFAULT_BENCH_DIR)

        snapshot = build_snapshot(bench_artifact)
        generated_at = datetime.fromisoformat(snapshot["generated_at"])
        if args.out:
            out_path = Path(args.out)
            if not out_path.is_absolute():
                out_path = PROJECT_ROOT / out_path
        else:
            out_path = DEFAULT_DEMO_DIR / f"snapshot_{generated_at.date().isoformat()}.json"
        write_snapshot(snapshot, out_path)
    except SnapshotError as exc:
        print(f"SNAPSHOT_ERROR: {exc}")
        return 2
    except Exception as exc:  # IO 等非契约错误
        print(f"SNAPSHOT_ERROR: {type(exc).__name__}: {exc}")
        return 1

    print(
        "SNAPSHOT_OK "
        f"path={_relative(out_path)} "
        f"cases={snapshot['demo']['title_count']} "
        f"data_source={snapshot['data_source']} "
        f"bench={snapshot['benchmark']['artifact_path']} "
        f"llm={snapshot['llm']['status']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
