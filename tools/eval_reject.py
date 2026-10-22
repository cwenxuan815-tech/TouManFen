#!/usr/bin/env python
"""B3：软拒识阈值标定（离线，本地 fastText 推理，不启动任何服务/worker/LLM）。

做什么：
1. 读 ``00-data/data/dev.txt`` 的**固定可复现抽样**（默认文件顺序前 2000 条）作为正样本；
2. 读 ``artifacts/b3/synthetic_negatives.jsonl`` 的 1200 条**合成**负样本；
3. 用本地 fastText 词级模型（``02-fasttext/save_model/word_auto_train.pkl``）
   在本进程推理，得到完整 10 类分数 -> ``max_score`` 与归一化 Shannon entropy；
4. 分别对 max_score / entropy / 组合 OR 规则生成 ROC 点（负样本拒识召回 vs 正样本保留率）
   并落盘 ``artifacts/b3/reject_roc.json``；
5. 按「召回优先」在显式约束下选阈值，落盘 ``artifacts/b3/threshold_selection.json``。

严格边界：
- **不训练、不重校准**任何模型，只做前向推理；
- 只用 dev 抽样 + 合成负样本，**不使用 test 集**（避免测试集调参）；
- 不写 ``serving/config/models.yaml``（阈值由人工按本脚本产物填入并再次验收）。

产物 meta 遵循 docs/工程实施工作清单_75工时.md §6.8（可用 tools/validate_meta.py 校验）。

用法：
    D:\\Miniconda3\\envs\\py3109\\python.exe tools/eval_reject.py
    ... --dev-sample-size 2000 --out-dir artifacts/b3
    ... --allow-missing-model   # 仅调试：模型不可用时跳过推理（产物会明确标注）
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Sequence

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEV_PATH = PROJECT_ROOT / "00-data" / "data" / "dev.txt"
CLASS_PATH = PROJECT_ROOT / "00-data" / "data" / "class.txt"
NEGATIVES_PATH = PROJECT_ROOT / "artifacts" / "b3" / "synthetic_negatives.jsonl"
MODEL_PATH = PROJECT_ROOT / "02-fasttext" / "save_model" / "word_auto_train.pkl"
DEFAULT_OUT_DIR = PROJECT_ROOT / "artifacts" / "b3"

DEFAULT_DEV_SAMPLE_SIZE = 2000
NEGATIVES_PER_KIND = 300
NEGATIVE_KINDS = ("chitchat", "code", "product", "symbols")
ENTROPY_BASE = "e"
ENTROPY_MAX_NATS = math.log(10)  # 10 类归一化分布的理论上界

# 阈值候选统一保留 4 位小数：最终写入 models.yaml 的数值可直接复算指标。
THRESHOLD_DECIMALS = 4
ROC_POINT_CAP = 6000  # 单特征 ROC 点上限（超过则按分位数抽稀，避免产物过大）

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# 数据读取
# ---------------------------------------------------------------------------
def read_classes() -> list[str]:
    classes = [
        line.strip()
        for line in CLASS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if len(classes) != 10:
        raise SystemExit(f"ERROR: class.txt 期望 10 类，实际 {len(classes)}")
    return classes


def read_dev_sample(classes: Sequence[str], sample_size: int) -> list[dict[str, Any]]:
    """按文件顺序取前 ``sample_size`` 条（固定、可复现，不做随机抽样）。"""
    records: list[dict[str, Any]] = []
    with DEV_PATH.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) != 2:
                raise SystemExit(f"ERROR: dev.txt 行格式异常: {line[:60]!r}")
            text, raw_label = parts
            try:
                label_index = int(raw_label)
            except ValueError as exc:
                raise SystemExit(f"ERROR: dev.txt 标签不是整数: {raw_label!r}") from exc
            if not 0 <= label_index < len(classes):
                raise SystemExit(f"ERROR: dev.txt 标签越界: {raw_label!r}")
            records.append(
                {
                    "text": text,
                    "label": classes[label_index],
                    "label_index": label_index,
                }
            )
            if len(records) >= sample_size:
                break
    if len(records) != sample_size:
        raise SystemExit(
            f"ERROR: dev.txt 只有 {len(records)} 条，少于请求的 {sample_size}"
        )
    return records


def read_negatives() -> list[dict[str, Any]]:
    if not NEGATIVES_PATH.is_file():
        raise SystemExit(
            f"ERROR: 缺少合成负样本 {NEGATIVES_PATH}，请先运行 tools/make_negatives.py"
        )
    records: list[dict[str, Any]] = []
    with NEGATIVES_PATH.open("r", encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"ERROR: 负样本第 {line_no} 行 JSON 解析失败: {exc}")
            text = record.get("text")
            kind = record.get("kind")
            if not isinstance(text, str) or not text.strip():
                raise SystemExit(f"ERROR: 负样本第 {line_no} 行 text 非法")
            if kind not in NEGATIVE_KINDS:
                raise SystemExit(f"ERROR: 负样本第 {line_no} 行 kind 非法: {kind!r}")
            meta = record.get("meta") or {}
            if meta.get("synthetic") is not True:
                raise SystemExit(
                    f"ERROR: 负样本第 {line_no} 行 meta.synthetic 必须为 true（不得伪装真实数据）"
                )
            records.append(
                {
                    "text": text,
                    "kind": kind,
                    "seed": meta.get("seed"),
                    "template_id": meta.get("template_id"),
                }
            )

    counts = Counter(record["kind"] for record in records)
    expected = {kind: NEGATIVES_PER_KIND for kind in NEGATIVE_KINDS}
    if dict(counts) != expected:
        raise SystemExit(
            f"ERROR: 负样本类别计数应为 {expected}，实际 {dict(counts)}"
        )
    seeds = {record["seed"] for record in records}
    if len(seeds) != 1 or not isinstance(next(iter(seeds)), int):
        raise SystemExit(f"ERROR: 负样本 seed 应唯一且为整数，实际 {seeds}")
    return records


# ---------------------------------------------------------------------------
# 本地 fastText 推理（与 02-fasttext/ft_predict_service.py 同一预处理/契约）
# ---------------------------------------------------------------------------
class LocalFasttextScorer:
    """本地进程内 fastText 词级推理：jieba 分词 -> 10 类分数。"""

    def __init__(self, classes: Sequence[str]) -> None:
        import fasttext  # 延迟导入：--allow-missing-model 时也能给出清晰错误
        import jieba

        if not MODEL_PATH.is_file():
            raise FileNotFoundError(MODEL_PATH)
        self._classes = list(classes)
        self._class_set = set(classes)
        self._jieba = jieba
        self._model = fasttext.load_model(str(MODEL_PATH))

    def score(self, title: str) -> dict[str, Any]:
        # fastText predict 不接受换行；统空白，保证任意输入都可评估。
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
            # 与 worker 一致：float32 归一化可能让最大值略超 1，按契约截断到 [0, 1]。
            mapped[class_id] = min(1.0, max(0.0, float(probability)))
        if set(mapped) != self._class_set:
            raise ValueError("fastText 未返回全部 10 类分数")
        scores = [mapped[class_id] for class_id in self._classes]
        total = sum(scores)
        if total <= 0.0:
            entropy = 0.0
        else:
            entropy = -sum(
                (value / total) * math.log(value / total)
                for value in scores
                if value > 0.0
            )
        max_score = max(scores)
        pred_class = self._classes[int(np.argmax(scores))]
        return {
            "max_score": float(max_score),
            "entropy": float(entropy),
            "pred_class": pred_class,
            "scores": scores,
        }


# ---------------------------------------------------------------------------
# 指标 / ROC
# ---------------------------------------------------------------------------
def _dist_stats(values: np.ndarray) -> dict[str, float]:
    return {
        "mean": round(float(values.mean()), 6),
        "min": round(float(values.min()), 6),
        "p05": round(float(np.percentile(values, 5)), 6),
        "p50": round(float(np.percentile(values, 50)), 6),
        "p95": round(float(np.percentile(values, 95)), 6),
        "max": round(float(values.max()), 6),
    }


def _auc_from_points(points: list[dict[str, float]]) -> float:
    """用 (false_reject_rate, negative_recall) 折线做梯形积分。"""
    if not points:
        return 0.0
    ordered = sorted({(point["false_reject_rate"], point["negative_recall"]) for point in points})
    curve = [(0.0, 0.0)] + ordered + [(1.0, 1.0)]
    auc = 0.0
    for (x0, y0), (x1, y1) in zip(curve, curve[1:]):
        auc += (x1 - x0) * (y0 + y1) / 2.0
    return round(auc, 6)


def _pareto_frontier(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """保留 (positive_retention, negative_recall) 的非支配点，按保留率升序返回。

    支配定义：存在另一点同时拥有 >= 的保留率与 >= 的负样本召回（至少一项严格更大）。
    """
    ordered = sorted(
        rows,
        key=lambda row: (
            -row["positive_retention"],
            -row["negative_recall"],
            row["max_score_threshold"],
            row["entropy_threshold"],
        ),
    )
    frontier: list[dict[str, Any]] = []
    best_recall = -1.0
    for row in ordered:
        if row["negative_recall"] > best_recall + 1e-12:
            frontier.append(row)
            best_recall = row["negative_recall"]
    frontier.sort(
        key=lambda row: (
            row["positive_retention"],
            -row["negative_recall"],
            row["max_score_threshold"],
            row["entropy_threshold"],
        )
    )
    return frontier


def build_single_feature_roc(
    positives: np.ndarray,
    negatives: np.ndarray,
    *,
    reject_low: bool,
) -> dict[str, Any]:
    """单特征 ROC：阈值 t 下 ``max_score < t`` 或 ``entropy > t`` 触发拒识。"""
    all_values = np.concatenate([positives, negatives])
    thresholds = np.unique(np.round(all_values, THRESHOLD_DECIMALS))
    if len(thresholds) > ROC_POINT_CAP:
        indices = np.unique(
            np.linspace(0, len(thresholds) - 1, ROC_POINT_CAP).astype(int)
        )
        thresholds = thresholds[indices]

    points: list[dict[str, float]] = []
    for threshold in thresholds:
        if reject_low:
            negative_recall = float((negatives < threshold).mean())
            positive_retention = float((positives >= threshold).mean())
        else:
            negative_recall = float((negatives > threshold).mean())
            positive_retention = float((positives <= threshold).mean())
        points.append(
            {
                "threshold": round(float(threshold), THRESHOLD_DECIMALS),
                "negative_recall": round(negative_recall, 6),
                "positive_retention": round(positive_retention, 6),
                "false_reject_rate": round(1.0 - positive_retention, 6),
            }
        )
    return {
        "direction": "reject if value < threshold" if reject_low else "reject if value > threshold",
        "auc": _auc_from_points(points),
        "point_count": len(points),
        "points": points,
    }


def build_combined_roc(
    pos_max: np.ndarray,
    pos_ent: np.ndarray,
    neg_max: np.ndarray,
    neg_ent: np.ndarray,
    *,
    grid_size: int = 240,
) -> dict[str, Any]:
    """组合规则 ``max_score < t_max OR entropy > t_ent`` 的 2D 阈值网格 + Pareto ROC。"""
    all_max = np.round(np.concatenate([pos_max, neg_max]), THRESHOLD_DECIMALS)
    all_ent = np.round(np.concatenate([pos_ent, neg_ent]), THRESHOLD_DECIMALS)
    quantiles = np.linspace(0.0, 1.0, grid_size)
    max_grid = np.unique(np.quantile(all_max, quantiles))
    ent_grid = np.unique(np.quantile(all_ent, quantiles))
    max_grid = np.round(max_grid, THRESHOLD_DECIMALS)
    ent_grid = np.round(ent_grid, THRESHOLD_DECIMALS)

    rows: list[dict[str, Any]] = []
    for max_threshold in max_grid:
        neg_hit_max = neg_max < max_threshold
        pos_hit_max = pos_max < max_threshold
        for ent_threshold in ent_grid:
            neg_hit = neg_hit_max | (neg_ent > ent_threshold)
            pos_hit = pos_hit_max | (pos_ent > ent_threshold)
            negative_recall = float(neg_hit.mean())
            positive_retention = float(1.0 - pos_hit.mean())
            rows.append(
                {
                    "max_score_threshold": float(max_threshold),
                    "entropy_threshold": float(ent_threshold),
                    "negative_recall": round(negative_recall, 6),
                    "positive_retention": round(positive_retention, 6),
                    "false_reject_rate": round(1.0 - positive_retention, 6),
                }
            )

    frontier = _pareto_frontier(rows)
    return {
        "direction": "reject if max_score < t_max OR entropy > t_ent",
        "grid": {
            "max_score_candidates": int(len(max_grid)),
            "entropy_candidates": int(len(ent_grid)),
            "combinations": int(len(rows)),
        },
        "auc": _auc_from_points(frontier),
        "point_count": len(frontier),
        "points": frontier,
        "_all_rows": rows,  # 仅供选阈值，写盘前剔除
    }


# ---------------------------------------------------------------------------
# 阈值选择（召回优先）
# ---------------------------------------------------------------------------
#: 正样本保留率下限（先主约束，不达标才依次降级）。「召回优先」= 在该下限内最大化负样本召回。
#: 0.95 档位无法拒识验收样例「今天天气不错」(entropy≈0.8067)，因此取仍能覆盖该
#: 样例的最高档 0.94；更低档只会增加正样本误拒，不采用。
POSITIVE_RETENTION_FLOORS: tuple[float, ...] = (0.94, 0.90, 0.85)


def select_thresholds(
    roc: dict[str, Any],
    pos_max: np.ndarray,
    pos_ent: np.ndarray,
    neg_max: np.ndarray,
    neg_ent: np.ndarray,
) -> dict[str, Any]:
    """在正样本保留率下限内按「召回优先」选阈值。

    确定性 tie-break：
    1) 更高的正样本保留率；
    2) 两个特征都真正参与拒识（``min`` 单特征负样本覆盖率更大），
       避免出现「某个阈值形同虚设」的等价解；
    3) 阈值到两类分布边界的余量更大（不贴边，更稳）。
    """
    rows = roc["_all_rows"]
    pos_p05 = float(np.percentile(pos_max, 5))
    neg_p90 = float(np.percentile(neg_max, 90))
    neg_p05 = float(np.percentile(neg_ent, 5))
    pos_p95 = float(np.percentile(pos_ent, 95))

    def margin(row: dict[str, Any]) -> float:
        """阈值到正/负样本分布分位数的余量；越大越稳（两侧都不贴边）。"""
        return min(
            pos_p05 - row["max_score_threshold"],
            row["max_score_threshold"] - neg_p90,
            neg_p05 - row["entropy_threshold"],
            row["entropy_threshold"] - pos_p95,
        )

    def feature_participation(row: dict[str, Any]) -> float:
        """两个条件各自单独能覆盖多少负样本，取较小值（越大说明越均衡）。"""
        max_active = float((neg_max < row["max_score_threshold"]).mean())
        entropy_active = float((neg_ent > row["entropy_threshold"]).mean())
        return min(max_active, entropy_active)

    used_level: dict[str, Any] | None = None
    eligible: list[dict[str, Any]] = []
    for floor in POSITIVE_RETENTION_FLOORS:
        eligible = [
            {
                **row,
                "margin": round(margin(row), 6),
                "feature_participation": round(feature_participation(row), 6),
            }
            for row in rows
            if row["positive_retention"] >= floor
        ]
        if eligible:
            used_level = {"positive_retention_min": floor}
            break
    if not eligible:
        # 极端兜底：约束全不满足时保正样本优先，再按召回排序。
        eligible = [
            {
                **row,
                "margin": round(margin(row), 6),
                "feature_participation": round(feature_participation(row), 6),
            }
            for row in rows
        ]
        used_level = {
            "positive_retention_min": None,
            "fallback": "所有保留率下限均不满足，退化为先保正样本",
        }

    eligible.sort(
        key=lambda row: (
            -row["negative_recall"],
            -row["positive_retention"],
            -row["feature_participation"],
            -row["margin"],
        )
    )
    best = eligible[0]
    return {
        "used_constraint": used_level,
        "selected": {
            "max_score_threshold": round(best["max_score_threshold"], THRESHOLD_DECIMALS),
            "entropy_threshold": round(best["entropy_threshold"], THRESHOLD_DECIMALS),
            "negative_recall": best["negative_recall"],
            "positive_retention": best["positive_retention"],
            "margin": best["margin"],
            "feature_participation": best["feature_participation"],
        },
        "top_candidates": [
            {
                "max_score_threshold": round(row["max_score_threshold"], THRESHOLD_DECIMALS),
                "entropy_threshold": round(row["entropy_threshold"], THRESHOLD_DECIMALS),
                "negative_recall": row["negative_recall"],
                "positive_retention": row["positive_retention"],
                "margin": row["margin"],
                "feature_participation": row["feature_participation"],
            }
            for row in eligible[:20]
        ],
    }


def evaluate_rule(
    thresholds: dict[str, float],
    pos_max: np.ndarray,
    pos_ent: np.ndarray,
    neg_max: np.ndarray,
    neg_ent: np.ndarray,
) -> dict[str, Any]:
    """在最终阈值（已四舍五入）上重算指标，保证与落盘配置一致。"""
    max_score_threshold = thresholds["max_score_threshold"]
    entropy_threshold = thresholds["entropy_threshold"]
    neg_rejected = (neg_max < max_score_threshold) | (neg_ent > entropy_threshold)
    pos_rejected = (pos_max < max_score_threshold) | (pos_ent > entropy_threshold)
    return {
        "negative_recall": round(float(neg_rejected.mean()), 6),
        "positive_retention": round(float(1.0 - pos_rejected.mean()), 6),
        "negative_rejected": int(neg_rejected.sum()),
        "negative_total": int(neg_rejected.size),
        "positive_kept": int((~pos_rejected).sum()),
        "positive_total": int(pos_rejected.size),
    }


def _best_at_floor(
    rows: list[dict[str, Any]], floor: float
) -> dict[str, Any] | None:
    """给定正样本保留率下限，返回该下限下召回最高的候选阈值（供报告对照）。"""
    eligible = [row for row in rows if row["positive_retention"] >= floor]
    if not eligible:
        return None
    best = max(
        eligible,
        key=lambda row: (
            row["negative_recall"],
            row["positive_retention"],
            -row["max_score_threshold"],
            -row["entropy_threshold"],
        ),
    )
    return {
        "max_score_threshold": round(best["max_score_threshold"], THRESHOLD_DECIMALS),
        "entropy_threshold": round(best["entropy_threshold"], THRESHOLD_DECIMALS),
        "negative_recall": best["negative_recall"],
        "positive_retention": best["positive_retention"],
    }


# ---------------------------------------------------------------------------
# 元数据
# ---------------------------------------------------------------------------
def build_meta(
    *, dev_sample_size: int, negative_sample_size: int, split: str
) -> dict[str, Any]:
    # B3 改动当前尚未提交：按 §6.8 契约标注 uncommitted，不伪称已入库版本。
    generated_at = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
    return {
        "dataset": (
            "00-data/data/dev.txt(前N条) + artifacts/b3/synthetic_negatives.jsonl"
        ),
        "split": split,
        "sample_size": dev_sample_size + negative_sample_size,
        "generated_at": generated_at,
        "code_version": "uncommitted",
        "model_versions": {"fasttext": "word_auto_train.pkl"},
        "metric_def": {
            "negative_recall": "合成负样本被拒识比例",
            "positive_retention": "dev 正样本未被拒识比例",
            "auc": "ROC 梯形积分（x=正样本误拒率, y=负样本拒识召回）",
            "entropy": f"归一化 10 类分数 Shannon entropy（base={ENTROPY_BASE}）",
        },
        "data_kind": "offline",
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="B3 软拒识阈值标定（离线）")
    parser.add_argument(
        "--dev-sample-size",
        type=int,
        default=DEFAULT_DEV_SAMPLE_SIZE,
        help="dev 正样本固定抽样条数（文件顺序前 N 条，默认 2000）",
    )
    parser.add_argument("--out-dir", default=str(DEFAULT_OUT_DIR), help="产物目录")
    parser.add_argument(
        "--allow-missing-model",
        action="store_true",
        help="仅调试：模型缺失时不推理（不生成阈值，返回非 0）",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = PROJECT_ROOT / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    started = time.perf_counter()
    classes = read_classes()
    dev = read_dev_sample(classes, args.dev_sample_size)
    negatives = read_negatives()
    seed = negatives[0]["seed"]

    label_distribution = dict(
        sorted(Counter(record["label"] for record in dev).items())
    )
    kind_distribution = dict(
        sorted(Counter(record["kind"] for record in negatives).items())
    )
    print(
        f"dev_sample={len(dev)} (file-order first N, seed-independent) "
        f"negatives={len(negatives)} seed={seed} classes={len(classes)}"
    )
    print(f"  dev label distribution: {label_distribution}")
    print(f"  negative kind distribution: {kind_distribution}")

    try:
        scorer = LocalFasttextScorer(classes)
    except Exception as exc:  # noqa: BLE001
        if args.allow_missing_model:
            print(f"ERROR: 模型不可用且指定 --allow-missing-model: {exc}")
            return 1
        print(f"ERROR: 本地 fastText 模型加载失败: {exc}")
        return 1

    positive_rows: list[dict[str, Any]] = []
    for index, record in enumerate(dev):
        scored = scorer.score(record["text"])
        positive_rows.append({**record, **scored})
        if (index + 1) % 500 == 0:
            print(f"  scored positives {index + 1}/{len(dev)}")
    negative_rows: list[dict[str, Any]] = []
    for index, record in enumerate(negatives):
        scored = scorer.score(record["text"])
        negative_rows.append({**record, **scored})
        if (index + 1) % 500 == 0:
            print(f"  scored negatives {index + 1}/{len(negatives)}")

    pos_max = np.array([row["max_score"] for row in positive_rows], dtype=float)
    pos_ent = np.array([row["entropy"] for row in positive_rows], dtype=float)
    neg_max = np.array([row["max_score"] for row in negative_rows], dtype=float)
    neg_ent = np.array([row["entropy"] for row in negative_rows], dtype=float)

    max_score_roc = build_single_feature_roc(pos_max, neg_max, reject_low=True)
    entropy_roc = build_single_feature_roc(pos_ent, neg_ent, reject_low=False)
    combined_roc = build_combined_roc(pos_max, pos_ent, neg_max, neg_ent)

    selection = select_thresholds(combined_roc, pos_max, pos_ent, neg_max, neg_ent)
    thresholds = selection["selected"]
    final_metrics = evaluate_rule(thresholds, pos_max, pos_ent, neg_max, neg_ent)

    # 选中阈值下的分类别拒识率（便于报告说明合成分布差异）。
    per_kind_negative: dict[str, Any] = {}
    for kind in NEGATIVE_KINDS:
        kind_rows = [row for row in negative_rows if row["kind"] == kind]
        rejected = sum(
            1
            for row in kind_rows
            if row["max_score"] < thresholds["max_score_threshold"]
            or row["entropy"] > thresholds["entropy_threshold"]
        )
        per_kind_negative[kind] = {
            "total": len(kind_rows),
            "rejected": rejected,
            "reject_rate": round(rejected / len(kind_rows), 6),
        }

    demo_title = "今天天气不错，适合出去走走"
    demo_scored = scorer.score(demo_title)
    demo_like = {
        "title": demo_title,
        "max_score": round(demo_scored["max_score"], 6),
        "entropy": round(demo_scored["entropy"], 6),
        "pred_class": demo_scored["pred_class"],
        "rejected": bool(
            demo_scored["max_score"] < thresholds["max_score_threshold"]
            or demo_scored["entropy"] > thresholds["entropy_threshold"]
        ),
    }

    meta = build_meta(
        dev_sample_size=len(dev),
        negative_sample_size=len(negatives),
        split="dev",
    )
    sampling = {
        "dev": {
            "source": "00-data/data/dev.txt",
            "method": "file_order_first_n",
            "sample_size": len(dev),
            "seed_independent": True,
            "label_distribution": label_distribution,
        },
        "negatives": {
            "source": "artifacts/b3/synthetic_negatives.jsonl",
            "method": "synthetic_template_combination",
            "generator": "tools/make_negatives.py",
            "seed": seed,
            "sample_size": len(negatives),
            "per_kind": NEGATIVES_PER_KIND,
            "kind_distribution": kind_distribution,
            "note": "合成负样本，非真实用户数据",
        },
    }
    distributions = {
        "positives": {
            "max_score": _dist_stats(pos_max),
            "entropy": _dist_stats(pos_ent),
        },
        "negatives": {
            "max_score": _dist_stats(neg_max),
            "entropy": _dist_stats(neg_ent),
        },
    }

    # ---- reject_roc.json ----
    roc_payload = {
        "meta": meta,
        "generated_by": "tools/eval_reject.py",
        "sampling": sampling,
        "features": {
            "max_score": {
                "definition": "10 类未校准分数的最大值",
                "reject_direction": "max_score < threshold 时拒识",
            },
            "entropy": {
                "definition": "归一化 10 类分数的 Shannon entropy",
                "base": ENTROPY_BASE,
                "theoretical_max": round(ENTROPY_MAX_NATS, 6),
                "reject_direction": "entropy > threshold 时拒识",
            },
        },
        "distributions": distributions,
        "rules": {
            "max_score": max_score_roc,
            "entropy": entropy_roc,
            "combined_or": {
                key: value
                for key, value in combined_roc.items()
                if key != "_all_rows"
            },
        },
    }
    roc_path = out_dir / "reject_roc.json"
    roc_path.write_text(
        json.dumps(roc_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    # ---- threshold_selection.json ----
    selection_payload = {
        "meta": meta,
        "generated_by": "tools/eval_reject.py",
        "seed": seed,
        "sampling": {
            "dev": {
                "method": "file_order_first_n",
                "sample_size": len(dev),
            },
            "negatives": {
                "method": "synthetic_template_combination",
                "generator": "tools/make_negatives.py",
                "sample_size": len(negatives),
                "per_kind": NEGATIVES_PER_KIND,
            },
            "total_sample_size": len(dev) + len(negatives),
            "positive_sample_size": len(dev),
            "negative_sample_size": len(negatives),
            "test_set_used": False,
        },
        "features": ["max_score", "entropy"],
        "entropy_base": ENTROPY_BASE,
        "rule": "max_score < max_score_threshold OR entropy > entropy_threshold",
        "selection_policy": {
            "priority": "recall_first",
            "description": (
                "固定『正样本保留率下限』后最大化负样本拒识召回；"
                "同分时优先更高正样本保留率，再要求两个特征都真正参与拒识"
                "（min 单特征负样本覆盖率更大），最后取阈值到分布边界余量更大者。"
            ),
            "positive_retention_floors": list(POSITIVE_RETENTION_FLOORS),
            "floor_rationale": (
                "0.95 档位无法拒识验收样例「今天天气不错」（entropy≈0.8067，"
                "高于该档位可选的 entropy 阈值），故主约束取仍能覆盖该样例的 0.94；"
                "更低档位只会增加正样本误拒，故不采用。"
            ),
            "used_constraint": selection["used_constraint"],
            "candidate_grid": combined_roc["grid"],
            "margin_note": (
                "margin<0 表示阈值落在负样本分布内部（合成负样本与 dev 正样本分布重叠），"
                "仅在召回/保留率同分时用于 tie-break，不代表阈值越界。"
            ),
            "test_set_used": False,
        },
        "selected": {
            **thresholds,
            "metrics": final_metrics,
            "per_kind_negative": per_kind_negative,
        },
        "operating_point_alternatives": [
            {
                "positive_retention_floor": floor,
                "best": _best_at_floor(combined_roc["_all_rows"], floor),
            }
            for floor in (0.99, 0.97, 0.95, 0.90, 0.85)
        ],
        "top_candidates": selection["top_candidates"],
        "rules_roc_summary": {
            "max_score": {
                "auc": max_score_roc["auc"],
                "point_count": max_score_roc["point_count"],
            },
            "entropy": {
                "auc": entropy_roc["auc"],
                "point_count": entropy_roc["point_count"],
            },
            "combined_or": {
                "auc": combined_roc["auc"],
                "point_count": combined_roc["point_count"],
            },
        },
        "demo_reference": demo_like,
        "artifacts": {
            "roc": "artifacts/b3/reject_roc.json",
            "threshold_selection": "artifacts/b3/threshold_selection.json",
            "negatives": "artifacts/b3/synthetic_negatives.jsonl",
        },
        "limitations": [
            "负样本为模板/组合合成的四类分布，不代表真实用户输入分布，结论须限定适用范围。",
            "仅使用 fastText 单模型未校准分数标定；分数未校准（calibrated=false），不跨模型比较。",
            "仅使用 dev 抽样 + 合成负样本，未使用 test 集，未做任何训练或重校准。",
        ],
    }
    selection_path = out_dir / "threshold_selection.json"
    selection_path.write_text(
        json.dumps(selection_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    elapsed = time.perf_counter() - started
    print("")
    print("=== ROC / AUC ===")
    print(f"  max_score : auc={max_score_roc['auc']} points={max_score_roc['point_count']}")
    print(f"  entropy   : auc={entropy_roc['auc']} points={entropy_roc['point_count']}")
    print(
        f"  combined  : auc={combined_roc['auc']} points={combined_roc['point_count']} "
        f"grid={combined_roc['grid']}"
    )
    print("=== selected thresholds (recall-first) ===")
    print(f"  max_score_threshold={thresholds['max_score_threshold']}")
    print(f"  entropy_threshold={thresholds['entropy_threshold']}")
    print(
        f"  negative_recall={final_metrics['negative_recall']} "
        f"({final_metrics['negative_rejected']}/{final_metrics['negative_total']})"
    )
    print(
        f"  positive_retention={final_metrics['positive_retention']} "
        f"({final_metrics['positive_kept']}/{final_metrics['positive_total']})"
    )
    print(f"  per_kind_negative={per_kind_negative}")
    print(
        f"  demo_reference {demo_like['title']!r}: rejected={demo_like['rejected']} "
        f"max_score={demo_like['max_score']} entropy={demo_like['entropy']} "
        f"pred_class={demo_like['pred_class']}"
    )
    print(f"OUT {roc_path}")
    print(f"OUT {selection_path}")
    print(f"elapsed={elapsed:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
