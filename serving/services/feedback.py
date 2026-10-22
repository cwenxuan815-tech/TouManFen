"""反馈记录与生命周期服务（B4）。

职责
----
- ``POST /feedback``：校验并写入纠正记录到 SQLite ``feedback`` 表，``status`` 固定
  ``pending``；按 ``client_id + title(trim 后) + model_id`` 去重，重复提交返回
  ``409 FEEDBACK_DUPLICATE`` 且不重复计数；
- ``GET /feedback/stats``：只返回聚合（total / pending / by_class / by_model /
  similar_hits / retention_days），不提供任何原始数据导出；
- 相似反馈 hint：对标题做「Unicode NFKC -> casefold -> 仅保留字母数字（含 CJK）
  -> 3-gram Jaccard」规范化，相似度 ``>= 0.75`` 时返回
  ``{corrected_class, status="pending"}``；并列规则为
  「相似度降序 -> created_at 最新 -> feedback_id 最大」。只读 pending 反馈，
  不按 pred_class 过滤，也不改写 ``pred_class`` / 模型分数；
- 保留期：反馈 30 天，惰性清理（网关启动 + ``/feedback/stats``），只删除
  ``feedback`` 表，不触碰 B7 的 ``vote`` / ``vote_item`` / ``arena_*`` 数据。

边界
----
- 只用标准库 ``sqlite3``；WAL + busy_timeout + 显式事务 + 参数化 SQL；
- 表全部 ``CREATE TABLE IF NOT EXISTS``，不 DROP/重建任何表；与 B7 ``arena``
  共用同一库 ``serving/run/tmf.db``，可用 ``TMF_DB_PATH`` 覆盖；
- 日志不记录 title / 用户原文 / IP / UA；错误信息不回显字段原值；
- 本期无审核后台、不训练、不入训练集：``status`` 只有 ``pending``；
- 文案只允许「已提交，待审核」「已记录」，禁止「已学会」「已重新训练」。
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import sqlite3
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from ..gateway.errors import (
    ApiError,
    ERROR_FEEDBACK_DUPLICATE,
    ERROR_FEEDBACK_INVALID,
    ERROR_FEEDBACK_STORE_ERROR,
    ERROR_FEEDBACK_UNAVAILABLE,
)
from ..gateway.settings import SERVING_DIR
from ..models.registry import ModelRegistry

# ---------------------------------------------------------------------------
# 常量（与 docs/工程实施工作清单_75工时.md §4.4 / B4 一致）
# ---------------------------------------------------------------------------
FEEDBACK_RETENTION_DAYS = 30
FEEDBACK_STATUS_PENDING = "pending"
FEEDBACK_DEFAULT_SOURCE = "api"
FEEDBACK_ACCEPTED_MESSAGE = "反馈已提交，待审核"

SIMILARITY_THRESHOLD = 0.75
NGRAM_SIZE = 3

#: 默认 SQLite 路径：与 B7 ``serving/services/arena.py`` 的 ``DEFAULT_DB_PATH``
#: 完全一致（单一库，不新建第二个 DB）；``serving/run`` 已在 .gitignore 中。
DEFAULT_DB_PATH = SERVING_DIR / "run" / "tmf.db"

_MAX_CLIENT_ID_CHARS = 64
_MAX_SOURCE_CHARS = 32
_CLIENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_SOURCE_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

#: 表结构：只新增 feedback，绝不 DROP/重建 arena 表。
_SCHEMA = """
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


# ---------------------------------------------------------------------------
# 纯函数：标题规范化 / 3-gram / Jaccard（可被验收脚本直接单测，不依赖 DB）
# ---------------------------------------------------------------------------
def normalize_title(title: str) -> str:
    """NFKC -> casefold -> 仅保留字母数字（含 CJK）。

    例：``"ＡＢＣ，Hello！"`` -> ``"abchello"``；``"央行 宣布，降准！"`` ->
    ``"央行宣布降准"``。空串（纯标点/空白）返回 ``""``，此类标题不参与 hint。
    """
    if not isinstance(title, str):
        return ""
    text = unicodedata.normalize("NFKC", title).casefold()
    return "".join(char for char in text if char.isalnum())


def character_ngrams(text: str, n: int = NGRAM_SIZE) -> frozenset[str]:
    """字符 n-gram 集合；``len(text) <= n`` 时退化为整串一个 gram。"""
    if not text:
        return frozenset()
    if len(text) <= n:
        return frozenset({text})
    return frozenset(text[index : index + n] for index in range(len(text) - n + 1))


def jaccard_similarity(left: str, right: str, n: int = NGRAM_SIZE) -> float:
    """两个**已规范化**字符串的字符 n-gram Jaccard；空集返回 0.0。"""
    left_grams = character_ngrams(left, n)
    right_grams = character_ngrams(right, n)
    union = left_grams | right_grams
    if not union:
        return 0.0
    return len(left_grams & right_grams) / len(union)


def _utc_now_iso(now_epoch: float | None = None) -> str:
    if now_epoch is None:
        return datetime.now(timezone.utc).isoformat()
    return datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 结果数据类（router 只做字段映射）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FeedbackOutcome:
    feedback_id: int
    status: str
    total: int


@dataclass(frozen=True)
class FeedbackStatsOutcome:
    total: int
    pending: int
    by_class: dict[str, int]
    by_model: dict[str, int]
    similar_hits: int
    retention_days: int


@dataclass(frozen=True)
class FeedbackHint:
    """相似历史反馈提示：只暴露纠正类别与待审核状态，不含原文/ID。"""

    corrected_class: str
    status: str = FEEDBACK_STATUS_PENDING


# ---------------------------------------------------------------------------
# SQLite 存储
# ---------------------------------------------------------------------------
class FeedbackStoreError(RuntimeError):
    """反馈 SQLite 读写失败（对外统一 503，不泄露堆栈）。"""


class DuplicateFeedbackError(RuntimeError):
    """``client_id + title + model_id`` 已存在（去重命中）。"""

    def __init__(self, feedback_id: int = 0, total: int = 0) -> None:
        super().__init__("duplicate feedback")
        self.feedback_id = int(feedback_id)
        self.total = int(total)


class FeedbackStore:
    """反馈 SQLite 存储（短连接 + 显式事务；WAL + busy_timeout 支撑并发写）。"""

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
            raise FeedbackStoreError("反馈数据库已关闭")
        try:
            conn = sqlite3.connect(
                str(self._db_path), timeout=10.0, isolation_level=None
            )
        except sqlite3.Error as exc:  # pragma: no cover - 由上层统一映射
            raise FeedbackStoreError("无法连接反馈数据库") from exc
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error as exc:
            conn.close()
            raise FeedbackStoreError("反馈数据库配置失败") from exc
        return conn

    def _initialize(self) -> None:
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self._db_path), timeout=10.0, isolation_level=None
            )
        except (sqlite3.Error, OSError) as exc:
            raise FeedbackStoreError("反馈数据库初始化失败") from exc
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise FeedbackStoreError("反馈数据库初始化失败") from exc
        finally:
            conn.close()

    def close(self) -> None:
        self._closed = True

    # -- 写入 --------------------------------------------------------------
    def add(
        self,
        *,
        client_id: str,
        title: str,
        normalized_title: str,
        model_id: str,
        pred_class: str,
        corrected_class: str,
        score: float | None,
        source: str,
        status: str = FEEDBACK_STATUS_PENDING,
        now_epoch: float | None = None,
    ) -> dict[str, object]:
        """写入一条 pending 反馈；去重命中抛 ``DuplicateFeedbackError``。

        返回 ``{"feedback_id", "total"}``（total 为写入后当前反馈总数）。
        """
        now_epoch = time.time() if now_epoch is None else float(now_epoch)
        now_iso = _utc_now_iso(now_epoch)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT feedback_id FROM feedback"
                " WHERE client_id = ? AND title = ? AND model_id = ?",
                (client_id, title, model_id),
            ).fetchone()
            if existing is not None:
                total = self._count_locked(conn)
                conn.execute("COMMIT")
                raise DuplicateFeedbackError(int(existing["feedback_id"]), total)
            cursor = conn.execute(
                "INSERT INTO feedback"
                " (client_id, title, normalized_title, model_id, pred_class,"
                "  corrected_class, score, source, status, created_at,"
                "  created_at_epoch)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    client_id,
                    title,
                    normalized_title,
                    model_id,
                    pred_class,
                    corrected_class,
                    score,
                    source,
                    status,
                    now_iso,
                    now_epoch,
                ),
            )
            feedback_id = int(cursor.lastrowid or 0)
            total = self._count_locked(conn)
            conn.execute("COMMIT")
            return {"feedback_id": feedback_id, "total": total}
        except DuplicateFeedbackError:
            raise
        except sqlite3.IntegrityError as exc:
            # UNIQUE 兜底：并发下两次提交只成功一次。
            _rollback_quietly(conn)
            feedback_id, total = self._duplicate_info(client_id, title, model_id)
            raise DuplicateFeedbackError(feedback_id, total) from exc
        except sqlite3.Error as exc:
            _rollback_quietly(conn)
            raise FeedbackStoreError("反馈写入失败") from exc
        finally:
            conn.close()

    def _duplicate_info(
        self, client_id: str, title: str, model_id: str
    ) -> tuple[int, int]:
        """IntegrityError 竞态下补齐 existing id/total（不持有写锁）。"""
        try:
            conn = self._connect()
        except FeedbackStoreError:
            return 0, 0
        try:
            row = conn.execute(
                "SELECT feedback_id FROM feedback"
                " WHERE client_id = ? AND title = ? AND model_id = ?",
                (client_id, title, model_id),
            ).fetchone()
            total = self._count_locked(conn)
            return (int(row["feedback_id"]) if row is not None else 0), total
        except sqlite3.Error:
            return 0, 0
        finally:
            conn.close()

    @staticmethod
    def _count_locked(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT COUNT(*) AS n FROM feedback").fetchone()
        return int(row["n"]) if row is not None else 0

    # -- 读取 / 清理 -------------------------------------------------------
    def stats(self) -> dict[str, object]:
        """聚合统计：total / pending / by_class(corrected_class) / by_model / similar_hits。

        ``similar_hits`` 只做聚合：统计“至少存在一条相似 pending 反馈”的
        pending 行数（3-gram Jaccard >= 0.75），不返回任何相似标题或原始行。
        """
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            total = self._count_locked(conn)
            pending_row = conn.execute(
                "SELECT COUNT(*) AS n FROM feedback WHERE status = ?",
                (FEEDBACK_STATUS_PENDING,),
            ).fetchone()
            class_rows = conn.execute(
                "SELECT corrected_class AS name, COUNT(*) AS n"
                " FROM feedback GROUP BY corrected_class"
            ).fetchall()
            model_rows = conn.execute(
                "SELECT model_id AS name, COUNT(*) AS n"
                " FROM feedback GROUP BY model_id"
            ).fetchall()
            title_rows = conn.execute(
                "SELECT normalized_title FROM feedback WHERE status = ?",
                (FEEDBACK_STATUS_PENDING,),
            ).fetchall()
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            _rollback_quietly(conn)
            raise FeedbackStoreError("反馈统计读取失败") from exc
        finally:
            conn.close()
        normalized = [
            str(row["normalized_title"] or "") for row in title_rows
        ]
        grams = [character_ngrams(text) for text in normalized]
        similar_hits = 0
        for index, current in enumerate(grams):
            if not current:
                continue
            for other_index, other in enumerate(grams):
                if other_index == index or not other:
                    continue
                union = current | other
                if union and len(current & other) / len(union) >= SIMILARITY_THRESHOLD:
                    similar_hits += 1
                    break
        return {
            "total": total,
            "pending": int(pending_row["n"]) if pending_row is not None else 0,
            "by_class": {str(row["name"]): int(row["n"]) for row in class_rows},
            "by_model": {str(row["name"]): int(row["n"]) for row in model_rows},
            "similar_hits": similar_hits,
        }

    def pending_rows(self) -> list[dict[str, object]]:
        """只读取 pending 反馈的 hint 所需字段（不含 title 原文）。

        候选规则（B4）：``status = 'pending'``，同题/跨模型均可，不按
        ``pred_class`` 过滤；返回顺序由调用方按相似度/时间/id 稳定排序。
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT feedback_id, normalized_title, corrected_class,"
                " created_at_epoch FROM feedback WHERE status = ?",
                (FEEDBACK_STATUS_PENDING,),
            ).fetchall()
        except sqlite3.Error as exc:
            raise FeedbackStoreError("反馈相似检索失败") from exc
        finally:
            conn.close()
        return [
            {
                "feedback_id": int(row["feedback_id"]),
                "normalized_title": str(row["normalized_title"] or ""),
                "corrected_class": str(row["corrected_class"]),
                "created_at_epoch": float(row["created_at_epoch"]),
            }
            for row in rows
        ]

    def purge_expired(self, cutoff_epoch: float) -> int:
        """只删除 ``created_at_epoch < cutoff_epoch`` 的 feedback 行。"""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                "DELETE FROM feedback WHERE created_at_epoch < ?",
                (float(cutoff_epoch),),
            )
            removed = int(cursor.rowcount) if cursor.rowcount is not None else 0
            conn.execute("COMMIT")
            return max(0, removed)
        except sqlite3.Error as exc:
            _rollback_quietly(conn)
            raise FeedbackStoreError("反馈过期清理失败") from exc
        finally:
            conn.close()

    def count(self) -> int:
        conn = self._connect()
        try:
            return self._count_locked(conn)
        except sqlite3.Error as exc:
            raise FeedbackStoreError("反馈计数读取失败") from exc
        finally:
            conn.close()


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------------------
# 业务服务
# ---------------------------------------------------------------------------
class FeedbackService:
    """反馈编排：校验 -> 去重落库 -> 聚合统计 -> 相似 hint -> 惰性清理。

    依赖通过构造参数注入（``ModelRegistry`` / 类别白名单 / DB 路径），便于
    fake/TestClient 验收；本类不加载模型、不访问网络、不重启 worker。
    """

    def __init__(
        self,
        *,
        registry: ModelRegistry,
        classes: Sequence[str],
        max_title_chars: int,
        logger: logging.Logger,
        db_path: str | Path | None = None,
        store: FeedbackStore | None = None,
        retention_days: int = FEEDBACK_RETENTION_DAYS,
    ) -> None:
        self._registry = registry
        self._classes = frozenset(classes)
        self._class_order = tuple(classes)
        self._max_title_chars = int(max_title_chars)
        self._logger = logger
        self._retention_days = int(retention_days)
        self._closed = False

        resolved_db = db_path or os.environ.get("TMF_DB_PATH") or DEFAULT_DB_PATH
        self._store = store if store is not None else FeedbackStore(resolved_db)

    # -- 基础 --------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self._store.db_path

    @property
    def retention_days(self) -> int:
        return self._retention_days

    def close(self) -> None:
        self._closed = True
        self._store.close()

    def _ensure_open(self) -> None:
        if self._closed:
            raise ApiError(503, ERROR_FEEDBACK_UNAVAILABLE, "反馈服务已关闭")

    def _model_ids(self) -> list[str]:
        return [
            model_id
            for model_id in self._registry.all_model_ids()
            if (spec := self._registry.get(model_id)) is not None and spec.enabled
        ]

    # -- 日志（只记事件/计数/ID，不记 title/原文/client_ip/ua） --------------
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

    def _store_error(self, request_id: str, exc: FeedbackStoreError) -> ApiError:
        self._log_error("feedback_store_error", request_id, ERROR_FEEDBACK_STORE_ERROR)
        return ApiError(503, ERROR_FEEDBACK_STORE_ERROR, "反馈存储暂时不可用")

    @staticmethod
    def _invalid(field: str, message: str) -> ApiError:
        # 错误信息只描述规则，不回显提交值原文。
        return ApiError(400, ERROR_FEEDBACK_INVALID, message, {"field": field})

    # -- 字段校验 ----------------------------------------------------------
    def _validate_client_id(self, value: str) -> str:
        if not isinstance(value, str):
            raise self._invalid("client_id", "client_id 必须是字符串")
        trimmed = value.strip()
        if not _CLIENT_ID_PATTERN.match(trimmed):
            raise self._invalid(
                "client_id", "client_id 必须为 1~64 位字母/数字/._- 字符"
            )
        return trimmed

    def _validate_title(self, value: str) -> str:
        if not isinstance(value, str):
            raise self._invalid("title", "title 必须是字符串")
        trimmed = value.strip()
        if not trimmed:
            raise self._invalid("title", "title 不能为空")
        if len(trimmed) > self._max_title_chars:
            raise self._invalid(
                "title", f"title 长度不能超过 {self._max_title_chars} 个字符"
            )
        return trimmed

    def _validate_model_id(self, value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise self._invalid("model_id", "model_id 不能为空")
        trimmed = value.strip()
        spec = self._registry.get(trimmed)
        if spec is None:
            raise self._invalid("model_id", "model_id 不在注册模型内")
        if not spec.enabled:
            raise self._invalid("model_id", "model_id 对应模型未启用")
        return trimmed

    def _validate_class(self, field: str, value: str) -> str:
        if not isinstance(value, str) or value not in self._classes:
            raise self._invalid(field, f"{field} 不在类别白名单内")
        return value

    @staticmethod
    def _validate_score(value: float | None) -> float | None:
        if value is None:
            return None
        try:
            score = float(value)
        except (TypeError, ValueError) as exc:
            raise FeedbackService._invalid("score", "score 必须是有限数值") from exc
        if not math.isfinite(score) or not 0.0 <= score <= 1.0:
            raise FeedbackService._invalid("score", "score 必须是 [0, 1] 内的有限数值")
        return score

    @staticmethod
    def _validate_source(value: str | None) -> str:
        if value is None or value == "":
            return FEEDBACK_DEFAULT_SOURCE
        if not isinstance(value, str):
            raise FeedbackService._invalid("source", "source 必须是字符串")
        trimmed = value.strip()
        if not _SOURCE_PATTERN.match(trimmed):
            raise FeedbackService._invalid(
                "source", "source 必须为 1~32 位字母/数字/._- 字符"
            )
        return trimmed

    # -- 提交 --------------------------------------------------------------
    async def submit(
        self,
        *,
        client_id: str,
        title: str,
        model_id: str,
        pred_class: str,
        corrected_class: str,
        score: float | None,
        source: str | None,
        request_id: str,
    ) -> FeedbackOutcome:
        self._ensure_open()
        clean_client = self._validate_client_id(client_id)
        clean_title = self._validate_title(title)
        clean_model = self._validate_model_id(model_id)
        clean_pred = self._validate_class("pred_class", pred_class)
        clean_corrected = self._validate_class("corrected_class", corrected_class)
        clean_score = self._validate_score(score)
        clean_source = self._validate_source(source)
        normalized = normalize_title(clean_title)

        try:
            recorded = await asyncio.to_thread(
                self._store.add,
                client_id=clean_client,
                title=clean_title,
                normalized_title=normalized,
                model_id=clean_model,
                pred_class=clean_pred,
                corrected_class=clean_corrected,
                score=clean_score,
                source=clean_source,
            )
        except DuplicateFeedbackError as exc:
            self._log(
                "feedback_duplicate",
                request_id,
                feedback_id=exc.feedback_id,
                total=exc.total,
            )
            raise ApiError(
                409,
                ERROR_FEEDBACK_DUPLICATE,
                "该反馈已提交过，无需重复提交",
                {"total": exc.total},
            ) from exc
        except FeedbackStoreError as exc:
            raise self._store_error(request_id, exc) from exc

        feedback_id = int(recorded.get("feedback_id") or 0)
        total = int(recorded.get("total") or 0)
        self._log(
            "feedback_submitted",
            request_id,
            feedback_id=feedback_id,
            model_id=clean_model,
            status=FEEDBACK_STATUS_PENDING,
            total=total,
        )
        return FeedbackOutcome(
            feedback_id=feedback_id,
            status=FEEDBACK_STATUS_PENDING,
            total=total,
        )

    # -- 统计 + 惰性清理 ---------------------------------------------------
    async def cleanup_expired(self, *, now_epoch: float | None = None) -> int:
        """删除超过保留期的反馈；只作用于 feedback 表。"""
        cutoff = (time.time() if now_epoch is None else float(now_epoch)) - (
            self._retention_days * 86400
        )
        try:
            removed = await asyncio.to_thread(self._store.purge_expired, cutoff)
        except FeedbackStoreError:
            raise
        if removed:
            self._log("feedback_cleanup", None, removed=removed)
        return removed

    async def stats(
        self, *, request_id: str, now_epoch: float | None = None
    ) -> FeedbackStatsOutcome:
        self._ensure_open()
        cutoff = (time.time() if now_epoch is None else float(now_epoch)) - (
            self._retention_days * 86400
        )
        try:
            removed = await asyncio.to_thread(self._store.purge_expired, cutoff)
            raw = await asyncio.to_thread(self._store.stats)
        except FeedbackStoreError as exc:
            raise self._store_error(request_id, exc) from exc
        if removed:
            self._log("feedback_cleanup", request_id, removed=removed)

        raw_by_class = raw.get("by_class") or {}
        by_class: dict[str, int] = {class_id: 0 for class_id in self._class_order}
        for class_id, count in dict(raw_by_class).items():
            by_class[str(class_id)] = int(count)

        raw_by_model = raw.get("by_model") or {}
        by_model: dict[str, int] = {model_id: 0 for model_id in self._model_ids()}
        for model_id, count in dict(raw_by_model).items():
            by_model[str(model_id)] = int(count)
        for extra_model_id in sorted(set(dict(raw_by_model)) - set(by_model)):
            by_model[str(extra_model_id)] = int(dict(raw_by_model)[extra_model_id])

        self._log(
            "feedback_stats",
            request_id,
            total=int(raw.get("total") or 0),
            removed=removed,
        )
        return FeedbackStatsOutcome(
            total=int(raw.get("total") or 0),
            pending=int(raw.get("pending") or 0),
            by_class=by_class,
            by_model=by_model,
            similar_hits=int(raw.get("similar_hits") or 0),
            retention_days=self._retention_days,
        )

    # -- 相似反馈 hint -----------------------------------------------------
    async def similar_hint(self, title: str) -> FeedbackHint | None:
        """返回相似 pending 反馈的纠正类别；无命中返回 None。

        规范化：NFKC -> casefold -> 仅保留字母数字（含 CJK） -> 3-gram
        Jaccard；阈值 ``>= 0.75``。并列规则：相似度降序 -> created_at 最新
        -> feedback_id 最大。只读取 ``status = 'pending'`` 的反馈。
        """
        if not isinstance(title, str):
            return None
        normalized = normalize_title(title)
        if not normalized:
            return None
        try:
            rows = await asyncio.to_thread(self._store.pending_rows)
        except FeedbackStoreError:
            raise
        best_row: dict[str, object] | None = None
        best_key: tuple[float, float, int] | None = None
        for row in rows:
            candidate = str(row.get("normalized_title") or "")
            if not candidate:
                continue
            similarity = jaccard_similarity(normalized, candidate)
            if similarity < SIMILARITY_THRESHOLD:
                continue
            key = (
                similarity,
                float(row.get("created_at_epoch") or 0.0),
                int(row.get("feedback_id") or 0),
            )
            if best_key is None or key > best_key:
                best_key = key
                best_row = row
        if best_row is None:
            return None
        return FeedbackHint(
            corrected_class=str(best_row.get("corrected_class") or ""),
            status=FEEDBACK_STATUS_PENDING,
        )
