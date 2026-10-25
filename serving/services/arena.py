"""竞技场投票业务服务（B7）。

职责
----
- ``/arena/next``：从 ``00-data/data/test.txt`` 随机抽取一条带标签样本，经由
  ``ModelAdapter`` 协议调用已装配的真实模型（本模块不加载模型、不访问网络），
  服务端打乱候选顺序并分配随机 ``anon_id``；``item_id`` 不可猜；
- ``/arena/vote``：校验 item/anon，按 ``client_id + item_id`` 去重，单客户
  每分钟 ≤20 票限速；落库到 SQLite 的 ``vote`` / ``vote_item``，并按 Elo
  （初始 1200、K=24）让被选中的模型对其余候选逐个更新；
- ``/arena/leaderboard``：返回 Elo、票数、Wilson 区间；票数 <50 标
  ``warming_up`` 且不给正式 rank。

边界
----
- 只用标准库 ``sqlite3``；WAL + busy_timeout + foreign_keys；参数化 SQL；
- 表全部用 ``CREATE TABLE IF NOT EXISTS``，不 DROP 其他表（与 B4 ``feedback``
  共用 ``serving/run/tmf.db``）；
- 默认库路径 ``serving/run/tmf.db``，可用环境变量 ``TMF_DB_PATH`` 覆盖；
- 日志不记录新闻标题；``/arena/next`` 相关日志不记录 model_id / display_name /
  model_version（匿名性由服务端 SQLite 的 ``arena_item.candidates_json`` 维护）；
- Elo 只表示人类偏好强度，不是准确率或客观排名。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import re
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from ..adapters.protocol import AdapterResult, ModelAdapter
from ..gateway.errors import (
    ApiError,
    ERROR_ARENA_ANON_INVALID,
    ERROR_ARENA_DUPLICATE_VOTE,
    ERROR_ARENA_ITEM_NOT_FOUND,
    ERROR_ARENA_NO_ITEMS,
    ERROR_ARENA_RATE_LIMITED,
    ERROR_ARENA_STORE_ERROR,
    ERROR_ARENA_UNAVAILABLE,
    ERROR_INVALID_REQUEST,
    ERROR_MODEL_ERROR,
)
from ..gateway.settings import SERVING_DIR
from ..models.registry import ModelRegistry

# ---------------------------------------------------------------------------
# 常量（与 docs/工程实施工作清单_75工时.md §4.3 / B7 一致）
# ---------------------------------------------------------------------------
ELO_INITIAL = 1200.0
ELO_K = 24.0
WARMUP_MIN_VOTES = 50
RATE_LIMIT_VOTES = 20
RATE_LIMIT_WINDOW_S = 60.0
WILSON_Z = 1.96

#: 竞技场题目来源（带标签）。可用环境变量 ``TMF_ARENA_TEST_FILE`` 覆盖。
DEFAULT_TEST_FILE = SERVING_DIR.parent / "00-data" / "data" / "test.txt"
#: 默认 SQLite 路径（serving/run 已在 .gitignore 中）。
DEFAULT_DB_PATH = SERVING_DIR / "run" / "tmf.db"

_ITEM_ID_BYTES = 16
_ANON_ID_BYTES = 9
_MAX_CLIENT_ID_CHARS = 64
_MAX_OPAQUE_ID_CHARS = 128
_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

LEADERBOARD_NOTE = "人类偏好投票强度，仅作演示参考；票数可被本机清除。"
LEADERBOARD_DATA_KIND = "vote"


# ---------------------------------------------------------------------------
# 纯函数：Elo / Wilson（可被验收脚本直接单测，不依赖 DB / 网络）
# ---------------------------------------------------------------------------
def expected_score(rating_a: float, rating_b: float) -> float:
    """标准 Elo 期望得分：``1 / (1 + 10 ** ((Rb - Ra) / 400))``。"""
    return 1.0 / (1.0 + 10.0 ** ((float(rating_b) - float(rating_a)) / 400.0))


def apply_elo(
    winner: float, loser: float, k: float = ELO_K
) -> tuple[float, float]:
    """一局胜/负后的新 Elo：返回 ``(winner_after, loser_after)``。

    ``winner_after = winner + K * (1 - E)``；
    ``loser_after = loser + K * (E - 1)``，其中 ``E = expected_score(winner, loser)``。
    一场对局双方评分变化量之和为 0，便于审计。
    """
    expected = expected_score(winner, loser)
    winner_after = float(winner) + float(k) * (1.0 - expected)
    loser_after = float(loser) + float(k) * (expected - 1.0)
    return winner_after, loser_after


def wilson_interval(wins: int, n: int, z: float = WILSON_Z) -> tuple[float, float]:
    """二项比例的 Wilson 置信区间（默认 95%），结果裁剪到 [0, 1]。

    ``n == 0`` 时无观测，返回 ``(0.0, 1.0)``（不暗示任何倾向）。
    """
    if n <= 0:
        return 0.0, 1.0
    p = wins / n
    denominator = 1.0 + z * z / n
    center = (p + z * z / (2.0 * n)) / denominator
    margin = (
        z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n)
    ) / denominator
    lower = max(0.0, center - margin)
    upper = min(1.0, center + margin)
    if lower > upper:  # 浮点极端情况下的兜底，保证契约 lower <= upper
        lower, upper = upper, lower
    return lower, upper


# ---------------------------------------------------------------------------
# 结果数据类（router 只做字段映射）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PublicCandidate:
    """下发到前端的匿名候选：不含任何模型身份。"""

    anon_id: str
    pred_class: str
    latency_ms: int


@dataclass(frozen=True)
class ArenaNextOutcome:
    item_id: str
    title: str
    candidates: tuple[PublicCandidate, ...]
    partial: bool


@dataclass(frozen=True)
class ArenaVoteOutcome:
    vote_id: int
    model_id: str
    display_name: str
    pred_class: str
    elo_after: float
    games_applied: int


@dataclass(frozen=True)
class LeaderboardEntry:
    model_id: str
    display_name: str
    elo: float
    #: 用户实际选择该模型的次数。
    votes: int
    #: 该模型参与的投票题数量；Wilson 区间与预热门槛使用此样本量。
    sample_size: int
    wins: int
    losses: int
    win_rate: float | None
    wilson_lower: float
    wilson_upper: float
    warming_up: bool
    rank: int | None


@dataclass(frozen=True)
class ArenaLeaderboardOutcome:
    status: str
    entries: tuple[LeaderboardEntry, ...]
    min_votes_for_ranking: int


# ---------------------------------------------------------------------------
# 限速器：仅进程内内存（§4.3：不依赖 IP/验证码；重启清零）
# ---------------------------------------------------------------------------
class RateLimiter:
    """单 key 滑动窗口限速：窗口内最多 ``max_votes`` 票。"""

    def __init__(
        self,
        max_votes: int = RATE_LIMIT_VOTES,
        window_s: float = RATE_LIMIT_WINDOW_S,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._max_votes = max_votes
        self._window_s = window_s
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}

    def hit(self, key: str) -> bool:
        """尝试消耗一个配额；允许返回 True 并记录，超限返回 False。"""
        now = self._clock()
        cutoff = now - self._window_s
        with self._lock:
            timestamps = self._hits.setdefault(key, [])
            while timestamps and timestamps[0] <= cutoff:
                timestamps.pop(0)
            if len(timestamps) >= self._max_votes:
                return False
            timestamps.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


# ---------------------------------------------------------------------------
# SQLite 存储
# ---------------------------------------------------------------------------
class ArenaStoreError(RuntimeError):
    """竞技场 SQLite 读写失败（对外统一映射为 503，不泄露堆栈）。"""


class DuplicateVoteError(RuntimeError):
    """同一 client_id + item_id 已投过票。"""


_SCHEMA = """
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


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ArenaStore:
    """竞技场 SQLite 存储（短连接 + 显式事务，线程安全由数据库锁保证）。"""

    def __init__(self, db_path: str | Path) -> None:
        self._db_path = Path(db_path)
        self._closed = False
        self._initialize()

    @property
    def db_path(self) -> Path:
        return self._db_path

    # -- 连接与初始化 ------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise ArenaStoreError("竞技场数据库已关闭")
        try:
            conn = sqlite3.connect(
                str(self._db_path), timeout=5.0, isolation_level=None
            )
        except sqlite3.Error as exc:  # pragma: no cover - 由上层统一映射
            raise ArenaStoreError("无法连接竞技场数据库") from exc
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
        except sqlite3.Error as exc:
            conn.close()
            raise ArenaStoreError("竞技场数据库配置失败") from exc
        return conn

    def _initialize(self) -> None:
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self._db_path), timeout=5.0, isolation_level=None
            )
        except (sqlite3.Error, OSError) as exc:
            raise ArenaStoreError("竞技场数据库初始化失败") from exc
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise ArenaStoreError("竞技场数据库初始化失败") from exc
        finally:
            conn.close()

    def close(self) -> None:
        self._closed = True

    # -- 写入 / 读取 -------------------------------------------------------
    def ensure_models(self, model_ids: Sequence[str]) -> None:
        """保证每个参赛模型在 ``arena_model`` 中有一行（初始 1200，不覆盖已有值）。"""
        now = _utc_now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            for model_id in sorted(model_ids):
                conn.execute(
                    "INSERT OR IGNORE INTO arena_model (model_id, rating, updated_at)"
                    " VALUES (?, ?, ?)",
                    (model_id, ELO_INITIAL, now),
                )
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            _rollback_quietly(conn)
            raise ArenaStoreError("初始化模型 Elo 失败") from exc
        finally:
            conn.close()

    def save_item(
        self,
        *,
        item_id: str,
        title: str,
        true_label: int,
        candidates: Sequence[Mapping[str, object]],
    ) -> None:
        """保存题目与「anon_id -> 真实模型」服务端映射（仅服务端可见）。"""
        candidates_json = json.dumps(list(candidates), ensure_ascii=False)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO arena_item"
                " (item_id, title, true_label, candidates_json, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (item_id, title, int(true_label), candidates_json, _utc_now_iso()),
            )
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            _rollback_quietly(conn)
            raise ArenaStoreError("竞技场题目写入冲突") from exc
        except sqlite3.Error as exc:
            _rollback_quietly(conn)
            raise ArenaStoreError("竞技场题目写入失败") from exc
        finally:
            conn.close()

    def get_item(self, item_id: str) -> dict[str, object] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT item_id, title, true_label, candidates_json"
                " FROM arena_item WHERE item_id = ?",
                (item_id,),
            ).fetchone()
            if row is None:
                return None
            try:
                candidates = json.loads(row["candidates_json"])
            except (TypeError, ValueError) as exc:
                raise ArenaStoreError("竞技场题目数据损坏") from exc
            if not isinstance(candidates, list):
                raise ArenaStoreError("竞技场题目数据损坏")
            return {
                "item_id": row["item_id"],
                "title": row["title"],
                "true_label": row["true_label"],
                "candidates": candidates,
            }
        except sqlite3.Error as exc:
            raise ArenaStoreError("竞技场题目读取失败") from exc
        finally:
            conn.close()

    def has_vote(self, client_id: str, item_id: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT 1 FROM vote WHERE client_id = ? AND item_id = ? LIMIT 1",
                (client_id, item_id),
            ).fetchone()
            return row is not None
        except sqlite3.Error as exc:
            raise ArenaStoreError("竞技场投票读取失败") from exc
        finally:
            conn.close()

    def record_vote(
        self,
        *,
        client_id: str,
        item_id: str,
        anon_id: str,
        model_id: str,
        pred_class: str,
        true_label: int | None,
        opponent_model_ids: Sequence[str],
        k: float = ELO_K,
    ) -> dict[str, object]:
        """在一个事务内：去重 -> 写 vote -> 逐对手更新 Elo -> 写 vote_item。

        ``opponent_model_ids`` 由调用方按 ``model_id`` 升序给定，保证同一票的
        Elo 更新顺序确定、可从头复算。
        返回 ``{"vote_id", "elo_after", "games_applied"}``。
        """
        opponents = list(opponent_model_ids)
        now_iso = _utc_now_iso()
        now_epoch = time.time()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT vote_id FROM vote WHERE client_id = ? AND item_id = ?",
                (client_id, item_id),
            ).fetchone()
            if existing is not None:
                raise DuplicateVoteError()
            involved = [model_id, *opponents]
            for involved_id in sorted(set(involved)):
                conn.execute(
                    "INSERT OR IGNORE INTO arena_model (model_id, rating, updated_at)"
                    " VALUES (?, ?, ?)",
                    (involved_id, ELO_INITIAL, now_iso),
                )
            placeholders = ", ".join("?" for _ in involved)
            rating_rows = conn.execute(
                f"SELECT model_id, rating FROM arena_model WHERE model_id IN ({placeholders})",
                tuple(involved),
            ).fetchall()
            ratings = {
                row["model_id"]: float(row["rating"]) for row in rating_rows
            }
            if model_id not in ratings:
                raise ArenaStoreError("模型 Elo 状态缺失")
            winner_rating = ratings[model_id]
            matches: list[tuple[str, float, float, float, float, float]] = []
            for opponent_id in opponents:
                if opponent_id not in ratings:
                    raise ArenaStoreError("模型 Elo 状态缺失")
                opponent_before = ratings[opponent_id]
                expected = expected_score(winner_rating, opponent_before)
                winner_after, opponent_after = apply_elo(
                    winner_rating, opponent_before, k
                )
                matches.append(
                    (
                        opponent_id,
                        winner_rating,
                        winner_after,
                        opponent_before,
                        opponent_after,
                        expected,
                    )
                )
                ratings[opponent_id] = opponent_after
                winner_rating = winner_after

            cursor = conn.execute(
                "INSERT INTO vote"
                " (client_id, item_id, anon_id, model_id, pred_class, true_label,"
                "  created_at, created_at_epoch)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    client_id,
                    item_id,
                    anon_id,
                    model_id,
                    pred_class,
                    None if true_label is None else int(true_label),
                    now_iso,
                    now_epoch,
                ),
            )
            vote_id = int(cursor.lastrowid or 0)
            for (
                opponent_id,
                winner_before,
                winner_after,
                opponent_before,
                opponent_after,
                expected,
            ) in matches:
                conn.execute(
                    "INSERT INTO vote_item"
                    " (vote_id, winner_model_id, opponent_model_id,"
                    "  winner_before, winner_after, opponent_before, opponent_after,"
                    "  expected_winner, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        vote_id,
                        model_id,
                        opponent_id,
                        winner_before,
                        winner_after,
                        opponent_before,
                        opponent_after,
                        expected,
                        now_iso,
                    ),
                )
                conn.execute(
                    "UPDATE arena_model SET rating = ?, updated_at = ? WHERE model_id = ?",
                    (opponent_after, now_iso, opponent_id),
                )
            conn.execute(
                "UPDATE arena_model SET rating = ?, updated_at = ? WHERE model_id = ?",
                (winner_rating, now_iso, model_id),
            )
            conn.execute("COMMIT")
            return {
                "vote_id": vote_id,
                "elo_after": winner_rating,
                "games_applied": len(opponents),
            }
        except DuplicateVoteError:
            _rollback_quietly(conn)
            raise
        except ArenaStoreError:
            _rollback_quietly(conn)
            raise
        except sqlite3.IntegrityError as exc:
            # 唯一约束 (client_id, item_id) 兜底：并发下两次提交只成功一次。
            _rollback_quietly(conn)
            raise DuplicateVoteError() from exc
        except sqlite3.Error as exc:
            _rollback_quietly(conn)
            raise ArenaStoreError("竞技场投票写入失败") from exc
        finally:
            conn.close()

    def leaderboard_stats(
        self, model_ids: Sequence[str]
    ) -> dict[str, dict[str, float]]:
        """返回获选票数与参赛样本数，区分用户选择和模型曝光次数。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            elo_rows = conn.execute(
                "SELECT model_id, rating FROM arena_model"
            ).fetchall()
            win_rows = conn.execute(
                "SELECT model_id, COUNT(*) AS n FROM vote GROUP BY model_id"
            ).fetchall()
            loss_rows = conn.execute(
                "SELECT opponent_model_id AS model_id, COUNT(*) AS n"
                " FROM vote_item GROUP BY opponent_model_id"
            ).fetchall()
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            _rollback_quietly(conn)
            raise ArenaStoreError("竞技场榜单读取失败") from exc
        finally:
            conn.close()

        elo = {row["model_id"]: float(row["rating"]) for row in elo_rows}
        wins = {row["model_id"]: int(row["n"]) for row in win_rows}
        losses = {row["model_id"]: int(row["n"]) for row in loss_rows}
        stats: dict[str, dict[str, float]] = {}
        for model_id in model_ids:
            win_count = wins.get(model_id, 0)
            loss_count = losses.get(model_id, 0)
            stats[model_id] = {
                "elo": elo.get(model_id, ELO_INITIAL),
                "wins": win_count,
                "losses": loss_count,
                "sample_size": win_count + loss_count,
            }
        return stats


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------------------
# 业务服务
# ---------------------------------------------------------------------------
class ArenaService:
    """竞技场编排：抽题 -> 匿名候选 -> 投票/Elo -> 榜单。

    依赖只通过构造参数注入（``ModelRegistry`` + ``Mapping[str, ModelAdapter]``），
    便于用 fake adapter 验收，不在本类加载模型。
    """

    def __init__(
        self,
        *,
        registry: ModelRegistry,
        adapters: Mapping[str, ModelAdapter],
        classes: Sequence[str],
        logger: logging.Logger,
        db_path: str | Path | None = None,
        test_file: str | Path | None = None,
        store: ArenaStore | None = None,
        clock: Callable[[], float] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._registry = registry
        self._adapters = dict(adapters)
        self._classes = tuple(classes)
        self._class_set = frozenset(self._classes)
        self._logger = logger
        self._rng = rng or random.Random()
        self._limiter = RateLimiter(clock=clock)
        self._closed = False

        resolved_db = db_path or os.environ.get("TMF_DB_PATH") or DEFAULT_DB_PATH
        self._store = store if store is not None else ArenaStore(resolved_db)
        self._store.ensure_models(self._model_ids())

        resolved_test_file = (
            test_file
            or os.environ.get("TMF_ARENA_TEST_FILE")
            or DEFAULT_TEST_FILE
        )
        self._test_file = Path(resolved_test_file)
        self._samples: list[tuple[str, int]] | None = None
        self._samples_lock = threading.Lock()

    # -- 基础信息 ----------------------------------------------------------
    def _model_ids(self) -> list[str]:
        """参赛模型：注册表中启用的**本地**模型，默认排除 LLM。

        阶段 4B：竞技场刷新不得隐式触发 LLM/消耗每 IP 配额；匿名投票/Elo
        仍只使用这些候选。未来若需要竞技场显式 opt-in，应另行授权。
        """
        model_ids: list[str] = []
        for model_id in self._registry.all_model_ids():
            spec = self._registry.get(model_id)
            if spec is None or not spec.enabled:
                continue
            if spec.model_id == "llm" or spec.worker_id == "llm":
                continue
            model_ids.append(model_id)
        return model_ids

    def _display_name(self, model_id: str) -> str:
        spec = self._registry.get(model_id)
        return spec.display_name if spec is not None else model_id

    def close(self) -> None:
        self._closed = True
        self._store.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ApiError(503, ERROR_ARENA_UNAVAILABLE, "竞技场服务已关闭")

    # -- 题目样本 ----------------------------------------------------------
    def _read_samples(self) -> list[tuple[str, int]]:
        try:
            text = self._test_file.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise ApiError(
                503, ERROR_ARENA_NO_ITEMS, "竞技场题目文件不存在"
            ) from exc
        except (OSError, UnicodeDecodeError) as exc:
            raise ApiError(
                503, ERROR_ARENA_NO_ITEMS, "竞技场题目文件不可读"
            ) from exc
        samples: list[tuple[str, int]] = []
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            parts = line.rsplit("\t", 1)
            if len(parts) != 2:
                continue
            title = parts[0].strip()
            label_text = parts[1].strip()
            if not title:
                continue
            try:
                label_index = int(label_text)
            except ValueError:
                continue
            if 0 <= label_index < len(self._classes):
                samples.append((title, label_index))
        return samples

    def _ensure_samples(self) -> list[tuple[str, int]]:
        with self._samples_lock:
            if self._samples is None:
                self._samples = self._read_samples()
            return self._samples

    # -- 日志（不记标题；next 阶段不记模型身份） ---------------------------
    def _log(self, event: str, request_id: str | None, **fields: object) -> None:
        extra: dict[str, object] = {"event": event}
        if request_id:
            extra["request_id"] = request_id
        extra.update(fields)
        try:
            self._logger.info(event, extra=extra)
        except Exception:  # 日志失败不影响主流程
            pass

    def _log_error(
        self, event: str, request_id: str | None, error_code: str | None = None
    ) -> None:
        extra: dict[str, object] = {"event": event}
        if request_id:
            extra["request_id"] = request_id
        if error_code:
            extra["error_code"] = error_code
        try:
            self._logger.error(event, extra=extra)
        except Exception:
            pass

    def _store_error(self, request_id: str, exc: ArenaStoreError) -> ApiError:
        self._log_error("arena_store_error", request_id, ERROR_ARENA_STORE_ERROR)
        return ApiError(503, ERROR_ARENA_STORE_ERROR, "竞技场存储暂时不可用")

    # -- 模型调用 ----------------------------------------------------------
    async def _predict(
        self, model_id: str, title: str, request_id: str
    ) -> AdapterResult:
        adapter = self._adapters.get(model_id)
        if adapter is None:
            return AdapterResult(
                model_id=model_id,
                status="error",
                latency_ms=0,
                error_code=ERROR_MODEL_ERROR,
                error_message="模型适配器未装配",
            )
        try:
            result = await adapter.predict(title, request_id)
        except Exception:  # 适配器协议要求不抛异常；这里做防御性隔离
            self._log_error("arena_model_call_failed", request_id, ERROR_MODEL_ERROR)
            return AdapterResult(
                model_id=model_id,
                status="error",
                latency_ms=0,
                error_code=ERROR_MODEL_ERROR,
                error_message="模型调用异常",
            )
        if not isinstance(result, AdapterResult):
            self._log_error("arena_model_bad_result", request_id, ERROR_MODEL_ERROR)
            return AdapterResult(
                model_id=model_id,
                status="error",
                latency_ms=0,
                error_code=ERROR_MODEL_ERROR,
                error_message="模型返回格式非法",
            )
        return result

    # -- /arena/next -------------------------------------------------------
    async def next_item(self, request_id: str) -> ArenaNextOutcome:
        self._ensure_open()
        try:
            samples = await asyncio.to_thread(self._ensure_samples)
        except ApiError:
            raise
        except Exception as exc:
            self._log_error("arena_samples_failed", request_id, ERROR_ARENA_NO_ITEMS)
            raise ApiError(
                503, ERROR_ARENA_NO_ITEMS, "竞技场题目不可用"
            ) from exc
        if not samples:
            raise ApiError(503, ERROR_ARENA_NO_ITEMS, "竞技场暂无可用题目")

        title, true_label = samples[self._rng.randrange(len(samples))]
        model_ids = self._model_ids()
        if not model_ids:
            raise ApiError(
                503, ERROR_ARENA_UNAVAILABLE, "竞技场暂无可用候选", {"available": 0}
            )
        raw_results = await asyncio.gather(
            *(self._predict(model_id, title, request_id) for model_id in model_ids),
            return_exceptions=True,
        )

        successes: list[tuple[str, str, int]] = []
        for model_id, raw in zip(model_ids, raw_results):
            if isinstance(raw, AdapterResult) and raw.status == "ok":
                pred_class = raw.pred_class
                if isinstance(pred_class, str) and pred_class in self._class_set:
                    successes.append(
                        (model_id, pred_class, int(raw.latency_ms or 0))
                    )
                    continue
            # 失败只记事件，不记录模型身份，避免破坏匿名性。
            self._log_error("arena_candidate_unavailable", request_id, ERROR_MODEL_ERROR)

        available = len(successes)
        if available < 2:
            details = {"available": available, "required": 2}
            raise ApiError(
                503,
                ERROR_ARENA_UNAVAILABLE,
                "可用候选不足，无法开始投票",
                details,
            )
        partial = available < len(model_ids)

        shuffled = list(successes)
        self._rng.shuffle(shuffled)
        used_anon_ids: set[str] = set()
        stored_candidates: list[dict[str, object]] = []
        public_candidates: list[PublicCandidate] = []
        for model_id, pred_class, latency_ms in shuffled:
            anon_id = self._new_anon_id(used_anon_ids)
            used_anon_ids.add(anon_id)
            stored_candidates.append(
                {
                    "anon_id": anon_id,
                    "model_id": model_id,
                    "pred_class": pred_class,
                    "latency_ms": latency_ms,
                }
            )
            public_candidates.append(
                PublicCandidate(
                    anon_id=anon_id,
                    pred_class=pred_class,
                    latency_ms=latency_ms,
                )
            )
        item_id = secrets.token_urlsafe(_ITEM_ID_BYTES)
        try:
            await asyncio.to_thread(
                self._store.save_item,
                item_id=item_id,
                title=title,
                true_label=true_label,
                candidates=stored_candidates,
            )
        except ArenaStoreError as exc:
            raise self._store_error(request_id, exc) from exc

        self._log(
            "arena_next",
            request_id,
            candidate_count=available,
            partial=partial,
        )
        return ArenaNextOutcome(
            item_id=item_id,
            title=title,
            candidates=tuple(public_candidates),
            partial=partial,
        )

    @staticmethod
    def _new_anon_id(used: set[str]) -> str:
        for _ in range(32):
            anon_id = secrets.token_urlsafe(_ANON_ID_BYTES)
            if anon_id not in used:
                return anon_id
        return secrets.token_urlsafe(_ANON_ID_BYTES + 6)

    # -- /arena/vote -------------------------------------------------------
    async def vote(
        self,
        *,
        client_id: str,
        item_id: str,
        anon_id: str,
        request_id: str,
    ) -> ArenaVoteOutcome:
        self._ensure_open()
        client_id = self._validate_client_id(client_id)
        item_id = self._validate_opaque_id(
            item_id, field="item_id", error_code=ERROR_INVALID_REQUEST
        )
        anon_id = self._validate_opaque_id(
            anon_id, field="anon_id", error_code=ERROR_ARENA_ANON_INVALID
        )

        try:
            item = await asyncio.to_thread(self._store.get_item, item_id)
        except ArenaStoreError as exc:
            raise self._store_error(request_id, exc) from exc
        if item is None:
            raise ApiError(404, ERROR_ARENA_ITEM_NOT_FOUND, "题目不存在或已过期")

        candidates = item.get("candidates") or []
        selected: dict[str, object] | None = None
        for candidate in candidates:
            if isinstance(candidate, Mapping) and candidate.get("anon_id") == anon_id:
                selected = dict(candidate)
                break
        if selected is None:
            raise ApiError(400, ERROR_ARENA_ANON_INVALID, "候选编号无效")

        try:
            already_voted = await asyncio.to_thread(
                self._store.has_vote, client_id, item_id
            )
        except ArenaStoreError as exc:
            raise self._store_error(request_id, exc) from exc
        if already_voted:
            raise ApiError(
                409,
                ERROR_ARENA_DUPLICATE_VOTE,
                "同一题目只能投一票",
                {"item_id": item_id},
            )

        if not self._limiter.hit(client_id):
            raise ApiError(
                429,
                ERROR_ARENA_RATE_LIMITED,
                "投票过于频繁，请稍后再试",
                {"limit": RATE_LIMIT_VOTES, "window_s": int(RATE_LIMIT_WINDOW_S)},
            )

        winner_model_id = str(selected.get("model_id") or "")
        winner_pred_class = str(selected.get("pred_class") or "")
        if not winner_model_id or winner_pred_class not in self._class_set:
            raise ApiError(503, ERROR_ARENA_STORE_ERROR, "竞技场题目数据异常")
        opponent_model_ids = sorted(
            str(candidate.get("model_id"))
            for candidate in candidates
            if isinstance(candidate, Mapping)
            and candidate.get("anon_id") != anon_id
        )
        if not opponent_model_ids:
            raise ApiError(
                503, ERROR_ARENA_UNAVAILABLE, "可用候选不足，无法计分", {"available": 1}
            )

        try:
            recorded = await asyncio.to_thread(
                self._store.record_vote,
                client_id=client_id,
                item_id=item_id,
                anon_id=anon_id,
                model_id=winner_model_id,
                pred_class=winner_pred_class,
                true_label=item.get("true_label"),
                opponent_model_ids=opponent_model_ids,
            )
        except DuplicateVoteError as exc:
            raise ApiError(409, ERROR_ARENA_DUPLICATE_VOTE, "同一题目只能投一票") from exc
        except ArenaStoreError as exc:
            raise self._store_error(request_id, exc) from exc

        self._log(
            "arena_vote",
            request_id,
            candidate_count=len(candidates),
            games_applied=int(recorded.get("games_applied") or 0),
            vote_id=recorded.get("vote_id"),
        )
        return ArenaVoteOutcome(
            vote_id=int(recorded.get("vote_id") or 0),
            model_id=winner_model_id,
            display_name=self._display_name(winner_model_id),
            pred_class=winner_pred_class,
            elo_after=round(float(recorded.get("elo_after") or ELO_INITIAL), 2),
            games_applied=int(recorded.get("games_applied") or 0),
        )

    @staticmethod
    def _validate_client_id(value: str) -> str:
        if not isinstance(value, str):
            raise ApiError(400, ERROR_INVALID_REQUEST, "client_id 必须是字符串")
        trimmed = value.strip()
        if not _CLIENT_ID_PATTERN.match(trimmed):
            raise ApiError(
                400,
                ERROR_INVALID_REQUEST,
                "client_id 必须为 1~64 位字母/数字/._- 字符",
                {"field": "client_id"},
            )
        return trimmed

    @staticmethod
    def _validate_opaque_id(value: str, *, field: str, error_code: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ApiError(400, error_code, f"{field} 不能为空", {"field": field})
        trimmed = value.strip()
        if len(trimmed) > _MAX_OPAQUE_ID_CHARS:
            raise ApiError(400, error_code, f"{field} 过长", {"field": field})
        return trimmed

    # -- /arena/leaderboard ------------------------------------------------
    async def leaderboard(self, request_id: str) -> ArenaLeaderboardOutcome:
        self._ensure_open()
        model_ids = self._model_ids()
        try:
            stats = await asyncio.to_thread(
                self._store.leaderboard_stats, model_ids
            )
        except ArenaStoreError as exc:
            raise self._store_error(request_id, exc) from exc

        rows: list[LeaderboardEntry] = []
        for model_id in model_ids:
            stat = stats.get(model_id, {})
            wins = int(stat.get("wins", 0))
            losses = int(stat.get("losses", 0))
            # `votes` 是用户实际选择该模型的次数；`sample_size` 是该模型
            # 出现在投票题中的次数。多模型每题都会参与 Elo 对局，因此旧的
            # wins+losses 会让所有模型票数相同，不能作为“票数”展示。
            votes = wins
            sample_size = int(stat.get("sample_size", wins + losses))
            win_rate = round(votes / sample_size, 4) if sample_size else None
            lower, upper = wilson_interval(votes, sample_size)
            rows.append(
                LeaderboardEntry(
                    model_id=model_id,
                    display_name=self._display_name(model_id),
                    elo=round(float(stat.get("elo", ELO_INITIAL)), 2),
                    votes=votes,
                    sample_size=sample_size,
                    wins=wins,
                    losses=losses,
                    win_rate=win_rate,
                    wilson_lower=round(lower, 4),
                    wilson_upper=round(upper, 4),
                    warming_up=sample_size < WARMUP_MIN_VOTES,
                    rank=None,
                )
            )

        overall_ready = bool(rows) and all(
            row.sample_size >= WARMUP_MIN_VOTES for row in rows
        )
        status = "ready" if overall_ready else "warming_up"
        if overall_ready:
            # 仅当全部模型票数 ≥50 才给正式 rank；entries 仍保持配置顺序。
            order = sorted(
                range(len(rows)),
                key=lambda index: (-rows[index].elo, index),
            )
            rank_by_index = {index: rank for rank, index in enumerate(order, start=1)}
            rows = [
                LeaderboardEntry(
                    model_id=row.model_id,
                    display_name=row.display_name,
                    elo=row.elo,
                    votes=row.votes,
                    sample_size=row.sample_size,
                    wins=row.wins,
                    losses=row.losses,
                    win_rate=row.win_rate,
                    wilson_lower=row.wilson_lower,
                    wilson_upper=row.wilson_upper,
                    warming_up=row.warming_up,
                    rank=rank_by_index[index],
                )
                for index, row in enumerate(rows)
            ]

        self._log("arena_leaderboard", request_id, status=status)
        return ArenaLeaderboardOutcome(
            status=status,
            entries=tuple(rows),
            min_votes_for_ranking=WARMUP_MIN_VOTES,
        )
