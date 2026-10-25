"""阶段 5B：访问 manager 的网关适配层。

职责：
- 把可信客户端 IP 通过域分离 HMAC 转成 manager 的 ip_key；
- 把 manager 状态映射为稳定的网关错误（403/409/429/503）；
- 业务 API gate 与 access API 共用同一 manager；
- 提供状态轮询最小间隔保护；
- 不记录明文 IP、ticket 或 ticket hash。

时间与状态仍由 5A manager 持有；本模块不持久化、不续租。
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping

from .client_ip import ClientIpError, normalize_ip
from .errors import (
    ERROR_ACCESS_CLAIM_REQUIRED,
    ERROR_ACCESS_COOLING,
    ERROR_ACCESS_IDENTITY_INVALID,
    ERROR_ACCESS_QUEUED,
    ERROR_ACCESS_REQUIRED,
    ERROR_ACCESS_TICKET_INVALID,
    ERROR_ACCESS_UNAVAILABLE,
)
from ..services.access import (
    AccessError,
    AccessManager,
    AccessResult,
)

ACCESS_TICKET_HEADER = "X-Access-Ticket"
_ACCESS_DOMAIN = b"tmf-access-v1|"


@dataclass
class AccessDenied(Exception):
    """业务 API gate 的预期拒绝；由 middleware 转统一错误体。"""

    status_code: int
    code: str
    message: str
    details: Mapping[str, Any] | None = None
    retry_after_seconds: float | None = None


class AccessGateUnavailable(RuntimeError):
    """manager/ticket 基础状态异常；必须 fail-closed 503。"""


class AccessPollThrottled(RuntimeError):
    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        super().__init__("access status polled too frequently")


def is_access_exempt(path: str, method: str) -> bool:
    """access/health/OPTIONS 免业务门禁；其他路径默认受保护。"""
    if method.upper() == "OPTIONS":
        return True
    if path in {"/api/v1/healthz", "/api/v1/readyz"}:
        return True
    if path == "/api/v1/access" or path.startswith("/api/v1/access/"):
        return True
    return False


class AccessGate:
    def __init__(
        self,
        manager: AccessManager,
        hmac_key: bytes,
        *,
        status_min_interval_seconds: float = 1.0,
        clock=time.monotonic,
    ) -> None:
        if not isinstance(hmac_key, (bytes, bytearray)) or not hmac_key:
            raise ValueError("access HMAC key 不能为空")
        self._manager = manager
        self._hmac_key = bytes(hmac_key)
        self._status_min_interval = max(0.0, float(status_min_interval_seconds))
        self._clock = clock
        self._poll_lock = threading.Lock()
        self._last_status_at: dict[str, float] = {}

    def _ip_key(self, client_ip: str | None) -> str:
        if client_ip is None or not str(client_ip).strip():
            raise AccessDenied(
                403,
                ERROR_ACCESS_IDENTITY_INVALID,
                "无法确认客户端身份，拒绝访问。",
            )
        try:
            normalized = normalize_ip(str(client_ip))
        except ClientIpError as exc:
            raise AccessDenied(
                403,
                ERROR_ACCESS_IDENTITY_INVALID,
                "无法确认客户端身份，拒绝访问。",
            ) from exc
        return hmac.new(
            self._hmac_key,
            _ACCESS_DOMAIN + normalized.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    # -- access API 操作 -------------------------------------------------
    def enter(self, client_ip: str | None, ticket: str | None = None) -> AccessResult:
        return self._manager.enter(self._ip_key(client_ip), ticket)

    def status(self, client_ip: str | None, ticket: str | None) -> AccessResult:
        ip_key = self._ip_key(client_ip)
        if self._status_min_interval > 0:
            now = self._clock()
            with self._poll_lock:
                last = self._last_status_at.get(ip_key)
                if last is not None:
                    elapsed = now - last
                    if elapsed < self._status_min_interval:
                        raise AccessPollThrottled(
                            self._status_min_interval - elapsed
                        )
                self._last_status_at[ip_key] = now
        return self._manager.status(ip_key, ticket)

    def claim(self, client_ip: str | None, ticket: str | None) -> AccessResult:
        return self._manager.claim(self._ip_key(client_ip), ticket or "")

    def leave(self, client_ip: str | None, ticket: str | None) -> AccessResult:
        return self._manager.leave(self._ip_key(client_ip), ticket or "")

    def cancel(self, client_ip: str | None, ticket: str | None) -> AccessResult:
        return self._manager.cancel(self._ip_key(client_ip), ticket or "")

    # -- 业务 API gate ---------------------------------------------------
    def authorize(
        self,
        client_ip: str | None,
        ticket: str | None,
    ) -> AccessResult:
        """只允许 active；queued/ready/cooling/expired 均显式拒绝。"""
        ip_key = self._ip_key(client_ip)
        if not isinstance(ticket, str) or not ticket.strip():
            raise AccessDenied(
                403,
                ERROR_ACCESS_REQUIRED,
                "需要访问票据；请先进入访问队列。",
            )
        try:
            result = self._manager.status(ip_key, ticket)
        except AccessError as exc:
            raise AccessGateUnavailable("访问状态查询失败") from exc

        if result.state == "active":
            return result
        if result.state == "queued":
            raise AccessDenied(
                429,
                ERROR_ACCESS_QUEUED,
                "当前 IP 正在排队；请等待轮到后 claim。",
                {
                    "state": "queued",
                    "position": result.position,
                    "ahead": result.ahead,
                    "ticket_remaining_seconds": result.remaining_seconds,
                    "retry_after_seconds": 2.0,
                },
                2.0,
            )
        if result.state == "ready":
            raise AccessDenied(
                409,
                ERROR_ACCESS_CLAIM_REQUIRED,
                "已轮到当前 IP；请先 claim。",
                {
                    "state": "ready",
                    "remaining_seconds": result.remaining_seconds,
                },
            )
        if result.state == "cooling":
            raise AccessDenied(
                429,
                ERROR_ACCESS_COOLING,
                "当前 IP 正在冷却，暂时不能访问。",
                {
                    "state": "cooling",
                    "retry_after_seconds": result.remaining_seconds,
                },
                result.remaining_seconds,
            )
        raise AccessDenied(
            403,
            ERROR_ACCESS_TICKET_INVALID,
            "访问票据无效或已过期。",
            {"state": "expired"},
        )
