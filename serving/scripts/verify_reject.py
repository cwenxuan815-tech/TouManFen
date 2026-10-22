#!/usr/bin/env python
"""B3 软拒识（负样本/阈值/字段）离线验收脚本。

原则：
- **不启动服务、不占用端口、不启动/重启 stage2 worker、不调用真实 LLM**；
- 只用本地 fastText 模型（02-fasttext/save_model/word_auto_train.pkl）在进程内推理，
  模拟“适配器已校验分数”的结果，喂给真实的 ClassificationService；
- API 段用 FastAPI TestClient + 临时 TMF_DB_PATH（**不触碰** serving/run/tmf.db）；
- 只读产物 artifacts/b3/*.json，不重新标定、不训练、不改配置。

覆盖（对应工作清单 §7 B3 验收）：
1. 配置/产物：models.yaml reject 严格校验、环境变量覆盖、ROC/阈值产物完整、合成负样本计数；
2. 「今天天气不错」（含长句）-> rejected=true，pred_class/分数保留；
3. 正常新闻（来自 dev.txt）-> rejected=false；
4. 10 正（dev，每个类别首条）+ 10 负（四类合成负样本）表驱动，逐项打印；
5. threshold_meta 必须含 source=synthetic_negatives / calibrated=false / 方法 / 阈值 / 适用范围；
6. 无分数 adapter（模拟 LLM）-> rejected=false、threshold_meta=null、不崩溃；
7. 配置阈值生效、可改（环境变量 / 内存配置）后结果改变；
8. 全量复算：2000 正 + 1200 负的拒识计数与 artifacts/b3/threshold_selection.json 一致；
9. /classify 响应字段形状（TestClient 映射层）。

输出：逐项 PASS/FAIL；全部通过时打印 RESULT PASS 并返回 0，否则 RESULT FAIL 返回 1。

运行（fastapi 来自仓库同级 venv-gateway，fasttext/jieba/numpy 来自本机 conda 环境）：
    $env:PYTHONPATH='E:\\AIModels\\test\\TouManFen\\venv-gateway\\Lib\\site-packages'
    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/verify_reject.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from serving.adapters.protocol import AdapterResult, ScoreEntry  # noqa: E402
from serving.gateway.errors import ConfigError  # noqa: E402
from serving.gateway.settings import load_settings  # noqa: E402
from serving.gateway.stats import StatsCollector  # noqa: E402
from serving.models.registry import ModelRegistry  # noqa: E402
from serving.services.classification import (  # noqa: E402
    ClassificationService,
    REJECT_REASON_HIGH_ENTROPY,
    REJECT_REASON_LOW_MAX_SCORE,
    shannon_entropy,
)

ARTIFACT_DIR = PROJECT_ROOT / "artifacts" / "b3"
NEGATIVES_PATH = ARTIFACT_DIR / "synthetic_negatives.jsonl"
ROC_PATH = ARTIFACT_DIR / "reject_roc.json"
SELECTION_PATH = ARTIFACT_DIR / "threshold_selection.json"
DEV_PATH = PROJECT_ROOT / "00-data" / "data" / "dev.txt"
CLASS_PATH = PROJECT_ROOT / "00-data" / "data" / "class.txt"
MODEL_PATH = PROJECT_ROOT / "02-fasttext" / "save_model" / "word_auto_train.pkl"

DEMO_TITLE = "今天天气不错，适合出去走走"
DEMO_TITLE_SHORT = "今天天气不错"
EXPECTED_SEED = 20261021
EXPECTED_POSITIVES = 2000
EXPECTED_NEGATIVES = 1200
NEGATIVE_KINDS = ("chitchat", "code", "product", "symbols")
META_REQUIRED = (
    "dataset",
    "split",
    "sample_size",
    "generated_at",
    "code_version",
    "model_versions",
    "metric_def",
    "data_kind",
)
#: F6 对接必须存在的 threshold_meta 键（此外允许辅助键）。
THRESHOLD_META_REQUIRED = (
    "source",
    "calibrated",
    "method",
    "max_score_threshold",
    "entropy_threshold",
    "scope_note",
    "sample_size",
)
REJECT_ENV_KEYS = (
    "TMF_REJECT_ENABLED",
    "TMF_REJECT_MAX_SCORE_THRESHOLD",
    "TMF_REJECT_ENTROPY_THRESHOLD",
    "TMF_DB_PATH",
)

FAILURES: list[str] = []

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover
        pass


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


def clear_reject_env() -> None:
    for key in REJECT_ENV_KEYS:
        os.environ.pop(key, None)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 本地 fastText 评分（与 worker 同预处理：jieba 分词 + k=类别数）
# ---------------------------------------------------------------------------
class LocalFasttextScorer:
    """独立实现的最小推理封装（不 import tools/eval_reject，避免复用被测逻辑）。"""

    def __init__(self, classes: list[str]) -> None:
        import fasttext
        import jieba

        if not MODEL_PATH.is_file():
            raise FileNotFoundError(MODEL_PATH)
        self._classes = list(classes)
        self._class_set = set(classes)
        self._jieba = jieba
        self._model = fasttext.load_model(str(MODEL_PATH))

    def score(self, title: str) -> dict[str, Any]:
        clean = " ".join(str(title).split())
        if not clean:
            raise ValueError("标题为空，无法评分")
        words = " ".join(self._jieba.lcut(clean))
        labels, probabilities = self._model.predict(words, k=len(self._classes))
        mapped: dict[str, float] = {}
        for label, probability in zip(labels, probabilities):
            class_id = label.replace("__label__", "")
            if class_id not in self._class_set or class_id in mapped:
                raise ValueError(f"fastText 返回非法/重复标签: {label!r}")
            mapped[class_id] = min(1.0, max(0.0, float(probability)))
        if set(mapped) != self._class_set:
            raise ValueError("fastText 未返回全部类别分数")
        values = [mapped[class_id] for class_id in self._classes]
        max_score = max(values)
        return {
            "max_score": max_score,
            "entropy": shannon_entropy(values),
            "pred_class": self._classes[max(range(len(values)), key=lambda i: values[i])],
            "scores": tuple(values),
        }


class LocalFasttextAdapter:
    """把本地推理结果包装成“已通过适配器校验”的 AdapterResult（不启动 worker）。"""

    def __init__(self, scorer: LocalFasttextScorer, classes: tuple[str, ...]) -> None:
        self.model_id = "fasttext"
        self.model_version: str | None = None
        self._scorer = scorer
        self._classes = tuple(classes)

    async def predict(self, title: str, request_id: str) -> AdapterResult:
        started = time.perf_counter()
        try:
            scored = self._scorer.score(title)
        except Exception as exc:  # pragma: no cover - 正常输入不会触发
            return AdapterResult(
                model_id=self.model_id,
                status="error",
                latency_ms=0,
                error_code="MODEL_ERROR",
                error_message=f"本地推理失败: {exc}",
            )
        latency_ms = int((time.perf_counter() - started) * 1000)
        entries = tuple(
            ScoreEntry(class_id=class_id, score=value)
            for class_id, value in zip(self._classes, scored["scores"])
        )
        top_k = tuple(
            sorted(entries, key=lambda entry: (-entry.score, entry.class_id))[:3]
        )
        return AdapterResult(
            model_id=self.model_id,
            status="ok",
            latency_ms=latency_ms,
            pred_class=scored["pred_class"],
            scores=entries,
            score_kind="raw_fasttext",
            top_k=top_k,
        )


class NoScoreAdapter:
    """模拟 LLM：成功返回类别但**不提供分数**。"""

    model_id = "llm"
    model_version: str | None = None

    async def predict(self, title: str, request_id: str) -> AdapterResult:
        return AdapterResult(
            model_id=self.model_id,
            status="ok",
            latency_ms=5,
            pred_class="finance",
        )


def make_service(
    settings,
    adapters: dict[str, Any],
    *,
    reject: Any = "default",
    registry: ModelRegistry | None = None,
) -> ClassificationService:
    return ClassificationService(
        registry=registry or ModelRegistry(settings),
        adapters=adapters,
        classes=settings.classes,
        max_title_chars=settings.max_title_chars,
        stats=StatsCollector(),
        logger=logging.getLogger("verify-b3"),
        reject=settings.reject if reject == "default" else reject,
    )


def classify(service: ClassificationService, title: str, model_id: str = "fasttext"):
    return asyncio.run(service.classify(title, model_id, "verify-b3"))


# ---------------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------------
def read_classes() -> list[str]:
    return [
        line.strip()
        for line in CLASS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def read_dev_sample(size: int = EXPECTED_POSITIVES) -> list[tuple[str, str]]:
    records: list[tuple[str, str]] = []
    with DEV_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            text, raw_label = line.split("\t")
            records.append((text, raw_label))
            if len(records) >= size:
                break
    if len(records) != size:
        raise SystemExit(f"ERROR: dev.txt 不足 {size} 条（实际 {len(records)}）")
    return records


def read_negatives() -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with NEGATIVES_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def find_dev_line(dev: list[tuple[str, str]], keyword: str) -> str | None:
    for text, _label in dev:
        if keyword in text:
            return text
    return None


# ---------------------------------------------------------------------------
# 1. 配置 / 产物
# ---------------------------------------------------------------------------
def artifact_checks(settings, selection: dict[str, Any], roc: dict[str, Any]) -> None:
    print("\n=== 1. 配置与离线产物 ===")
    check("config reject enabled", settings.reject.enabled is True)
    calibration = settings.reject.calibration
    check(
        "config calibration source=synthetic_negatives",
        calibration.source == "synthetic_negatives",
        str(calibration.source),
    )
    check("config calibration calibrated=false", calibration.calibrated is False)
    check(
        "config calibration method=max_score+entropy",
        calibration.method == "max_score+entropy",
        str(calibration.method),
    )
    check(
        "config calibration sample sizes 2000/1200",
        calibration.positive_sample_size == EXPECTED_POSITIVES
        and calibration.negative_sample_size == EXPECTED_NEGATIVES,
        f"{calibration.positive_sample_size}/{calibration.negative_sample_size}",
    )
    check(
        "config calibration scope_note not empty",
        bool(calibration.scope_note.strip()),
    )

    check("synthetic negatives file exists", NEGATIVES_PATH.is_file(), str(NEGATIVES_PATH))
    negatives = read_negatives()
    counts = {kind: 0 for kind in NEGATIVE_KINDS}
    synthetic_flags: set[bool] = set()
    seeds: set[Any] = set()
    multiline = 0
    for record in negatives:
        kind = record.get("kind")
        if kind in counts:
            counts[kind] += 1
        meta = record.get("meta") or {}
        synthetic_flags.add(meta.get("synthetic") is True)
        seeds.add(meta.get("seed"))
        if "\n" in str(record.get("text", "")):
            multiline += 1
    check(
        "synthetic negatives 1200 (300 per kind)",
        len(negatives) == EXPECTED_NEGATIVES
        and counts == {kind: 300 for kind in NEGATIVE_KINDS},
        f"total={len(negatives)} counts={counts}",
    )
    check("synthetic negatives marked synthetic", synthetic_flags == {True})
    check(
        "synthetic negatives seed recorded",
        seeds == {EXPECTED_SEED},
        str(seeds),
    )
    check("synthetic negatives single-line texts", multiline == 0, f"multiline={multiline}")

    check("reject ROC artifact exists", ROC_PATH.is_file(), str(ROC_PATH))
    check(
        "reject ROC artifact has meta",
        isinstance(roc.get("meta"), dict)
        and all(key in roc["meta"] for key in META_REQUIRED),
        str(sorted(set(META_REQUIRED) - set(roc.get("meta", {})))),
    )
    try:
        generated_at = datetime.fromisoformat(roc["meta"]["generated_at"])
        generated_ok = generated_at.tzinfo is not None
    except Exception:  # noqa: BLE001
        generated_ok = False
    check("ROC meta generated_at has timezone", generated_ok)
    check(
        "ROC meta sample_size = 2000+1200",
        roc["meta"].get("sample_size") == EXPECTED_POSITIVES + EXPECTED_NEGATIVES,
        str(roc["meta"].get("sample_size")),
    )
    check(
        "ROC meta data_kind=offline",
        roc["meta"].get("data_kind") == "offline",
        str(roc["meta"].get("data_kind")),
    )
    rules = roc.get("rules") or {}
    point_counts = {
        name: len((rules.get(name) or {}).get("points") or [])
        for name in ("max_score", "entropy", "combined_or")
    }
    check(
        "ROC points > 0 for all rules",
        all(count > 0 for count in point_counts.values()),
        str(point_counts),
    )
    aucs = {
        name: (rules.get(name) or {}).get("auc") for name in ("max_score", "entropy", "combined_or")
    }
    check(
        "ROC AUC within [0, 1]",
        all(isinstance(auc, (int, float)) and 0.0 <= auc <= 1.0 for auc in aucs.values()),
        str(aucs),
    )
    sample_point = ((rules.get("combined_or") or {}).get("points") or [{}])[0]
    check(
        "ROC points contain recall/retention",
        {"negative_recall", "positive_retention"} <= set(sample_point),
        str(sample_point),
    )

    check("threshold selection artifact exists", SELECTION_PATH.is_file())
    check(
        "threshold selection has meta",
        isinstance(selection.get("meta"), dict)
        and all(key in selection["meta"] for key in META_REQUIRED),
    )
    sampling = selection.get("sampling") or {}
    check(
        "selection sampling records seed/sample sizes",
        sampling.get("positive_sample_size") == EXPECTED_POSITIVES
        and sampling.get("negative_sample_size") == EXPECTED_NEGATIVES
        and selection.get("seed") == EXPECTED_SEED
        and sampling.get("test_set_used") is False,
        f"seed={selection.get('seed')} sampling={ {k: sampling.get(k) for k in ('positive_sample_size','negative_sample_size','test_set_used')} }",
    )
    selected = selection.get("selected") or {}
    check(
        "selection thresholds match models.yaml",
        selected.get("max_score_threshold") == settings.reject.max_score_threshold
        and selected.get("entropy_threshold") == settings.reject.entropy_threshold,
        f"artifact=({selected.get('max_score_threshold')},{selected.get('entropy_threshold')}) "
        f"config=({settings.reject.max_score_threshold},{settings.reject.entropy_threshold})",
    )
    metrics = selected.get("metrics") or {}
    check(
        "selection metrics valid",
        0.0 <= (metrics.get("negative_recall") or -1) <= 1.0
        and 0.0 <= (metrics.get("positive_retention") or -1) <= 1.0,
        str(metrics),
    )
    policy = selection.get("selection_policy") or {}
    check(
        "selection policy is recall_first with retention floor",
        policy.get("priority") == "recall_first"
        and bool(policy.get("positive_retention_floors")),
        str(policy.get("used_constraint")),
    )


# ---------------------------------------------------------------------------
# 2/3/4. 核心分类行为 + 表驱动
# ---------------------------------------------------------------------------
def _score_metrics(outcome) -> tuple[float, float]:
    values = [entry.score for entry in outcome.scores or ()]
    return max(values), shannon_entropy(values)


def service_checks(settings, scorer: LocalFasttextScorer, dev: list[tuple[str, str]]) -> None:
    print("\n=== 2. 演示样例与正常新闻 ===")
    adapters = {
        "fasttext": LocalFasttextAdapter(scorer, settings.classes),
        "llm": NoScoreAdapter(),
    }
    service = make_service(settings, adapters)
    thresholds = settings.reject
    max_threshold = thresholds.max_score_threshold
    entropy_threshold = thresholds.entropy_threshold

    outcome = classify(service, DEMO_TITLE)
    max_score, entropy = _score_metrics(outcome)
    check(
        "demo chitchat rejected (long)",
        outcome.rejected is True
        and outcome.reject_reason in {
            REJECT_REASON_LOW_MAX_SCORE,
            REJECT_REASON_HIGH_ENTROPY,
        },
        f"rejected={outcome.rejected} reason={outcome.reject_reason}",
    )
    check(
        "demo chitchat keeps pred_class",
        isinstance(outcome.pred_class, str) and bool(outcome.pred_class),
        str(outcome.pred_class),
    )
    check(
        "demo chitchat keeps latency/scores/top_k",
        isinstance(outcome.latency_ms, int)
        and outcome.scores is not None
        and len(outcome.scores) == len(settings.classes)
        and outcome.score_kind == "raw_fasttext"
        and outcome.top_k is not None
        and len(outcome.top_k) == 3,
        f"latency={outcome.latency_ms} scores={None if outcome.scores is None else len(outcome.scores)}",
    )

    meta = outcome.threshold_meta or {}
    check(
        "threshold_meta has F6 required keys",
        isinstance(outcome.threshold_meta, dict)
        and all(key in meta for key in THRESHOLD_META_REQUIRED),
        str(sorted(set(THRESHOLD_META_REQUIRED) - set(meta))),
    )
    check(
        "threshold_meta source/calibrated/method",
        meta.get("source") == "synthetic_negatives"
        and meta.get("calibrated") is False
        and meta.get("method") == "max_score+entropy",
        str({key: meta.get(key) for key in ("source", "calibrated", "method")}),
    )
    check(
        "threshold_meta thresholds match config",
        meta.get("max_score_threshold") == max_threshold
        and meta.get("entropy_threshold") == entropy_threshold,
        str({key: meta.get(key) for key in ("max_score_threshold", "entropy_threshold")}),
    )
    check(
        "threshold_meta sample_size 3200",
        meta.get("sample_size") == EXPECTED_POSITIVES + EXPECTED_NEGATIVES,
        str(meta.get("sample_size")),
    )
    scope_note = str(meta.get("scope_note") or "")
    check(
        "threshold_meta scope_note mentions synthetic & uncalibrated",
        "合成负样本" in scope_note and "未校准" in scope_note,
        scope_note,
    )
    check(
        "threshold_meta observed values match local inference",
        abs(float(meta.get("observed_max_score", -1)) - max_score) < 1e-6
        and abs(float(meta.get("observed_entropy", -1)) - entropy) < 1e-6,
        f"meta=({meta.get('observed_max_score')},{meta.get('observed_entropy')}) "
        f"local=({max_score:.6f},{entropy:.6f})",
    )

    # 短句「今天天气不错」是工作清单验收原文；必须同样被拒识。
    outcome_short = classify(service, DEMO_TITLE_SHORT)
    check(
        "demo chitchat rejected (short 今天天气不错)",
        outcome_short.rejected is True and bool(outcome_short.pred_class),
        f"rejected={outcome_short.rejected} reason={outcome_short.reject_reason}",
    )

    normal_title = find_dev_line(dev, "上半年北京二手房成交")
    if normal_title is None:
        normal_title = find_dev_line(dev, "二手房") or dev[0][0]
    outcome_normal = classify(service, normal_title)
    normal_max, normal_entropy = _score_metrics(outcome_normal)
    check(
        "normal dev news retained",
        outcome_normal.rejected is False
        and outcome_normal.reject_reason is None
        and bool(outcome_normal.pred_class),
        f"title={normal_title!r} rejected={outcome_normal.rejected} "
        f"reason={outcome_normal.reject_reason} max={normal_max:.4f} entropy={normal_entropy:.4f}",
    )
    check(
        "normal dev news has threshold_meta (has scores)",
        isinstance(outcome_normal.threshold_meta, dict),
        str(outcome_normal.threshold_meta),
    )
    # 文档示例句（非 test 集，仅作正常新闻的补充样例）
    outcome_example = classify(service, "北京二手房成交量创年内新高")
    check(
        "normal example sentence retained",
        outcome_example.rejected is False,
        f"rejected={outcome_example.rejected} reason={outcome_example.reject_reason}",
    )

    print("\n=== 3. 表驱动：10 正（dev 各类首条）+ 10 负（四类合成） ===")
    classes = list(settings.classes)
    first_per_class: dict[str, str] = {}
    for text, raw_label in dev:
        label = classes[int(raw_label)]
        first_per_class.setdefault(label, text)
    positive_rows = [(label, first_per_class[label]) for label in classes]
    for label, text in positive_rows:
        row = classify(service, text)
        row_max, row_entropy = _score_metrics(row)
        expected_rejected = row_max < max_threshold or row_entropy > entropy_threshold
        expected_reason = None
        if row_max < max_threshold:
            expected_reason = REJECT_REASON_LOW_MAX_SCORE
        elif row_entropy > entropy_threshold:
            expected_reason = REJECT_REASON_HIGH_ENTROPY
        check(
            f"table positive kept [{label}]",
            row.rejected is expected_rejected and row.reject_reason == expected_reason,
            f"text={text!r} rejected={row.rejected} reason={row.reject_reason} "
            f"max={row_max:.4f} entropy={row_entropy:.4f}",
        )
        print(
            f"  POS  {label:<13} rejected={str(row.rejected):<5} "
            f"reason={str(row.reject_reason):<14} max={row_max:.4f} "
            f"entropy={row_entropy:.4f}  {text}"
        )

    negatives = read_negatives()
    picked: list[dict[str, Any]] = []
    quotas = {"chitchat": 3, "code": 3, "product": 2, "symbols": 2}
    for kind in NEGATIVE_KINDS:
        picked.extend([row for row in negatives if row["kind"] == kind][: quotas[kind]])
    check("table negatives has 10 rows", len(picked) == 10, str(len(picked)))
    rejected_count = 0
    for row in picked:
        outcome_neg = classify(service, row["text"])
        neg_max, neg_entropy = _score_metrics(outcome_neg)
        expected_rejected = neg_max < max_threshold or neg_entropy > entropy_threshold
        expected_reason = None
        if neg_max < max_threshold:
            expected_reason = REJECT_REASON_LOW_MAX_SCORE
        elif neg_entropy > entropy_threshold:
            expected_reason = REJECT_REASON_HIGH_ENTROPY
        if expected_rejected:
            rejected_count += 1
        check(
            f"table negative consistent [{row['kind']}]",
            outcome_neg.rejected is expected_rejected
            and outcome_neg.reject_reason == expected_reason
            and bool(outcome_neg.pred_class),
            f"text={row['text']!r} expected={expected_rejected} actual={outcome_neg.rejected} "
            f"reason={outcome_neg.reject_reason} max={neg_max:.4f} entropy={neg_entropy:.4f}",
        )
        print(
            f"  NEG  {row['kind']:<9} rejected={str(outcome_neg.rejected):<5} "
            f"reason={str(outcome_neg.reject_reason):<14} max={neg_max:.4f} "
            f"entropy={neg_entropy:.4f}  {row['text']}"
        )
    print(
        f"  (table negatives: rejected={rejected_count}/10；"
        "合成负样本整体拒识率见第 5 段与 threshold_selection.json)"
    )


# ---------------------------------------------------------------------------
# 5. 全量复算（2000 正 + 1200 负）与产物一致
# ---------------------------------------------------------------------------
def reproduction_checks(
    settings,
    scorer: LocalFasttextScorer,
    dev: list[tuple[str, str]],
    selection: dict[str, Any],
) -> None:
    print("\n=== 4. 全量复算：拒识计数与标定产物一致 ===")
    adapters = {"fasttext": LocalFasttextAdapter(scorer, settings.classes)}
    service = make_service(settings, adapters)
    selected = selection["selected"]
    metrics = selected["metrics"]

    positive_rejected = sum(1 for text, _label in dev if classify(service, text).rejected)
    expected_positive_rejected = (
        metrics["positive_total"] - metrics["positive_kept"]
    )
    check(
        "positive rejection count reproduces artifact",
        positive_rejected == expected_positive_rejected,
        f"actual={positive_rejected} expected={expected_positive_rejected}",
    )

    negatives = read_negatives()
    negative_rejected = 0
    per_kind: dict[str, int] = {kind: 0 for kind in NEGATIVE_KINDS}
    for row in negatives:
        if classify(service, row["text"]).rejected:
            negative_rejected += 1
            per_kind[row["kind"]] += 1
    check(
        "negative rejection count reproduces artifact",
        negative_rejected == metrics["negative_rejected"],
        f"actual={negative_rejected} expected={metrics['negative_rejected']}",
    )
    expected_per_kind = {
        kind: selected["per_kind_negative"][kind]["rejected"] for kind in NEGATIVE_KINDS
    }
    check(
        "per-kind negative rejection reproduces artifact",
        per_kind == expected_per_kind,
        f"actual={per_kind} expected={expected_per_kind}",
    )


# ---------------------------------------------------------------------------
# 6. 无分数 / 配置可改 / 环境变量覆盖
# ---------------------------------------------------------------------------
def behavior_checks(settings, scorer: LocalFasttextScorer) -> None:
    print("\n=== 5. 无分数、配置阈值生效与可修改 ===")
    adapters = {
        "fasttext": LocalFasttextAdapter(scorer, settings.classes),
        "llm": NoScoreAdapter(),
    }
    service = make_service(settings, adapters)

    outcome_no_score = classify(service, "今天天气不错", model_id="llm")
    check(
        "no-score adapter not rejected and no crash",
        outcome_no_score.rejected is False
        and outcome_no_score.reject_reason is None
        and outcome_no_score.threshold_meta is None
        and bool(outcome_no_score.pred_class),
        f"rejected={outcome_no_score.rejected} reason={outcome_no_score.reject_reason} "
        f"meta={outcome_no_score.threshold_meta}",
    )

    # 内存配置修改：放宽阈值 -> 演示样例不再被拒识（证明阈值真实生效）
    lenient = settings.reject.model_copy(
        update={"max_score_threshold": 0.3, "entropy_threshold": 2.0}
    )
    lenient_service = make_service(settings, adapters, reject=lenient)
    outcome_lenient = classify(lenient_service, DEMO_TITLE)
    check(
        "changed thresholds change result (lenient keeps demo)",
        outcome_lenient.rejected is False
        and outcome_lenient.reject_reason is None,
        f"rejected={outcome_lenient.rejected} reason={outcome_lenient.reject_reason}",
    )

    # 收紧阈值 -> 正常新闻也被拒识（high_entropy），证明两个条件都可触发
    strict = settings.reject.model_copy(
        update={"max_score_threshold": 0.99, "entropy_threshold": 0.0}
    )
    strict_service = make_service(settings, adapters, reject=strict)
    outcome_strict = classify(strict_service, "北京二手房成交量创年内新高")
    check(
        "changed thresholds change result (strict rejects normal news)",
        outcome_strict.rejected is True
        and outcome_strict.reject_reason == REJECT_REASON_HIGH_ENTROPY,
        f"rejected={outcome_strict.rejected} reason={outcome_strict.reject_reason}",
    )

    # 关闭拒识 -> 不拒识且 meta 为 null
    disabled = settings.reject.model_copy(update={"enabled": False})
    disabled_service = make_service(settings, adapters, reject=disabled)
    outcome_disabled = classify(disabled_service, DEMO_TITLE)
    check(
        "disabled reject -> rejected=false / meta=null",
        outcome_disabled.rejected is False
        and outcome_disabled.reject_reason is None
        and outcome_disabled.threshold_meta is None
        and bool(outcome_disabled.pred_class),
        f"rejected={outcome_disabled.rejected} meta={outcome_disabled.threshold_meta}",
    )

    print("\n=== 6. 环境变量覆盖与非法配置拒绝 ===")
    clear_reject_env()
    os.environ["TMF_REJECT_MAX_SCORE_THRESHOLD"] = "0.3"
    os.environ["TMF_REJECT_ENTROPY_THRESHOLD"] = "2.0"
    try:
        overridden = load_settings()
        check(
            "env overrides thresholds",
            overridden.reject.max_score_threshold == 0.3
            and overridden.reject.entropy_threshold == 2.0,
            f"{overridden.reject.max_score_threshold}/{overridden.reject.entropy_threshold}",
        )
        overridden_service = make_service(overridden, adapters, reject=overridden.reject)
        outcome_overridden = classify(overridden_service, DEMO_TITLE)
        check(
            "env-overridden thresholds change result",
            outcome_overridden.rejected is False,
            f"rejected={outcome_overridden.rejected}",
        )
    finally:
        clear_reject_env()

    os.environ["TMF_REJECT_ENABLED"] = "false"
    try:
        disabled_env = load_settings()
        check(
            "env TMF_REJECT_ENABLED=false disables reject",
            disabled_env.reject.enabled is False,
        )
    finally:
        clear_reject_env()

    invalid_cases = [
        ("TMF_REJECT_ENABLED", "maybe"),
        ("TMF_REJECT_MAX_SCORE_THRESHOLD", "0"),
        ("TMF_REJECT_MAX_SCORE_THRESHOLD", "abc"),
        ("TMF_REJECT_ENTROPY_THRESHOLD", "99"),
    ]
    for key, value in invalid_cases:
        os.environ[key] = value
        try:
            load_settings()
            check(f"invalid env rejected ({key}={value})", False)
        except ConfigError:
            check(f"invalid env rejected ({key}={value})", True)
        finally:
            clear_reject_env()


# ---------------------------------------------------------------------------
# 7. /classify 响应字段（TestClient，不启动 uvicorn / 不占端口）
# ---------------------------------------------------------------------------
def api_checks(settings, scorer: LocalFasttextScorer, dev: list[tuple[str, str]]) -> None:
    print("\n=== 7. /classify 响应字段（TestClient，临时 TMF_DB_PATH） ===")
    from serving.gateway.main import create_app

    normal_title = find_dev_line(dev, "上半年北京二手房成交") or dev[0][0]
    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["TMF_DB_PATH"] = str(Path(temp_dir) / "verify_b3.db")
        try:
            app = create_app()
            with TestClient(app) as client:
                fake_adapters = {
                    "fasttext": LocalFasttextAdapter(scorer, settings.classes),
                    "llm": NoScoreAdapter(),
                }
                app.state.classifier = make_service(
                    settings,
                    fake_adapters,
                    reject=settings.reject,
                    registry=app.state.registry,
                )

                response = client.post(
                    "/api/v1/classify",
                    json={"title": DEMO_TITLE, "model": "fasttext"},
                )
                body = response.json()
                meta = body.get("threshold_meta") or {}
                check(
                    "api demo returns rejected=true with fields",
                    response.status_code == 200
                    and "error" not in body
                    and body.get("rejected") is True
                    and body.get("reject_reason") in {
                        REJECT_REASON_LOW_MAX_SCORE,
                        REJECT_REASON_HIGH_ENTROPY,
                    }
                    and isinstance(body.get("pred_class"), str)
                    and body.get("pred_class") != ""
                    and isinstance(body.get("latency_ms"), int)
                    and isinstance(body.get("scores"), dict)
                    and len(body["scores"]) == len(settings.classes)
                    and body.get("score_kind") == "raw_fasttext"
                    and isinstance(body.get("top_k"), list)
                    and len(body["top_k"]) == 3,
                    f"{response.status_code} {str(body)[:300]}",
                )
                check(
                    "api threshold_meta shape",
                    isinstance(body.get("threshold_meta"), dict)
                    and all(key in meta for key in THRESHOLD_META_REQUIRED)
                    and meta.get("source") == "synthetic_negatives"
                    and meta.get("calibrated") is False,
                    str(meta)[:300],
                )

                normal_response = client.post(
                    "/api/v1/classify",
                    json={"title": normal_title, "model": "fasttext"},
                )
                normal_body = normal_response.json()
                check(
                    "api normal news rejected=false",
                    normal_response.status_code == 200
                    and normal_body.get("rejected") is False
                    and normal_body.get("reject_reason") is None
                    and isinstance(normal_body.get("threshold_meta"), dict),
                    f"{normal_response.status_code} {str(normal_body)[:300]}",
                )

                llm_response = client.post(
                    "/api/v1/classify",
                    json={"title": "今天天气不错", "model": "llm"},
                )
                llm_body = llm_response.json()
                check(
                    "api no-score model rejected=false / meta=null",
                    llm_response.status_code == 200
                    and llm_body.get("rejected") is False
                    and llm_body.get("reject_reason") is None
                    and llm_body.get("threshold_meta") is None
                    and bool(llm_body.get("pred_class")),
                    f"{llm_response.status_code} {str(llm_body)[:300]}",
                )
        finally:
            os.environ.pop("TMF_DB_PATH", None)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    clear_reject_env()
    started = time.perf_counter()
    print("=== B3 软拒识离线验收（本地 fastText 推理，不启动服务/不调 LLM） ===")

    settings = load_settings()
    classes = read_classes()
    if list(settings.classes) != classes:
        print("ERROR: settings.classes 与 class.txt 不一致")
        return 1
    selection = load_json(SELECTION_PATH)
    roc = load_json(ROC_PATH)
    dev = read_dev_sample()

    artifact_checks(settings, selection, roc)

    scorer = LocalFasttextScorer(classes)
    service_checks(settings, scorer, dev)
    reproduction_checks(settings, scorer, dev, selection)
    behavior_checks(settings, scorer)
    api_checks(settings, scorer, dev)

    clear_reject_env()
    elapsed = time.perf_counter() - started
    print("")
    if FAILURES:
        print(f"RESULT FAIL ({len(FAILURES)}): " + ", ".join(FAILURES))
        print(f"elapsed={elapsed:.1f}s")
        return 1
    print(f"RESULT PASS (elapsed={elapsed:.1f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
