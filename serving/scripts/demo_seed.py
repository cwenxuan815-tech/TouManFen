#!/usr/bin/env python
"""D2：演示数据可重复种子脚本（``--reset`` 清库 + 灌入反馈/投票初态）。

安全边界（务必先读）
--------------------
* **必须显式设置 ``TMF_DB_PATH``**；未设置直接失败，绝不回退到默认
  ``serving/run/tmf.db``；
* 显式拒绝默认库 ``serving/run/tmf.db`` 及 ``serving/run/`` 下的任何路径；
  本脚本不读取、不写入用户的真实反馈/投票库；
* ``--reset`` 只清空/填充 **feedback / vote / vote_item / arena_model** 四张表，
  以及 ``arena_item`` 中本脚本自己写入的 ``demo-seed-%`` 占位题；
  **不 DROP 任何表**、不清其它表（例如未知扩展表保持原样）；
* SQLite 使用 WAL + 显式事务 + 参数化 SQL；表结构用 ``CREATE TABLE IF NOT
  EXISTS``，并在写入前核对列名（B4/B7 schema 漂移时直接失败，不静默写坏库）；
* 灌入内容全部为**演示种子**：
  - 3 条 ``status=pending``、``source=demo`` 的反馈（可演示反馈计数/hint）；
  - 2 条演示投票（每票 4 个本地模型参与、3 个对手；Elo 按 B7 同一公式
    K=24 顺序更新），全部模型票数 < 50，F10 仍是 ``warming_up`` 预热态；
  - **不伪造 50 票、不伪造准确率**；投票行没有 source 字段，故用
    ``client_id=demo-seed-vote-*`` / ``item_id=demo-seed-q*`` 明确标记为种子数据；
* 幂等：同一 DB 连续 ``--reset`` 后，删除并重建的行（含自增 id、
  ``arena_model`` 评分）完全一致；默认时间戳取运行时刻，若需逐字节可复现，
  传入固定 ``--at 2026-10-22T00:00:00+08:00``；
* 脚本结束前对比默认库 ``serving/run/tmf.db`` 的 stat，若被触达则报错退出。

用法（工作目录=工程根）::

    $env:TMF_DB_PATH = "$env:TEMP\\tmf-demo-seed\\tmf.db"
    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/demo_seed.py --reset

    # 可复现时间戳 + 报告
    ... serving/scripts/demo_seed.py --reset --at 2026-10-22T00:00:00+08:00 \
        --report artifacts/demo/demo_seed_report_2026-10-22.json

退出码：0 成功；2 用法/安全检查/校验失败；1 其它异常。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

try:  # Windows 控制台统一 UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = PROJECT_ROOT / "artifacts" / "demo"
BENCH_DIR = PROJECT_ROOT / "artifacts" / "bench"
SERVING_RUN_DIR = (PROJECT_ROOT / "serving" / "run").resolve()
DEFAULT_DB_PATH = (SERVING_RUN_DIR / "tmf.db").resolve()

LOCAL_MODELS = ("rf", "fasttext", "bert", "distill")
ARENA_MODELS = ("rf", "fasttext", "bert", "llm", "distill")
ELO_INITIAL = 1200.0
ELO_K = 24.0
WARMUP_MIN_VOTES = 50

#: 本脚本允许清理/填充的表（按依赖顺序删除，先子表）。
RESET_TABLES = ("vote_item", "vote", "feedback", "arena_model")
#: arena_item 只删除本脚本自己的占位题，避免清掉其它题目。
DEMO_ITEM_PREFIX = "demo-seed-"
DEMO_CLIENT_PREFIX = "demo-seed-"

#: 反馈种子（title 由 demo_cases 产物按 case_id 解析；score 不伪造 → NULL）。
FEEDBACK_SEEDS: tuple[dict[str, str], ...] = (
    {
        "client_id": "demo-seed-client-1",
        "case_id": "demo-correctable-entertainment-disagree",
        "model_id": "rf",
        "pred_class": "stocks",
        "corrected_class": "entertainment",
    },
    {
        "client_id": "demo-seed-client-2",
        "case_id": "demo-confusing-education-disagree",
        "model_id": "fasttext",
        "pred_class": "society",
        "corrected_class": "education",
    },
    {
        "client_id": "demo-seed-client-3",
        "case_id": "demo-correctable-entertainment-disagree",
        "model_id": "fasttext",
        "pred_class": "realty",
        "corrected_class": "entertainment",
    },
)

#: 演示投票：第 1 题胜者 bert、第 2 题胜者 distill（确定性，非真实用户投票）。
DEMO_VOTE_WINNERS = ("bert", "distill")


class DemoSeedError(RuntimeError):
    """安全/用法/schema 校验失败（退出码 2）。"""


# ---------------------------------------------------------------------------
# 表结构（与 B4 serving/services/feedback.py、B7 serving/services/arena.py 一致）
# 说明：本脚本只依赖标准库，故意不 import 服务模块，避免加载 yaml/pydantic 等
# 网关依赖；写入前会用 PRAGMA table_info 核对列名，schema 漂移直接失败。
# ---------------------------------------------------------------------------
FEEDBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS feedback (
    feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL DEFAULT '',
    model_id TEXT NOT NULL,
    pred_class TEXT NOT NULL,
    corrected_class TEXT NOT NULL,
    score REAL,
    source TEXT NOT NULL DEFAULT 'api',
    status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL,
    created_at_epoch REAL NOT NULL,
    UNIQUE (client_id, title, model_id)
);
CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at_epoch);
CREATE INDEX IF NOT EXISTS idx_feedback_status_created
    ON feedback(status, created_at_epoch);
CREATE INDEX IF NOT EXISTS idx_feedback_model ON feedback(model_id);
"""

ARENA_SCHEMA = """
CREATE TABLE IF NOT EXISTS arena_item (
    item_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    true_label INTEGER,
    candidates_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS arena_model (
    model_id TEXT PRIMARY KEY,
    rating REAL NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vote (
    vote_id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id TEXT NOT NULL,
    item_id TEXT NOT NULL REFERENCES arena_item(item_id),
    anon_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    pred_class TEXT NOT NULL,
    true_label INTEGER,
    created_at TEXT NOT NULL,
    created_at_epoch REAL NOT NULL,
    UNIQUE (client_id, item_id)
);
CREATE TABLE IF NOT EXISTS vote_item (
    vote_item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    vote_id INTEGER NOT NULL REFERENCES vote(vote_id) ON DELETE CASCADE,
    winner_model_id TEXT NOT NULL,
    opponent_model_id TEXT NOT NULL,
    winner_before REAL NOT NULL,
    winner_after REAL NOT NULL,
    opponent_before REAL NOT NULL,
    opponent_after REAL NOT NULL,
    expected_winner REAL NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_vote_item_vote ON vote_item(vote_id);
CREATE INDEX IF NOT EXISTS idx_vote_item_opponent ON vote_item(opponent_model_id);
CREATE INDEX IF NOT EXISTS idx_vote_model ON vote(model_id);
"""

EXPECTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "feedback": (
        "feedback_id",
        "client_id",
        "title",
        "normalized_title",
        "model_id",
        "pred_class",
        "corrected_class",
        "score",
        "source",
        "status",
        "created_at",
        "created_at_epoch",
    ),
    "arena_item": ("item_id", "title", "true_label", "candidates_json", "created_at"),
    "arena_model": ("model_id", "rating", "updated_at"),
    "vote": (
        "vote_id",
        "client_id",
        "item_id",
        "anon_id",
        "model_id",
        "pred_class",
        "true_label",
        "created_at",
        "created_at_epoch",
    ),
    "vote_item": (
        "vote_item_id",
        "vote_id",
        "winner_model_id",
        "opponent_model_id",
        "winner_before",
        "winner_after",
        "opponent_before",
        "opponent_after",
        "expected_winner",
        "created_at",
    ),
}


# ---------------------------------------------------------------------------
# Elo 纯函数（与 serving/services/arena.py 一致；本脚本独立实现用于安全种子）
# ---------------------------------------------------------------------------
def expected_score(rating_a: float, rating_b: float) -> float:
    return 1.0 / (1.0 + 10.0 ** ((float(rating_b) - float(rating_a)) / 400.0))


def apply_elo(winner: float, loser: float, k: float = ELO_K) -> tuple[float, float]:
    expected = expected_score(winner, loser)
    return float(winner) + float(k) * (1.0 - expected), float(loser) + float(k) * (
        expected - 1.0
    )


# ---------------------------------------------------------------------------
# 标题规范化（与 B4 normalize_title 等价：NFKC -> casefold -> 仅字母数字）
# ---------------------------------------------------------------------------
def normalize_title(title: str) -> str:
    import unicodedata

    text = unicodedata.normalize("NFKC", title).casefold()
    return "".join(char for char in text if char.isalnum())


# ---------------------------------------------------------------------------
# 安全路径
# ---------------------------------------------------------------------------
def resolve_db_path() -> Path:
    raw = os.environ.get("TMF_DB_PATH", "").strip()
    if not raw:
        raise DemoSeedError(
            "必须显式设置 TMF_DB_PATH（例如临时目录），本脚本拒绝默认库 "
            "serving/run/tmf.db"
        )
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (Path.cwd() / path).resolve()
    else:
        path = path.resolve()
    if path == DEFAULT_DB_PATH:
        raise DemoSeedError(
            f"拒绝使用默认库 {DEFAULT_DB_PATH}；请把 TMF_DB_PATH 指向临时/演示库"
        )
    try:
        path.relative_to(SERVING_RUN_DIR)
    except ValueError:
        pass
    else:
        raise DemoSeedError(
            f"TMF_DB_PATH 不允许位于 serving/run/（保护真实库）: {path}"
        )
    if path.is_dir():
        raise DemoSeedError(f"TMF_DB_PATH 不能是目录: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _stat_or_none(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_mtime_ns, stat.st_size


# ---------------------------------------------------------------------------
# 输入产物读取
# ---------------------------------------------------------------------------
def latest_artifact(pattern: str, description: str) -> Path:
    candidates = [
        path
        for path in DEMO_DIR.glob(pattern)
        if path.is_file() and not path.is_symlink()
    ]
    if not candidates:
        raise DemoSeedError(
            f"缺少{description}: {DEMO_DIR.name}/{pattern}（请先运行 "
            "tools/make_demo_data.py）"
        )
    return max(candidates, key=lambda path: path.stat().st_mtime)


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DemoSeedError(f"文件不存在: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DemoSeedError(f"JSON 解析失败: {path}: {exc}") from exc


def load_demo_cases(path: Path) -> list[dict[str, Any]]:
    document = load_json(path)
    cases = document.get("cases")
    if not isinstance(cases, list) or len(cases) != 8:
        raise DemoSeedError(f"演示标题产物必须含 8 条 cases: {path}")
    return [case for case in cases if isinstance(case, dict)]


def load_arena_questions(path: Path) -> list[dict[str, Any]]:
    document = load_json(path)
    questions = document.get("questions")
    if not isinstance(questions, list) or len(questions) != 20:
        raise DemoSeedError(f"竞技场题产物必须含 20 道题: {path}")
    return [question for question in questions if isinstance(question, dict)]


def load_prediction(model_id: str) -> list[dict[str, Any]]:
    path = BENCH_DIR / f"_pred_{model_id}_test.jsonl"
    if not path.is_file():
        raise DemoSeedError(f"缺少 B8 预测文件: {path}")
    try:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except json.JSONDecodeError as exc:
        raise DemoSeedError(f"B8 预测文件不可读: {path}: {exc}") from exc


# ---------------------------------------------------------------------------
# DB 初始化 / 清理
# ---------------------------------------------------------------------------
def open_db(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    except sqlite3.Error as exc:
        raise DemoSeedError(f"无法连接数据库 {path}: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def ensure_schema(conn: sqlite3.Connection) -> None:
    try:
        conn.executescript(FEEDBACK_SCHEMA)
        conn.executescript(ARENA_SCHEMA)
    except sqlite3.Error as exc:
        raise DemoSeedError(f"创建表结构失败: {exc}") from exc
    for table, columns in EXPECTED_COLUMNS.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        actual = [str(row[1]) for row in rows]
        if not actual:
            raise DemoSeedError(f"表 {table} 不存在或不可读")
        if set(actual) != set(columns):
            missing = sorted(set(columns) - set(actual))
            extra = sorted(set(actual) - set(columns))
            raise DemoSeedError(
                f"表 {table} 结构与 B4/B7 schema 不一致（missing={missing}, extra={extra}）；"
                "为避免写坏库，脚本中止"
            )


def reset_rows(conn: sqlite3.Connection) -> dict[str, int]:
    """按依赖顺序清空允许清理的行；不 DROP 表。返回每表删除行数。"""
    removed: dict[str, int] = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        demo_items = conn.execute(
            "SELECT item_id FROM arena_item WHERE item_id LIKE ?",
            (f"{DEMO_ITEM_PREFIX}%",),
        ).fetchall()
        removed["arena_item_demo"] = len(demo_items)
        for table in RESET_TABLES:
            cursor = conn.execute(f"DELETE FROM {table}")
            removed[table] = (
                int(cursor.rowcount) if cursor.rowcount is not None else 0
            )
        conn.execute(
            "DELETE FROM arena_item WHERE item_id LIKE ?",
            (f"{DEMO_ITEM_PREFIX}%",),
        )
        # 让自增 id 从头开始，保证 --reset 幂等（AUTOINCREMENT 不会自动复用 id）。
        sequence_rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'"
        ).fetchone()
        if sequence_rows is not None:
            names = ("feedback", "vote", "vote_item")
            conn.executemany(
                "DELETE FROM sqlite_sequence WHERE name = ?",
                [(name,) for name in names],
            )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise DemoSeedError(f"清库失败: {exc}") from exc
    return removed


# ---------------------------------------------------------------------------
# 灌入演示数据
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SeedContext:
    demo_cases_path: Path
    arena_questions_path: Path
    demo_cases_by_id: dict[str, dict[str, Any]]
    questions: list[dict[str, Any]]
    predictions: dict[str, list[dict[str, Any]]]
    base_iso: str
    base_epoch: float


def build_seed_context(
    demo_cases_path: Path,
    arena_questions_path: Path,
    base_epoch: float,
) -> SeedContext:
    demo_cases = load_demo_cases(demo_cases_path)
    by_id = {str(case.get("case_id")): case for case in demo_cases}
    for seed in FEEDBACK_SEEDS:
        case = by_id.get(seed["case_id"])
        if case is None:
            raise DemoSeedError(f"演示标题产物缺少 case: {seed['case_id']}")
        expected = (case.get("expected_model_preds") or {}).get(seed["model_id"])
        if expected != seed["pred_class"]:
            raise DemoSeedError(
                f"反馈种子与 B8 预测不一致: {seed['case_id']} ({seed['model_id']}) "
                f"{expected!r} != {seed['pred_class']!r}"
            )
    questions = load_arena_questions(arena_questions_path)
    predictions = {model_id: load_prediction(model_id) for model_id in LOCAL_MODELS}
    for question in questions:
        index = int(question["line_index"])
        for model_id in LOCAL_MODELS:
            records = predictions[model_id]
            if index >= len(records):
                raise DemoSeedError(f"B8 预测行号越界: {model_id} index={index}")
            if records[index].get("true_label") != question["true_label"]:
                raise DemoSeedError(
                    f"竞技场题真值与 B8 预测不一致: {question['question_id']} ({model_id})"
                )
    base = datetime.fromtimestamp(base_epoch, tz=timezone.utc)
    return SeedContext(
        demo_cases_path=demo_cases_path,
        arena_questions_path=arena_questions_path,
        demo_cases_by_id=by_id,
        questions=questions,
        predictions=predictions,
        base_iso=base.isoformat(),
        base_epoch=float(base_epoch),
    )


def _iso_at(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def seed_feedback(conn: sqlite3.Connection, context: SeedContext) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for offset, seed in enumerate(FEEDBACK_SEEDS):
            case = context.demo_cases_by_id[seed["case_id"]]
            title = str(case["title"])
            created_epoch = context.base_epoch - 180.0 + offset * 60.0
            cursor = conn.execute(
                "INSERT INTO feedback"
                " (client_id, title, normalized_title, model_id, pred_class,"
                "  corrected_class, score, source, status, created_at,"
                "  created_at_epoch)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    seed["client_id"],
                    title,
                    normalize_title(title),
                    seed["model_id"],
                    seed["pred_class"],
                    seed["corrected_class"],
                    None,  # 不伪造模型分数
                    "demo",
                    "pending",
                    _iso_at(created_epoch),
                    created_epoch,
                ),
            )
            rows.append(
                {
                    "feedback_id": int(cursor.lastrowid or 0),
                    "client_id": seed["client_id"],
                    "case_id": seed["case_id"],
                    "model_id": seed["model_id"],
                    "pred_class": seed["pred_class"],
                    "corrected_class": seed["corrected_class"],
                    "source": "demo",
                    "status": "pending",
                }
            )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise DemoSeedError(f"写入演示反馈失败: {exc}") from exc
    return rows


def seed_arena(conn: sqlite3.Connection, context: SeedContext) -> dict[str, Any]:
    if len(context.questions) < len(DEMO_VOTE_WINNERS):
        raise DemoSeedError("竞技场题数量不足，无法灌入演示投票")
    ratings = {model_id: ELO_INITIAL for model_id in ARENA_MODELS}
    items: list[dict[str, Any]] = []
    votes: list[dict[str, Any]] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for model_id in ARENA_MODELS:
            conn.execute(
                "INSERT INTO arena_model (model_id, rating, updated_at) VALUES (?, ?, ?)",
                (model_id, ELO_INITIAL, context.base_iso),
            )
        for order, winner in enumerate(DEMO_VOTE_WINNERS, start=1):
            question = context.questions[order - 1]
            index = int(question["line_index"])
            item_id = f"{DEMO_ITEM_PREFIX}q{order:02d}"
            candidates = [
                {
                    "anon_id": f"anon-demo-{order:02d}-{model_id}",
                    "model_id": model_id,
                    "pred_class": context.predictions[model_id][index]["pred_class"],
                    "latency_ms": 0,
                }
                for model_id in LOCAL_MODELS
            ]
            if winner not in LOCAL_MODELS:
                raise DemoSeedError(f"演示投票胜者必须是本地模型: {winner}")
            conn.execute(
                "INSERT INTO arena_item (item_id, title, true_label, candidates_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    item_id,
                    str(question["title"]),
                    int(question["true_label_index"]),
                    json.dumps(candidates, ensure_ascii=False),
                    context.base_iso,
                ),
            )
            items.append(
                {
                    "item_id": item_id,
                    "question_id": question["question_id"],
                    "line_index": index,
                    "true_label": question["true_label"],
                    "winner": winner,
                }
            )

            opponents = sorted(model_id for model_id in LOCAL_MODELS if model_id != winner)
            winner_pred = context.predictions[winner][index]["pred_class"]
            winner_before = ratings[winner]
            matches: list[tuple[str, float, float, float, float, float]] = []
            for opponent in opponents:
                opponent_before = ratings[opponent]
                expected = expected_score(winner_before, opponent_before)
                winner_after, opponent_after = apply_elo(winner_before, opponent_before)
                matches.append(
                    (
                        opponent,
                        winner_before,
                        winner_after,
                        opponent_before,
                        opponent_after,
                        expected,
                    )
                )
                ratings[opponent] = opponent_after
                winner_before = winner_after
            winner_after_final = winner_before
            ratings[winner] = winner_after_final

            vote_epoch = context.base_epoch - 30.0 + order * 10.0
            cursor = conn.execute(
                "INSERT INTO vote"
                " (client_id, item_id, anon_id, model_id, pred_class, true_label,"
                "  created_at, created_at_epoch)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    f"{DEMO_CLIENT_PREFIX}vote-{order}",
                    item_id,
                    f"anon-demo-{order:02d}-{winner}",
                    winner,
                    winner_pred,
                    int(question["true_label_index"]),
                    _iso_at(vote_epoch),
                    vote_epoch,
                ),
            )
            vote_id = int(cursor.lastrowid or 0)
            for (
                opponent,
                before,
                after,
                opponent_before,
                opponent_after,
                expected,
            ) in matches:
                conn.execute(
                    "INSERT INTO vote_item"
                    " (vote_id, winner_model_id, opponent_model_id, winner_before,"
                    "  winner_after, opponent_before, opponent_after, expected_winner,"
                    "  created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        vote_id,
                        winner,
                        opponent,
                        before,
                        after,
                        opponent_before,
                        opponent_after,
                        expected,
                        _iso_at(vote_epoch),
                    ),
                )
            votes.append(
                {
                    "vote_id": vote_id,
                    "client_id": f"{DEMO_CLIENT_PREFIX}vote-{order}",
                    "item_id": item_id,
                    "model_id": winner,
                    "pred_class": winner_pred,
                    "games_applied": len(opponents),
                }
            )
        for model_id, rating in ratings.items():
            conn.execute(
                "UPDATE arena_model SET rating = ?, updated_at = ? WHERE model_id = ?",
                (float(rating), context.base_iso, model_id),
            )
        conn.execute("COMMIT")
    except sqlite3.Error as exc:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise DemoSeedError(f"写入演示投票失败: {exc}") from exc
    return {"items": items, "votes": votes, "ratings": ratings}


# ---------------------------------------------------------------------------
# 状态快照 / 幂等摘要
# ---------------------------------------------------------------------------
def _json_default(value: Any) -> Any:
    return value


def state_snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    feedback = [
        dict(row)
        for row in conn.execute(
            "SELECT feedback_id, client_id, title, normalized_title, model_id,"
            " pred_class, corrected_class, score, source, status"
            " FROM feedback ORDER BY feedback_id"
        ).fetchall()
    ]
    arena_item = [
        dict(row)
        for row in conn.execute(
            "SELECT item_id, title, true_label, candidates_json"
            " FROM arena_item ORDER BY item_id"
        ).fetchall()
    ]
    arena_model = [
        dict(row)
        for row in conn.execute(
            "SELECT model_id, rating FROM arena_model ORDER BY model_id"
        ).fetchall()
    ]
    votes = [
        dict(row)
        for row in conn.execute(
            "SELECT vote_id, client_id, item_id, anon_id, model_id, pred_class,"
            " true_label FROM vote ORDER BY vote_id"
        ).fetchall()
    ]
    vote_items = [
        dict(row)
        for row in conn.execute(
            "SELECT vote_item_id, vote_id, winner_model_id, opponent_model_id,"
            " winner_before, winner_after, opponent_before, opponent_after,"
            " expected_winner FROM vote_item ORDER BY vote_item_id"
        ).fetchall()
    ]
    return {
        "feedback": feedback,
        "arena_item": arena_item,
        "arena_model": arena_model,
        "vote": votes,
        "vote_item": vote_items,
    }


def state_digest(state: dict[str, Any]) -> str:
    payload = json.dumps(state, ensure_ascii=False, sort_keys=True, default=_json_default)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ("feedback", "arena_item", "arena_model", "vote", "vote_item"):
        row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
        counts[table] = int(row["n"]) if row is not None else 0
    return counts


def replay_elo(conn: sqlite3.Connection) -> dict[str, float]:
    """从 vote_item 按 (vote_id, vote_item_id) 顺序复算 Elo，返回最终评分。"""
    ratings = {model_id: ELO_INITIAL for model_id in ARENA_MODELS}
    for row in conn.execute(
        "SELECT vote_id, winner_model_id, opponent_model_id, winner_before,"
        " winner_after, opponent_before, opponent_after, expected_winner"
        " FROM vote_item ORDER BY vote_id, vote_item_id"
    ).fetchall():
        winner = str(row["winner_model_id"])
        opponent = str(row["opponent_model_id"])
        if winner not in ratings or opponent not in ratings:
            raise DemoSeedError(f"vote_item 引用未知模型: {winner}/{opponent}")
        before_winner = ratings[winner]
        before_opponent = ratings[opponent]
        expected = expected_score(before_winner, before_opponent)
        after_winner, after_opponent = apply_elo(before_winner, before_opponent)
        if abs(float(row["winner_before"]) - before_winner) > 1e-9:
            raise DemoSeedError("Elo 复算失败：winner_before 不一致")
        if abs(float(row["opponent_before"]) - before_opponent) > 1e-9:
            raise DemoSeedError("Elo 复算失败：opponent_before 不一致")
        if abs(float(row["expected_winner"]) - expected) > 1e-12:
            raise DemoSeedError("Elo 复算失败：expected_winner 不一致")
        if abs(float(row["winner_after"]) - after_winner) > 1e-9:
            raise DemoSeedError("Elo 复算失败：winner_after 不一致")
        if abs(float(row["opponent_after"]) - after_opponent) > 1e-9:
            raise DemoSeedError("Elo 复算失败：opponent_after 不一致")
        ratings[winner] = after_winner
        ratings[opponent] = after_opponent
    return ratings


def write_report(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def run_reset(db_path: Path, base_epoch: float, report_path: Path | None) -> dict[str, Any]:
    default_before = _stat_or_none(DEFAULT_DB_PATH)
    demo_cases_path = latest_artifact("demo_cases_*.json", "演示标题产物")
    arena_path = latest_artifact("arena_questions_*.json", "竞技场题产物")
    context = build_seed_context(demo_cases_path, arena_path, base_epoch)

    conn = open_db(db_path)
    try:
        ensure_schema(conn)
        removed = reset_rows(conn)
        feedback_rows = seed_feedback(conn, context)
        arena_seed = seed_arena(conn, context)
        state = state_snapshot(conn)
        counts = table_counts(conn)
        replay = replay_elo(conn)
        stored_ratings = {
            str(row["model_id"]): float(row["rating"])
            for row in conn.execute("SELECT model_id, rating FROM arena_model").fetchall()
        }
        for model_id, rating in replay.items():
            if abs(stored_ratings.get(model_id, float("nan")) - rating) > 1e-9:
                raise DemoSeedError(
                    f"Elo 回放与 arena_model 不一致: {model_id} "
                    f"{stored_ratings.get(model_id)} != {rating}"
                )
        digest = state_digest(state)
        total_votes = counts["vote"]
        if total_votes >= WARMUP_MIN_VOTES:
            raise DemoSeedError(
                f"演示投票数必须 < {WARMUP_MIN_VOTES}（不伪造正式榜单），实际 {total_votes}"
            )
    finally:
        conn.close()

    default_after = _stat_or_none(DEFAULT_DB_PATH)
    if default_after != default_before:
        raise DemoSeedError(
            "检测到默认库 serving/run/tmf.db 被触达（stat 变化），脚本异常中止；"
            f"before={default_before}, after={default_after}"
        )

    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_by": "serving/scripts/demo_seed.py",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path),
        "default_db_protected": {
            "path": str(DEFAULT_DB_PATH),
            "touched": False,
            "before": default_before,
            "after": default_after,
        },
        "inputs": {
            "demo_cases_artifact": str(demo_cases_path.relative_to(PROJECT_ROOT)),
            "arena_questions_artifact": str(arena_path.relative_to(PROJECT_ROOT)),
            "b8_prediction_dir": "artifacts/bench",
            "base_epoch": base_epoch,
            "base_iso": context.base_iso,
        },
        "removed_rows": removed,
        "counts": counts,
        "seeded_feedback": feedback_rows,
        "seeded_arena": arena_seed,
        "arena_ratings": stored_ratings,
        "warmup": {
            "total_votes": total_votes,
            "warmup_threshold": WARMUP_MIN_VOTES,
            "state": "warming_up",
            "note": "演示种子票数远小于 50，榜单保持预热态；不伪造正式排名/准确率",
        },
        "idempotency": {
            "state_digest_semantic": digest,
            "note": (
                "state_digest 不含 created_at/updated_at；同一 DB 连续 --reset 后"
                "语义状态一致；传固定 --at 时时间戳也一致"
            ),
        },
    }
    if report_path is not None:
        write_report(report_path, report)
        report["report_path"] = str(report_path.relative_to(PROJECT_ROOT))
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="D2 演示数据种子：--reset 清空并灌入 feedback/arena 演示初态"
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="必须显式指定：清空 feedback/vote/vote_item/arena_model 后灌入演示数据",
    )
    parser.add_argument(
        "--at",
        default=None,
        help="固定时间戳（ISO 8601 带时区，可选；用于逐字节可复现）",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="可选：把本次灌入结果写入 JSON 报告（相对路径按工程根解析）",
    )
    args = parser.parse_args(argv)

    if not args.reset:
        print(
            "DEMO_SEED_ERROR: 必须显式提供 --reset（本脚本只用于重置演示库，不做增量写入）",
            file=sys.stderr,
        )
        return 2

    try:
        db_path = resolve_db_path()
        if args.at:
            try:
                fixed = datetime.fromisoformat(args.at)
            except ValueError as exc:
                raise DemoSeedError(f"--at 不是合法 ISO 8601: {args.at}") from exc
            if fixed.tzinfo is None or fixed.utcoffset() is None:
                raise DemoSeedError("--at 必须带显式时区偏移（如 +08:00）")
            base_epoch = fixed.timestamp()
        else:
            base_epoch = datetime.now(timezone.utc).timestamp()
        report_path: Path | None = None
        if args.report:
            report_path = Path(args.report)
            if not report_path.is_absolute():
                report_path = PROJECT_ROOT / report_path
        report = run_reset(db_path, base_epoch, report_path)
    except DemoSeedError as exc:
        print(f"DEMO_SEED_ERROR: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - 非预期 IO 等
        print(f"DEMO_SEED_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    counts = report["counts"]
    print(
        "DEMO_SEED_OK "
        f"db={report['db_path']} "
        f"feedback={counts['feedback']} vote={counts['vote']} "
        f"vote_item={counts['vote_item']} arena_model={counts['arena_model']} "
        f"arena_item={counts['arena_item']} "
        f"state={report['warmup']['state']} "
        f"digest={report['idempotency']['state_digest_semantic'][:16]}"
    )
    print(f"  default_db_protected=serving/run/tmf.db (touched={report['default_db_protected']['touched']})")
    if report.get("report_path"):
        print(f"  report={report['report_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
