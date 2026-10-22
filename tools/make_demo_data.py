#!/usr/bin/env python
"""D2：生成固定演示数据与 ``frontend/public/snapshot.json``（可重复、只读引用 B8/B11）。

产出（不启动任何服务、不加载模型、不调用 LLM、不写运行时统计）：

1. ``artifacts/demo/demo_cases_2026-10-22.json``
   固定 **8 条演示标题**：正常 ×3、非新闻 ×1、易混 ×1、可纠错 ×1、
   慢响应 ×1、超长 ×1。每条含
   ``case_id / title / category / expected_state / expected_pred / reason / source``：
   - 正常/易混/可纠错：从 B8 ``test.txt`` 逐条预测中间文件选**真实有预测**的标题，
     引用 ``line_index / true_label / 4 模型 preds``；其中 4 条复用 B11 快照的
     cached cases（sports/finance/education/entertainment），另加 1 条 B8 共识
     正确案例（game）；构建时逐条核对，漂移立即失败；
   - 非新闻：来自 B3 合成负样本（synthetic，明确不是真实用户/test 数据），
     预期「软拒识或无实时结果」，**不编造模型分数**；
   - 慢响应：``mock_behavior="slow"``、预期状态 ``mock_slow``，只用 mock worker
     演示，**不伪造真实模型延迟**；
   - 超长：>200 字符，预期 ``too_long_rejected``（前端拦截/后端 400），无模型预测。
2. ``artifacts/demo/arena_questions_2026-10-22.json``
   20 道竞技场题：来自 ``test.txt``，保留真实 ``line_index / title / true_label``；
   标注 ``source=test.txt, purpose=demo``，不与运行时反馈/指标混用。
3. ``frontend/public/snapshot.json``（F15 离线兜底构建产物）
   8 条 cases（结构沿用 B11 snapshot，扩展 expected 元数据），
   ``data_source=cached``、B11/B8 引用、LLM missing、
   mock/cached/offline 分块标注；**不覆盖** B11 的
   ``artifacts/demo/snapshot_2026-10-22.json``，也不含任何实时/mock 推理结果。

校验：写入前用 B13 ``tools/validate_meta.py`` 校验 snapshot 的 §6.8 meta
（``data_kind=demo``），并做「8 条覆盖 / 预测与 B8 一致 / LLM 无预测」自检。

用法（工作目录=工程根）::

    D:\\Miniconda3\\envs\\py3109\\python.exe tools/make_demo_data.py
    ... tools/make_demo_data.py --date 2026-10-22
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import random
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "00-data" / "data"
CLASS_FILE = DATA_DIR / "class.txt"
TEST_FILE = DATA_DIR / "test.txt"
DEMO_DIR = PROJECT_ROOT / "artifacts" / "demo"
BENCH_DIR = PROJECT_ROOT / "artifacts" / "bench"
B11_SNAPSHOT = DEMO_DIR / "snapshot_2026-10-22.json"
PUBLIC_SNAPSHOT = PROJECT_ROOT / "frontend" / "public" / "snapshot.json"

LOCAL_MODELS = ("rf", "fasttext", "bert", "distill")
LLM_MODEL_ID = "llm"
LLM_MISSING_REASON = "external_provider_not_authorized"

try:  # Windows 控制台统一 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass


class DemoDataError(RuntimeError):
    """演示数据构建失败（产物漂移、引用缺失、校验失败等）。"""


# ---------------------------------------------------------------------------
# 固定 8 条演示标题定义（title/line_index/true_label 均逐条核对 B8/B11）
# ---------------------------------------------------------------------------
CACHED_CASE_DEFS: tuple[dict[str, Any], ...] = (
    {
        "case_id": "demo-normal-sports-consensus",
        "category": "normal",
        "expected_state": "classified_ok",
        "line_index": 8000,
        "title": "里皮回避卸任问题谈世界杯卫冕：只靠年轻人是不够的",
        "true_label": "sports",
        "b11_case_id": "demo-sports-consensus",
        "reason": "B8 test 真实缓存预测：4 个本地模型一致且与真实标签相同（正常样本）。",
    },
    {
        "case_id": "demo-normal-finance-consensus",
        "category": "normal",
        "expected_state": "classified_ok",
        "line_index": 2000,
        "title": "泰达荷银基金：管理层维护市场稳定信心坚决",
        "true_label": "finance",
        "b11_case_id": "demo-finance-consensus",
        "reason": "B8 test 真实缓存预测：4 个本地模型一致且与真实标签相同（正常样本）。",
    },
    {
        "case_id": "demo-normal-game-consensus",
        "category": "normal",
        "expected_state": "classified_ok",
        "line_index": 3000,
        "title": "开心OL媒体礼包开送 好礼十天领不停",
        "true_label": "game",
        "b11_case_id": None,
        "reason": "B8 test 真实缓存预测：4 个本地模型一致且与真实标签相同（正常样本）。",
    },
    {
        "case_id": "demo-confusing-education-disagree",
        "category": "confusing",
        "expected_state": "classified_confusing",
        "line_index": 592,
        "title": "野鸡大学调查：国外半小时注册 国内无周密审查",
        "true_label": "education",
        "b11_case_id": "demo-education-disagree",
        "reason": (
            "易混：4 个本地模型分歧（rf=finance、fasttext=society、"
            "bert/distill=education），用于展示模型差异；不据此调参或改口径。"
        ),
    },
    {
        "case_id": "demo-correctable-entertainment-disagree",
        "category": "correctable",
        "expected_state": "classified_correctable",
        "line_index": 1229,
        "title": "电影票房今日冲刺100亿 细数百亿背后的痒与痛",
        "true_label": "entertainment",
        "b11_case_id": "demo-entertainment-disagree",
        "reason": (
            "可纠错：rf=stocks、fasttext=realty 预测错误，bert/distill=entertainment；"
            "演示可对错误模型提交纠正反馈（status=pending），不得宣称已学习/已重训。"
        ),
    },
)

#: 每条 cached case 的 4 模型预期（与 B8 中间预测文件逐条核对，漂移即失败）。
CACHED_EXPECTED: dict[str, dict[str, str]] = {
    "demo-normal-sports-consensus": {
        "rf": "sports",
        "fasttext": "sports",
        "bert": "sports",
        "distill": "sports",
    },
    "demo-normal-finance-consensus": {
        "rf": "finance",
        "fasttext": "finance",
        "bert": "finance",
        "distill": "finance",
    },
    "demo-normal-game-consensus": {
        "rf": "game",
        "fasttext": "game",
        "bert": "game",
        "distill": "game",
    },
    "demo-confusing-education-disagree": {
        "rf": "finance",
        "fasttext": "society",
        "bert": "education",
        "distill": "education",
    },
    "demo-correctable-entertainment-disagree": {
        "rf": "stocks",
        "fasttext": "realty",
        "bert": "entertainment",
        "distill": "entertainment",
    },
}

#: 非新闻：B3 合成负样本（第 1 行，chitchat）；非真实用户/test 数据。
NON_NEWS_TITLE = "今天有点困，哈哈"
NON_NEWS_REASON = (
    "非新闻：B3 合成负样本（chitchat，模板生成、非真实用户数据）；"
    "预期软拒识（rejected=true，仍返回 pred_class）或无实时结果；"
    "本演示不编造模型分数，也不把合成负样本当成 test/用户数据。"
)

#: 慢响应：只用 mock worker 演示，不伪造真实模型延迟。
SLOW_TITLE = "【演示】慢响应：mock worker 延迟约 3 秒返回"
SLOW_REASON = (
    "慢响应：仅由 mock worker 的 mock_behavior=slow 演示约 3 秒延迟；"
    "真实 live 模型不注入延迟，快照中也没有任何 mock 推理结果。"
)

#: 超长：>200 字符，前端拦截或后端 400 INVALID_TITLE，无模型预测。
LONG_TITLE = "超长标题演示：" + "这是一段用于验证前端长度提示与后端 400 拦截的演示文本。" * 12
LONG_REASON = (
    f"超长：标题长度 {len(LONG_TITLE)} > 200 字符（TMF_MAX_TITLE_CHARS=200）；"
    "预期前端拦截或后端 400 INVALID_TITLE，不进入模型推理，无预测结果。"
)


# ---------------------------------------------------------------------------
# 读取/校验工具
# ---------------------------------------------------------------------------
def relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> Any:
    if not path.is_file():
        raise DemoDataError(f"文件不存在: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DemoDataError(f"JSON 解析失败: {path}: {exc}") from exc


def load_validator() -> Any:
    path = PROJECT_ROOT / "tools" / "validate_meta.py"
    spec = importlib.util.spec_from_file_location("_tmf_validate_meta_d2", path)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise DemoDataError(f"无法加载 meta 校验器: {path}")
    module = importlib.util.module_from_spec(spec)
    # validate_meta 使用 dataclass，需先把模块注册进 sys.modules。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def load_classes() -> list[str]:
    classes = [line.strip() for line in CLASS_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]
    if len(classes) != 10 or len(set(classes)) != 10:
        raise DemoDataError(f"class.txt 类别数异常: {len(classes)}")
    return classes


def load_test_rows() -> list[tuple[str, int]]:
    rows: list[tuple[str, int]] = []
    for line_no, line in enumerate(TEST_FILE.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        title, label = line.rsplit("\t", 1)
        try:
            label_index = int(label)
        except ValueError as exc:
            raise DemoDataError(f"test.txt 第 {line_no} 行标签不是整数: {label!r}") from exc
        rows.append((title, label_index))
    if len(rows) != 10000:
        raise DemoDataError(f"test.txt 行数异常: {len(rows)}")
    return rows


def load_predictions() -> dict[str, list[dict[str, Any]]]:
    predictions: dict[str, list[dict[str, Any]]] = {}
    for model_id in LOCAL_MODELS:
        path = BENCH_DIR / f"_pred_{model_id}_test.jsonl"
        if not path.is_file():
            raise DemoDataError(f"缺少 B8 逐条预测文件: {relative(path)}")
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if len(records) != 10000:
            raise DemoDataError(f"B8 预测行数异常: {relative(path)} -> {len(records)}")
        predictions[model_id] = records
    return predictions


def latest_bench_artifact() -> Path:
    candidates = [
        path
        for path in BENCH_DIR.glob("pk_*.json")
        if path.is_file() and not path.is_symlink()
    ]
    if not candidates:
        raise DemoDataError(f"B8 离线产物不存在: {relative(BENCH_DIR)}/pk_*.json")
    return max(candidates, key=lambda path: path.stat().st_mtime)


# ---------------------------------------------------------------------------
# 1. 8 条固定演示标题
# ---------------------------------------------------------------------------
def build_cached_case(
    definition: dict[str, Any],
    test_rows: Sequence[tuple[str, int]],
    classes: Sequence[str],
    predictions: dict[str, list[dict[str, Any]]],
    bench_versions: dict[str, str],
    b11_cases: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    index = int(definition["line_index"])
    title, label_index = test_rows[index]
    true_label = definition["true_label"]
    if title != definition["title"]:
        raise DemoDataError(
            f"标题漂移: {definition['case_id']} test[{index}]={title!r}"
        )
    if classes[label_index] != true_label:
        raise DemoDataError(
            f"真值漂移: {definition['case_id']} {classes[label_index]!r} != {true_label!r}"
        )

    expected = CACHED_EXPECTED[definition["case_id"]]
    results: list[dict[str, Any]] = []
    for model_id in LOCAL_MODELS:
        record = predictions[model_id][index]
        if record.get("true_label") != true_label or record.get("pred_class") != expected[model_id]:
            raise DemoDataError(
                f"B8 预测漂移: {definition['case_id']} ({model_id}) "
                f"{record.get('pred_class')!r} != {expected[model_id]!r}"
            )
        results.append(
            {
                "model_id": model_id,
                "status": "ok",
                "pred_class": record["pred_class"],
                "model_version": bench_versions.get(model_id),
                "offline_latency_ms": record.get("latency_ms"),
                "data_source": "cached",
                "provenance": {
                    "prediction_file": f"artifacts/bench/_pred_{model_id}_test.jsonl",
                    "split": "test",
                    "line_index": index,
                },
            }
        )

    # 与 B11 快照交叉核对（复用其 4 条 cached cases，B11 只读）。
    b11_case_id = definition.get("b11_case_id")
    if b11_case_id:
        b11_case = b11_cases.get(b11_case_id)
        if b11_case is None:
            raise DemoDataError(f"B11 快照缺少 case: {b11_case_id}")
        if b11_case.get("line_index") != index or b11_case.get("title") != title:
            raise DemoDataError(f"B11 快照与 B8 不一致: {b11_case_id}")
        b11_preds = {
            item.get("model_id"): item.get("pred_class")
            for item in b11_case.get("results", [])
            if isinstance(item, dict)
        }
        if b11_preds != expected:
            raise DemoDataError(f"B11 快照预测与 B8 不一致: {b11_case_id} {b11_preds}")

    predictions_list = [item["pred_class"] for item in results]
    majority = max(set(predictions_list), key=predictions_list.count)
    return {
        "case_id": definition["case_id"],
        "title": title,
        "category": definition["category"],
        "expected_state": definition["expected_state"],
        "expected_pred": majority,
        "reason": definition["reason"],
        "source": (
            f"00-data/data/test.txt line_index={index}；"
            "artifacts/bench/_pred_<model>_test.jsonl（B8）；"
            + ("artifacts/demo/snapshot_2026-10-22.json（B11，只读交叉核对）"
               if b11_case_id else "B8 共识正确案例")
        ),
        "data_source": "cached",
        "split": "test",
        "line_index": index,
        "true_label": true_label,
        "true_label_index": label_index,
        "provenance": {
            "dataset": "00-data/data/test.txt",
            "split": "test",
            "line_index": index,
            "true_label": true_label,
            "prediction_files": {
                model_id: f"artifacts/bench/_pred_{model_id}_test.jsonl"
                for model_id in LOCAL_MODELS
            },
            "b11_case_id": b11_case_id,
        },
        "expected_model_preds": expected,
        "expected_correction": (
            {
                "corrected_class": "entertainment",
                "models_to_correct": ["rf", "fasttext"],
                "status": "pending",
                "note": "仅演示提交纠正反馈；记录为 pending，不训练、不修改预测、不宣称已学习。",
            }
            if definition["category"] == "correctable"
            else None
        ),
        "mock_behavior": None,
        "results": results,
        "missing_models": [LLM_MODEL_ID],
        "summary": {
            "consensus": len(set(predictions_list)) == 1,
            "majority_pred_class": majority,
            "correct_models": [
                model_id
                for model_id, pred in zip(LOCAL_MODELS, predictions_list)
                if pred == true_label
            ],
        },
        "has_cached_results": True,
        "note": (
            "预计算（离线 test 集）演示响应：不是实时推理；四模型结果来自 B8 逐条预测"
            "中间文件，LLM 未授权不生成任何结果。"
        ),
    }


def build_no_prediction_cases(negatives: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    first = negatives[0] if negatives else {}
    negative_title = str(first.get("text") or NON_NEWS_TITLE)
    negative_meta = first.get("meta") if isinstance(first.get("meta"), dict) else {}
    negative_kind = str(first.get("kind") or "chitchat")
    if negative_title != NON_NEWS_TITLE:
        # 允许 B3 产物内容演进，但必须在产物里如实记录实际标题/kind。
        pass

    return [
        {
            "case_id": "demo-non-news-synthetic-negative",
            "title": negative_title,
            "category": "non_news",
            "expected_state": "soft_rejected_or_no_realtime",
            "expected_pred": None,
            "reason": NON_NEWS_REASON,
            "source": (
                "artifacts/b3/synthetic_negatives.jsonl 第 1 行（B3 合成负样本，"
                "synthetic=true，非真实用户/test 数据）"
            ),
            "data_source": "unknown",
            "split": None,
            "line_index": None,
            "true_label": None,
            "true_label_index": None,
            "provenance": {
                "artifact": "artifacts/b3/synthetic_negatives.jsonl",
                "line": 1,
                "kind": negative_kind,
                "synthetic": True,
                "generator": negative_meta.get("generator"),
                "template_id": negative_meta.get("template_id"),
                "note": "合成负样本；不冒充真实 test/用户数据",
            },
            "expected_model_preds": None,
            "expected_correction": None,
            "mock_behavior": None,
            "results": [],
            "missing_models": [LLM_MODEL_ID],
            "summary": None,
            "has_cached_results": False,
            "note": "演示预期行为（非缓存预测）：非新闻标题应触发软拒识或无实时结果；不编造模型分数。",
        },
        {
            "case_id": "demo-slow-response-mock",
            "title": SLOW_TITLE,
            "category": "slow_response",
            "expected_state": "mock_slow",
            "expected_pred": None,
            "reason": SLOW_REASON,
            "source": "mock worker（仅演示行为；synthetic_demo，非真实用户/test 数据）",
            "data_source": "mock",
            "split": None,
            "line_index": None,
            "true_label": None,
            "true_label_index": None,
            "provenance": {
                "component": "serving/scripts/mock_worker.py --display-name mock-llm",
                "mock_behavior": "slow",
                "synthetic": True,
            },
            "expected_model_preds": None,
            "expected_correction": None,
            "mock_behavior": "slow",
            "results": [],
            "missing_models": [LLM_MODEL_ID],
            "summary": None,
            "has_cached_results": False,
            "note": "需要 mock worker 演示慢响应；快照不包含 mock 推理结果，也不伪造真实模型延迟。",
        },
        {
            "case_id": "demo-too-long-title",
            "title": LONG_TITLE,
            "category": "too_long",
            "expected_state": "too_long_rejected",
            "expected_pred": None,
            "reason": LONG_REASON,
            "source": "synthetic_demo（D2 生成；非真实用户/test 数据）",
            "data_source": "unknown",
            "split": None,
            "line_index": None,
            "true_label": None,
            "true_label_index": None,
            "provenance": {
                "generator": "tools/make_demo_data.py",
                "length_chars": len(LONG_TITLE),
                "max_title_chars": 200,
                "synthetic": True,
            },
            "expected_model_preds": None,
            "expected_correction": None,
            "mock_behavior": None,
            "results": [],
            "missing_models": [LLM_MODEL_ID],
            "summary": None,
            "has_cached_results": False,
            "note": "超长标题不进入模型推理：前端应拦截/提示，后端应返回 400 INVALID_TITLE。",
        },
    ]


def build_demo_cases(
    test_rows: Sequence[tuple[str, int]],
    classes: Sequence[str],
    predictions: dict[str, list[dict[str, Any]]],
    bench_versions: dict[str, str],
    b11_cases: dict[str, dict[str, Any]],
    negatives: Sequence[dict[str, Any]],
    generated_at: str,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cached_cases = [
        build_cached_case(
            definition, test_rows, classes, predictions, bench_versions, b11_cases
        )
        for definition in CACHED_CASE_DEFS
    ]
    no_pred_cases = build_no_prediction_cases(negatives)
    all_cases = cached_cases + no_pred_cases

    categories = [case["category"] for case in all_cases]
    expected_counts = {
        "normal": 3,
        "non_news": 1,
        "confusing": 1,
        "correctable": 1,
        "slow_response": 1,
        "too_long": 1,
    }
    actual_counts = {name: categories.count(name) for name in expected_counts}
    if actual_counts != expected_counts:
        raise DemoDataError(f"8 条演示标题分类不匹配: {actual_counts} != {expected_counts}")
    if len(all_cases) != 8:
        raise DemoDataError(f"演示标题数量必须为 8，实际 {len(all_cases)}")

    for case in all_cases:
        required = (
            "case_id",
            "title",
            "category",
            "expected_state",
            "expected_pred",
            "reason",
            "source",
        )
        missing = [field for field in required if field not in case]
        if missing:
            raise DemoDataError(f"{case.get('case_id')} 缺少字段: {missing}")
        if case["category"] in {"normal", "confusing", "correctable"}:
            if not case.get("results") or len(case["results"]) != 4:
                raise DemoDataError(f"cached case 必须有 4 个模型预测: {case['case_id']}")
        else:
            if case.get("results"):
                raise DemoDataError(f"无预测 case 不得包含模型结果: {case['case_id']}")
            if case.get("expected_pred") is not None:
                raise DemoDataError(f"无预测 case 的 expected_pred 必须为 null: {case['case_id']}")

    document = {
        "schema_version": 1,
        "generated_by": "tools/make_demo_data.py",
        "generated_at": generated_at,
        "source": "D2 演示数据（固定 8 条）",
        "purpose": "demo",
        "note": (
            "固定演示标题：正常/易混/可纠错引用 B8 test 真实缓存预测；"
            "非新闻来自 B3 合成负样本；慢响应用 mock 行为；超长不进入推理。"
            "本文件仅用于演示，不写入运行时统计、反馈或离线指标。"
        ),
        "summary": {
            "total": len(all_cases),
            "by_category": actual_counts,
            "with_model_predictions": sum(1 for case in all_cases if case["has_cached_results"]),
            "without_model_predictions": [
                case["case_id"] for case in all_cases if not case["has_cached_results"]
            ],
            "llm_missing": LLM_MODEL_ID,
        },
        "cases": all_cases,
    }
    return all_cases, document


# ---------------------------------------------------------------------------
# 2. 20 道竞技场题（来自 test.txt，固定种子分层抽样）
# ---------------------------------------------------------------------------
def select_arena_questions(
    test_rows: Sequence[tuple[str, int]],
    classes: Sequence[str],
    seed: int = 20261022,
    per_class: int = 2,
) -> list[dict[str, Any]]:
    pools: dict[str, list[int]] = {name: [] for name in classes}
    for index, (_, label_index) in enumerate(test_rows):
        pools[classes[label_index]].append(index)

    rng = random.Random(seed)
    picked: list[int] = []
    for name in classes:
        pool = pools[name]
        if len(pool) < per_class:
            raise DemoDataError(f"类别 {name} 可用题目不足: {len(pool)}")
        picked.extend(rng.sample(pool, per_class))
    picked.sort()

    questions: list[dict[str, Any]] = []
    for order, index in enumerate(picked, start=1):
        title, label_index = test_rows[index]
        questions.append(
            {
                "question_id": f"arena-demo-{order:03d}",
                "order": order,
                "line_index": index,
                "title": title,
                "true_label": classes[label_index],
                "true_label_index": label_index,
            }
        )
    if len(questions) != 20:
        raise DemoDataError(f"竞技场题数量必须为 20，实际 {len(questions)}")
    return questions


def build_arena_document(
    questions: Sequence[dict[str, Any]], generated_at: str
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "generated_by": "tools/make_demo_data.py",
        "generated_at": generated_at,
        "source": "00-data/data/test.txt",
        "purpose": "demo",
        "dataset": "00-data/data/test.txt",
        "split": "test",
        "selection": {
            "method": "stratified_by_true_label_per_class",
            "per_class": 2,
            "sample_size": 20,
            "seed": 20261022,
            "order": "line_index_ascending",
        },
        "note": (
            "20 道竞技场预置题：来自 test.txt，保留真实 label 与 line_index；"
            "仅用于演示/预置与人工核对，不写入运行时反馈、投票统计或离线指标，"
            "也不作为考试/榜单依据。"
        ),
        "questions": list(questions),
    }


# ---------------------------------------------------------------------------
# 3. frontend/public/snapshot.json（8 cases，cached/offline/mock 分块标注）
# ---------------------------------------------------------------------------
def build_public_snapshot(
    demo_cases: Sequence[dict[str, Any]],
    demo_cases_path: Path,
    arena_path: Path,
    bench_artifact: Path,
    bench_document: dict[str, Any],
    b11_snapshot: dict[str, Any],
    b11_path: Path,
    generated_at: str,
) -> dict[str, Any]:
    bench_meta = bench_document.get("meta") or {}
    if not isinstance(bench_meta, dict):
        raise DemoDataError("B8 产物缺少 meta")
    b11_benchmark = b11_snapshot.get("benchmark")
    if not isinstance(b11_benchmark, dict):
        raise DemoDataError("B11 快照缺少 benchmark 块")
    if b11_benchmark.get("artifact_sha256") != sha256(bench_artifact):
        raise DemoDataError(
            "B11 快照引用的 B8 产物 sha256 与当前 B8 产物不一致（B11 可能过期）"
        )
    if b11_benchmark.get("artifact_path") != relative(bench_artifact):
        raise DemoDataError("B11 快照引用的 B8 产物路径与当前产物不一致")

    model_versions = bench_meta.get("model_versions") or {}
    if not isinstance(model_versions, dict) or not model_versions:
        raise DemoDataError("B8 meta.model_versions 缺失")

    # 保持与 B11 一致的 snapshot 结构；cases 扩展为 8 条，并保留其 cached 结果结构。
    titles: list[dict[str, Any]] = []
    for case in demo_cases:
        title = {
            "case_id": case["case_id"],
            "category": case["category"],
            "expected_state": case["expected_state"],
            "expected_pred": case["expected_pred"],
            "reason": case["reason"],
            "source": case["source"],
            "data_source": case["data_source"],
            "title": case["title"],
            "has_cached_results": case["has_cached_results"],
            "missing_models": list(case["missing_models"]),
            "expected_model_preds": case["expected_model_preds"],
            "expected_correction": case["expected_correction"],
            "mock_behavior": case["mock_behavior"],
            "results": list(case["results"]),
            "summary": case["summary"],
            "note": case["note"],
        }
        if case["has_cached_results"]:
            title["split"] = "test"
            title["line_index"] = case["line_index"]
            title["true_label"] = case["true_label"]
            title["provenance"] = case["provenance"]
        else:
            title["provenance"] = case["provenance"]
        titles.append(title)

    category_counts = {
        "normal": sum(1 for case in demo_cases if case["category"] == "normal"),
        "non_news": sum(1 for case in demo_cases if case["category"] == "non_news"),
        "confusing": sum(1 for case in demo_cases if case["category"] == "confusing"),
        "correctable": sum(1 for case in demo_cases if case["category"] == "correctable"),
        "slow_response": sum(1 for case in demo_cases if case["category"] == "slow_response"),
        "too_long": sum(1 for case in demo_cases if case["category"] == "too_long"),
    }

    snapshot: dict[str, Any] = {
        "schema_version": 1,
        "generated_by": "tools/make_demo_data.py（D2；扩展 B11 snapshot 结构）",
        "generated_at": generated_at,
        "data_source": "cached",
        "meta": {
            "dataset": bench_meta.get("dataset") or "00-data/data/test.txt",
            "split": "test",
            "sample_size": int(bench_meta.get("sample_size") or 10000),
            "generated_at": generated_at,
            "code_version": "uncommitted",
            "model_versions": {str(key): str(value) for key, value in model_versions.items()},
            "metric_def": {
                "purpose": "缓存演示标题的预计算分类结果；本快照不产生新的模型指标",
                "metrics_source": (
                    f"{relative(bench_artifact)}（B8 离线产物，数值不重算）"
                ),
                "expected_states": (
                    "classified_ok=正常缓存分类；classified_confusing=模型分歧；"
                    "classified_correctable=可提交纠正反馈（pending）；"
                    "soft_rejected_or_no_realtime=非新闻软拒识/无实时结果；"
                    "mock_slow=mock worker 慢响应；too_long_rejected=超长拦截/400"
                ),
                "data_source": "cached=离线快照；mock=需 mock worker 演示；unknown=无缓存预测",
            },
            "data_kind": "demo",
        },
        "source_contract": {
            "image_language_note": "快照不伪称实时推理",
            "bench_artifact": relative(bench_artifact),
            "bench_artifact_sha256": sha256(bench_artifact),
            "b11_snapshot": relative(b11_path),
            "b11_snapshot_sha256": sha256(b11_path),
            "prediction_files": [
                f"artifacts/bench/_pred_{model_id}_test.jsonl" for model_id in LOCAL_MODELS
            ],
            "demo_cases_artifact": relative(demo_cases_path),
            "arena_questions_artifact": relative(arena_path),
            "b8_values_modified": False,
            "b11_snapshot_overwritten": False,
        },
        "benchmark": b11_benchmark,
        "demo": {
            "note": (
                "8 条固定演示标题：5 条引用 B8 test 真实缓存预测（cached），"
                "1 条 B3 合成负样本（软拒识预期），1 条 mock 慢响应行为，"
                "1 条超长标题拦截；页面必须标注 cached/非实时，不得宣称实时推理。"
            ),
            "title_count": len(titles),
            "category_counts": category_counts,
            "titles": titles,
            "expected_table": [
                {
                    "case_id": case["case_id"],
                    "category": case["category"],
                    "expected_state": case["expected_state"],
                    "expected_pred": case["expected_pred"],
                }
                for case in demo_cases
            ],
        },
        "llm": b11_snapshot.get("llm")
        or {
            "status": "missing",
            "reason": LLM_MISSING_REASON,
            "predictions": None,
            "scores": None,
            "data_source": None,
            "note": "LLM 未经授权：快照不生成 LLM 预测/分数，也不使用 mock 结果冒充 live 或 cached 预测。",
        },
        "model_coverage": b11_snapshot.get("model_coverage")
        or {
            "available": list(LOCAL_MODELS),
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
                "note": (
                    "快照只描述 mock 慢响应演示的预期行为（mock_behavior=slow），"
                    "不包含任何 mock 推理结果；实际 mock 演示需启动 mock_worker.py"
                ),
            },
            "cached": {
                "used": True,
                "note": "本文件即离线快照（data_source=cached），断网时只读展示",
            },
            "offline_benchmark": {
                "used": True,
                "data_kind": "offline",
                "artifact_path": relative(bench_artifact),
                "note": "B8 离线评估指标单独引用，不与运行时统计或演示缓存混合",
            },
            "synthetic": {
                "used": True,
                "note": "非新闻来自 B3 合成负样本；慢响应/超长为演示合成标题，均非真实用户数据",
            },
        },
        "demo_plan": {
            "live": ["demo-normal-*", "demo-confusing-*", "demo-correctable-*"],
            "mock": ["demo-slow-response-mock"],
            "frontend_intercept": ["demo-too-long-title"],
            "soft_reject": ["demo-non-news-synthetic-negative"],
            "note": "离线断网时 F15 只展示 cached 结果与 expected_table；不得把 demo 数据写入运行时统计。",
        },
        "runtime_stats": {
            "included": False,
            "note": "快照不包含 /stats 运行时统计；mock/cached 不写入统计基线、反馈或基准产物",
        },
    }
    return snapshot


# ---------------------------------------------------------------------------
# 写入与校验
# ---------------------------------------------------------------------------
def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def validate_snapshot(snapshot: dict[str, Any]) -> None:
    validator = load_validator()
    try:
        validator.validate_document(snapshot)
    except Exception as exc:
        raise DemoDataError(f"snapshot 未通过 B13 meta 校验: {exc}") from exc

    titles = snapshot["demo"]["titles"]
    if len(titles) != 8:
        raise DemoDataError(f"snapshot 必须含 8 条 cases，实际 {len(titles)}")
    for title in titles:
        if title["data_source"] == "cached":
            if not title["results"] or len(title["results"]) != 4:
                raise DemoDataError(f"cached case 缺少 4 模型结果: {title['case_id']}")
            if any(item.get("model_id") == LLM_MODEL_ID for item in title["results"]):
                raise DemoDataError("snapshot 中出现 LLM 预测结果")
            if not title.get("expected_model_preds"):
                raise DemoDataError(f"cached case 缺少 expected_model_preds: {title['case_id']}")
            if [item["pred_class"] for item in title["results"]] != [
                title["expected_model_preds"][model_id] for model_id in LOCAL_MODELS
            ]:
                raise DemoDataError(f"cached case 结果与 expected 不一致: {title['case_id']}")
        else:
            if title["results"]:
                raise DemoDataError(f"无缓存预测 case 不得包含 results: {title['case_id']}")
    if snapshot["llm"].get("status") != "missing":
        raise DemoDataError("snapshot 必须保留 LLM missing")
    if snapshot["llm"].get("predictions") is not None:
        raise DemoDataError("snapshot 不得包含 LLM 预测")
    if snapshot["data_source"] != "cached":
        raise DemoDataError("snapshot 顶层 data_source 必须为 cached")
    if snapshot.get("runtime_stats", {}).get("included") is not False:
        raise DemoDataError("snapshot 不得包含运行时统计")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="D2：生成固定 8 条演示标题、20 道竞技场题与 frontend/public/snapshot.json"
    )
    parser.add_argument(
        "--date",
        default=datetime.now().strftime("%Y-%m-%d"),
        help="产物日期后缀（默认今天）",
    )
    args = parser.parse_args(argv)
    date = args.date

    try:
        classes = load_classes()
        test_rows = load_test_rows()
        predictions = load_predictions()
        bench_artifact = latest_bench_artifact()
        bench_document = load_json(bench_artifact)
        bench_versions = {
            str(entry.get("model_id")): entry.get("model_version")
            for entry in bench_document.get("models", [])
            if isinstance(entry, dict) and entry.get("model_version")
        }
        b11_snapshot = load_json(B11_SNAPSHOT)
        b11_cases = {
            str(case.get("case_id")): case
            for case in (b11_snapshot.get("demo") or {}).get("titles", [])
            if isinstance(case, dict)
        }
        negatives = [
            json.loads(line)
            for line in (PROJECT_ROOT / "artifacts" / "b3" / "synthetic_negatives.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
            if line.strip()
        ]
        if not negatives:
            raise DemoDataError("B3 合成负样本为空")

        generated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        demo_cases, demo_document = build_demo_cases(
            test_rows, classes, predictions, bench_versions, b11_cases, negatives, generated_at
        )
        questions = select_arena_questions(test_rows, classes)
        arena_document = build_arena_document(questions, generated_at)

        demo_cases_path = DEMO_DIR / f"demo_cases_{date}.json"
        arena_path = DEMO_DIR / f"arena_questions_{date}.json"
        snapshot = build_public_snapshot(
            demo_cases,
            demo_cases_path,
            arena_path,
            bench_artifact,
            bench_document,
            b11_snapshot,
            B11_SNAPSHOT,
            generated_at,
        )
        validate_snapshot(snapshot)

        write_json(demo_cases_path, demo_document)
        write_json(arena_path, arena_document)
        write_json(PUBLIC_SNAPSHOT, snapshot)
    except DemoDataError as exc:
        print(f"DEMO_DATA_ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - IO 等非契约错误
        print(f"DEMO_DATA_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(
        "DEMO_DATA_OK "
        f"cases={len(demo_cases)} "
        f"categories={snapshot['demo']['category_counts']} "
        f"arena_questions={len(questions)} "
        f"snapshot={relative(PUBLIC_SNAPSHOT)} "
        f"llm={snapshot['llm']['status']}"
    )
    print(f"  demo_cases={relative(demo_cases_path)}")
    print(f"  arena={relative(arena_path)}")
    print(f"  meta_check=PASS data_kind={snapshot['meta']['data_kind']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
