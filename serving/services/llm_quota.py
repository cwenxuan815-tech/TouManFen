"""LLM 配额 SQLite 基础存储（阶段 4A）。

设计边界：
- 只负责按 HMAC IP key 原子预留额度、持久化计数/封禁和审计事件；
- 不接入路由/服务/LLM adapter，不调用 Provider；
- 不使用进程内字典作为权威状态；数据库异常一律抛
  ``LlmQuotaStoreError``，由后续调用层 fail-closed 返回 503；
- 不实现管理员解除命令/API，只建立可容纳解除记录的表结构。

数据库选择复用 ``serving/run/tmf.db``（``TMF_DB_PATH`` 可覆盖），
与 arena/feedback 同一 SQLite 文件；本模块使用自己的两张表，不改动既有表。
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..gateway.client_ip import normalize_ip
from ..gateway.quota_config import LlmQuotaConfigError

LLM_QUOTA_LIMIT = 3
MIN_HMAC_KEY_BYTES = 32


@dataclass(frozen=True)
class LlmQuotaContext:
    """请求级配额上下文；不包含标题/Provider payload/明文日志字段。"""

    client_ip: str | None
    route: str


_current_context: ContextVar[LlmQuotaContext | None] = ContextVar(
    "tmf_llm_quota_context", default=None
)


@contextmanager
def llm_quota_context(client_ip: str | None, route: str) -> Iterator[None]:
    """在 service 调用 LLM adapter 的作用域内设置可信客户端身份。"""
    token = _current_context.set(LlmQuotaContext(client_ip=client_ip, route=route))
    try:
        yield
    finally:
        _current_context.reset(token)


def current_llm_quota_context() -> LlmQuotaContext | None:
    return _current_context.get()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS llm_ip_quota (
    ip_key TEXT PRIMARY KEY,
    used INTEGER NOT NULL CHECK (used >= 0 AND used <= 3),
    first_used_at TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    blocked_at TEXT
);

CREATE TABLE IF NOT EXISTS llm_quota_event (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    ip_key TEXT NOT NULL,
    request_id TEXT,
    route TEXT,
    operation TEXT NOT NULL,
    used_before INTEGER NOT NULL,
    used_after INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    admin_note TEXT
);

CREATE INDEX IF NOT EXISTS idx_llm_quota_event_key_created
    ON llm_quota_event(ip_key, created_at);
"""


class LlmQuotaError(RuntimeError):
    """配额基础设施错误基类。"""


class LlmQuotaStoreError(LlmQuotaError):
    """SQLite 打开/事务/写入失败；调用方必须 fail-closed。"""


class LlmQuotaIdentityError(LlmQuotaError):
    """无法归一化 IP；调用方必须拒绝，不得猜测身份。"""


class LlmQuotaAdminError(LlmQuotaError):
    """离线管理员操作的参数/状态错误。"""


class QuotaNotFoundError(LlmQuotaAdminError):
    """指定 IP 尚无配额记录；不得凭空创建或解除。"""


#: 管理员解除操作的审计 operation，复用既有 llm_quota_event 表。
ADMIN_UNBLOCK_OPERATION = "admin_unblock"


@dataclass(frozen=True)
class QuotaReservation:
    allowed: bool
    used_before: int
    used_after: int
    limit: int
    blocked: bool
    event_id: int | None


@dataclass(frozen=True)
class QuotaUsage:
    #: HMAC-SHA256(IP)；不是明文 IP。
    ip_key: str
    used: int
    limit: int
    blocked: bool
    first_used_at: str | None
    last_used_at: str | None
    blocked_at: str | None


@dataclass(frozen=True)
class QuotaUnblockResult:
    """一次管理员解除的结果；不包含明文 IP。"""

    used_before: int
    used_after: int
    blocked_before: bool
    blocked_after: bool
    first_used_at: str | None
    last_used_at: str | None
    event_id: int | None
    admin_note: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rollback_quietly(conn: sqlite3.Connection) -> None:
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


class LlmQuotaStore:
    """短连接 + ``BEGIN IMMEDIATE`` 的原子 LLM 配额存储。"""

    def __init__(
        self,
        db_path: str | Path,
        hmac_key: bytes | str,
        *,
        limit: int = LLM_QUOTA_LIMIT,
    ) -> None:
        if isinstance(hmac_key, str):
            key = hmac_key.encode("utf-8")
        elif isinstance(hmac_key, (bytes, bytearray)):
            key = bytes(hmac_key)
        else:
            raise LlmQuotaConfigError("LLM quota HMAC key 必须是 bytes 或 str")
        if len(key) < MIN_HMAC_KEY_BYTES:
            raise LlmQuotaConfigError(
                f"LLM quota HMAC key 长度必须不少于 {MIN_HMAC_KEY_BYTES} 字节"
            )
        if not 1 <= int(limit) <= LLM_QUOTA_LIMIT:
            # 产品固定上限为 3；测试可注入更小值，但不能放宽。
            raise LlmQuotaConfigError("LLM quota limit 必须在 1~3 之间")
        self._db_path = Path(db_path)
        self._hmac_key = key
        self._limit = int(limit)
        self._closed = False
        self._initialize()

    @property
    def db_path(self) -> Path:
        return self._db_path

    @property
    def limit(self) -> int:
        return self._limit

    def close(self) -> None:
        self._closed = True

    # -- 连接/初始化 -----------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise LlmQuotaStoreError("LLM 配额数据库已关闭")
        try:
            conn = sqlite3.connect(
                str(self._db_path), timeout=10.0, isolation_level=None
            )
        except sqlite3.Error as exc:
            raise LlmQuotaStoreError("无法连接 LLM 配额数据库") from exc
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
        except sqlite3.Error as exc:
            conn.close()
            raise LlmQuotaStoreError("LLM 配额数据库配置失败") from exc
        return conn

    def _initialize(self) -> None:
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(
                str(self._db_path), timeout=10.0, isolation_level=None
            )
        except (sqlite3.Error, OSError) as exc:
            raise LlmQuotaStoreError("LLM 配额数据库初始化失败") from exc
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise LlmQuotaStoreError("LLM 配额数据库初始化失败") from exc
        finally:
            conn.close()

    def _ip_key(self, ip: str) -> str:
        try:
            normalized = normalize_ip(ip)
        except Exception as exc:
            raise LlmQuotaIdentityError("客户端 IP 无法归一化") from exc
        return hmac.new(
            self._hmac_key,
            normalized.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    # -- 预留 ------------------------------------------------------------
    def reserve(
        self,
        ip: str,
        *,
        request_id: str | None = None,
        route: str | None = None,
    ) -> QuotaReservation:
        """原子预留一次额度；返回结果后调用方才能发起 LLM 调用。

        - 第 1/2/3 次允许；
        - 第 4 次及以后拒绝，不增加 used；
        - 任何 SQLite 错误抛 ``LlmQuotaStoreError``，绝不退化为内存/fail-open。
        """
        ip_key = self._ip_key(ip)
        now = _utc_now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT used, first_used_at, last_used_at, blocked_at"
                " FROM llm_ip_quota WHERE ip_key = ?",
                (ip_key,),
            ).fetchone()

            if row is None:
                used_before = 0
                used_after = 1
                blocked_at = now if used_after >= self._limit else None
                conn.execute(
                    "INSERT INTO llm_ip_quota"
                    " (ip_key, used, first_used_at, last_used_at, blocked_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (ip_key, used_after, now, now, blocked_at),
                )
                allowed = True
                operation = "reserve_allowed"
                event_id = conn.execute(
                    "INSERT INTO llm_quota_event"
                    " (ip_key, request_id, route, operation, used_before, used_after, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ip_key, request_id, route, operation, used_before, used_after, now),
                ).lastrowid
            else:
                used_before = int(row["used"])
                if used_before >= self._limit or row["blocked_at"] is not None:
                    used_after = used_before
                    if row["blocked_at"] is None:
                        conn.execute(
                            "UPDATE llm_ip_quota SET blocked_at = ? WHERE ip_key = ?",
                            (now, ip_key),
                        )
                    allowed = False
                    operation = "reserve_denied"
                else:
                    used_after = used_before + 1
                    blocked_at = now if used_after >= self._limit else None
                    conn.execute(
                        "UPDATE llm_ip_quota"
                        " SET used = ?, last_used_at = ?,"
                        " blocked_at = COALESCE(blocked_at, ?)"
                        " WHERE ip_key = ?",
                        (used_after, now, blocked_at, ip_key),
                    )
                    allowed = True
                    operation = "reserve_allowed"
                event_id = conn.execute(
                    "INSERT INTO llm_quota_event"
                    " (ip_key, request_id, route, operation, used_before, used_after, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ip_key, request_id, route, operation, used_before, used_after, now),
                ).lastrowid

            blocked = used_after >= self._limit
            conn.execute("COMMIT")
            return QuotaReservation(
                allowed=allowed,
                used_before=used_before,
                used_after=used_after,
                limit=self._limit,
                blocked=blocked,
                event_id=int(event_id) if event_id is not None else None,
            )
        except sqlite3.Error as exc:
            _rollback_quietly(conn)
            raise LlmQuotaStoreError("LLM 配额存储不可用") from exc
        finally:
            conn.close()

    # -- 只读使用量（供测试/后续只读展示；不提供解除功能） ---------------
    def get_usage(self, ip: str) -> QuotaUsage:
        ip_key = self._ip_key(ip)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT used, first_used_at, last_used_at, blocked_at"
                " FROM llm_ip_quota WHERE ip_key = ?",
                (ip_key,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise LlmQuotaStoreError("LLM 配额存储不可用") from exc
        finally:
            conn.close()
        if row is None:
            return QuotaUsage(
                ip_key=ip_key,
                used=0,
                limit=self._limit,
                blocked=False,
                first_used_at=None,
                last_used_at=None,
                blocked_at=None,
            )
        used = int(row["used"])
        return QuotaUsage(
            ip_key=ip_key,
            used=used,
            limit=self._limit,
            blocked=used >= self._limit or row["blocked_at"] is not None,
            first_used_at=str(row["first_used_at"]),
            last_used_at=str(row["last_used_at"]),
            blocked_at=None if row["blocked_at"] is None else str(row["blocked_at"]),
        )

    # -- 离线管理员解除（纯 storage 方法；不提供 HTTP/CLI 之外的入口） ----
    def unblock(
        self,
        ip: str,
        *,
        admin_note: str,
        request_id: str | None = None,
    ) -> QuotaUnblockResult:
        """原子解除该 IP 的永久封禁并重置当前配额计数。

        - 只操作显式给出的 IP（HMAC 后查行）；无记录时抛
          ``QuotaNotFoundError``，不凭空创建；
        - `used` 重置为 0、`blocked_at` 清空；保留 first/last 时间；
        - 同事务写 ``admin_unblock`` 审计事件，`admin_note` 记录理由；
        - 不删除历史 reserve 事件；失败回滚，不部分更新。
        """
        note = admin_note.strip() if isinstance(admin_note, str) else ""
        if not note:
            raise LlmQuotaAdminError("管理员备注不能为空")
        ip_key = self._ip_key(ip)
        now = _utc_now_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT used, first_used_at, last_used_at, blocked_at"
                " FROM llm_ip_quota WHERE ip_key = ?",
                (ip_key,),
            ).fetchone()
            if row is None:
                _rollback_quietly(conn)
                raise QuotaNotFoundError("该 IP 尚无配额记录")
            used_before = int(row["used"])
            blocked_before = (
                used_before >= self._limit or row["blocked_at"] is not None
            )
            first_used_at = str(row["first_used_at"])
            last_used_at = str(row["last_used_at"])
            conn.execute(
                "UPDATE llm_ip_quota"
                " SET used = 0, blocked_at = NULL"
                " WHERE ip_key = ?",
                (ip_key,),
            )
            event_id = conn.execute(
                "INSERT INTO llm_quota_event"
                " (ip_key, request_id, route, operation, used_before, used_after, created_at, admin_note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ip_key,
                    request_id,
                    "offline-cli",
                    ADMIN_UNBLOCK_OPERATION,
                    used_before,
                    0,
                    now,
                    note,
                ),
            ).lastrowid
            conn.execute("COMMIT")
            return QuotaUnblockResult(
                used_before=used_before,
                used_after=0,
                blocked_before=blocked_before,
                blocked_after=False,
                first_used_at=first_used_at,
                last_used_at=last_used_at,
                event_id=int(event_id) if event_id is not None else None,
                admin_note=note,
            )
        except QuotaNotFoundError:
            raise
        except sqlite3.Error as exc:
            _rollback_quietly(conn)
            raise LlmQuotaStoreError("LLM 配额存储不可用") from exc
        finally:
            conn.close()
