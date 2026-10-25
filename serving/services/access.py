"""阶段 5A：TMF 访问管理核心内存状态机。

边界：
- 本模块是**纯内存、单进程**状态机；不依赖 FastAPI、SQLite、网络或日志；
- 不接入网关路由/middleware/前端；当前没有生产开关；
- 进程重启后所有 active/ready/queued/cooling 状态自然清空；
- 调用方负责用可信客户端 IP 解析结果，并生成 HMAC-SHA256 ``ip_key``；
  本模块只接受已脱敏的 ``ip_key``，不接收/不保存/不输出明文 IP；
- 使用注入 clock（默认 ``time.monotonic``）与 ticket generator；
  所有状态变更在单进程 ``threading.Lock`` 内串行化。

规则（阶段 5A 已确认）：
- 一个 IP 一个记录；active/ready 合计最多 10；
- 首次进入分配 ready，15 秒内 claim 后成为 active；
- active 固定 180 秒，不因任何请求/轮询续期；
- active 到期释放名额并进入 60 秒 cooling；cooling 不占名额、不排队；
- queued 120 秒过期，状态轮询不续期；
- FIFO 队列，唯一 IP 最多 50 个；第 51 个返回 ``queue_full``（后续映射 429 ACCESS_QUEUE_FULL）；
- 提前 leave/cancel 释放名额，不进入冷却；
- 队列位置按当前有效 ticket 动态计算。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Callable, Literal

MAX_ACTIVE_READY = 10
MAX_QUEUE = 50
ACTIVE_SECONDS = 180.0
COOLDOWN_SECONDS = 60.0
READY_SECONDS = 15.0
QUEUE_TICKET_SECONDS = 120.0

AccessState = Literal[
    "queued",
    "ready",
    "active",
    "cooling",
    "expired",
    "cancelled",
    "queue_full",
]


class AccessError(RuntimeError):
    """访问状态机显式错误基类；调用方应 fail-closed。"""


class AccessTicketError(AccessError):
    """ticket 缺失、无效或与当前 IP 不匹配。"""


class AccessStateError(AccessError):
    """操作不适用于当前状态（例如对 active 执行 cancel）。"""


@dataclass(frozen=True)
class AccessResult:
    """一次状态机操作的只读结果；不含明文 IP、不含 ticket 哈希。"""

    state: AccessState
    ticket: str | None = None
    position: int | None = None
    ahead: int | None = None
    remaining_seconds: float | None = None


@dataclass
class _Entry:
    """单 IP 内部记录；ticket 只保存 SHA-256 哈希。"""

    ticket_hash: str
    state: AccessState
    enqueued_at: float | None = None
    ready_deadline: float | None = None
    active_deadline: float | None = None
    cooldown_deadline: float | None = None


def _default_ticket() -> str:
    # 32 字节随机；URL-safe，适合 HTTP header。
    return secrets.token_urlsafe(32)


def _hash_ticket(ticket: str) -> str:
    return hashlib.sha256(ticket.encode("utf-8")).hexdigest()


class AccessManager:
    """单进程内存访问状态机。

    典型调用：
        result = manager.enter(ip_key)
        result = manager.status(ip_key, ticket)
        result = manager.claim(ip_key, ticket)
        result = manager.leave(ip_key, ticket) / manager.cancel(ip_key, ticket)
        manager.sweep()

    参数：
    - ``clock``：返回单调秒数的 callable；测试注入 fake clock；
    - ``ticket_factory``：返回不可预测 token 的 callable；测试注入 fake generator。

    时间倒退：内部维护 high-water clock，操作时间不会小于上一次时间，
    因此时间回拨不会延长 active/ready/queue/cooldown 的剩余时间。
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        ticket_factory: Callable[[], str] = _default_ticket,
        max_active_ready: int = MAX_ACTIVE_READY,
        max_queue: int = MAX_QUEUE,
    ) -> None:
        if not 1 <= int(max_active_ready) <= MAX_ACTIVE_READY:
            raise ValueError("max_active_ready 必须在 1~10 之间")
        # 生产默认 50；测试可注入更小值，但不允许放宽到 50 以上。
        if not 1 <= int(max_queue) <= MAX_QUEUE:
            raise ValueError("max_queue 必须在 1~50 之间")
        self._clock = clock
        self._ticket_factory = ticket_factory
        self._max_active_ready = int(max_active_ready)
        self._max_queue = int(max_queue)
        self._lock = threading.Lock()
        self._last_now: float | None = None
        self._entries: dict[str, _Entry] = {}
        self._queue: OrderedDict[str, None] = OrderedDict()

    # -- 内部时间/锁 -----------------------------------------------------
    def _now_locked(self, now: float | None = None) -> float:
        raw = self._clock() if now is None else now
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise AccessError("访问管理 clock 返回值非法") from exc
        if self._last_now is not None and value < self._last_now:
            # 时间回拨不得延长任何剩余时间；按 high-water 处理。
            value = self._last_now
        self._last_now = value
        return value

    def _new_ticket(self) -> tuple[str, str]:
        token = self._ticket_factory()
        if not isinstance(token, str) or not token:
            raise AccessError("访问管理 ticket generator 返回非法 token")
        return token, _hash_ticket(token)

    @staticmethod
    def _valid_ip_key(ip_key: str) -> str:
        if not isinstance(ip_key, str) or not ip_key.strip():
            # 不包含调用方传入内容，避免意外泄露身份。
            raise AccessError("ip_key 非法")
        return ip_key

    @staticmethod
    def _matches(entry: _Entry, ticket: str | None) -> bool:
        if not isinstance(ticket, str) or not ticket:
            return False
        return hmac.compare_digest(entry.ticket_hash, _hash_ticket(ticket))

    def _capacity_used_locked(self) -> int:
        return sum(
            1 for entry in self._entries.values() if entry.state in {"active", "ready"}
        )

    def _position_locked(self, ip_key: str) -> int | None:
        for index, queued_key in enumerate(self._queue, start=1):
            if queued_key == ip_key:
                return index
        return None

    def _result_locked(self, ip_key: str, entry: _Entry, now: float) -> AccessResult:
        if entry.state == "active":
            deadline = entry.active_deadline if entry.active_deadline is not None else now
            return AccessResult(
                state="active",
                position=0,
                ahead=0,
                remaining_seconds=max(0.0, deadline - now),
            )
        if entry.state == "ready":
            deadline = entry.ready_deadline if entry.ready_deadline is not None else now
            return AccessResult(
                state="ready",
                position=0,
                ahead=0,
                remaining_seconds=max(0.0, deadline - now),
            )
        if entry.state == "queued":
            position = self._position_locked(ip_key)
            if position is None:
                return AccessResult(state="expired")
            enqueued_at = entry.enqueued_at if entry.enqueued_at is not None else now
            deadline = enqueued_at + QUEUE_TICKET_SECONDS
            return AccessResult(
                state="queued",
                position=position,
                ahead=max(0, position - 1),
                remaining_seconds=max(0.0, deadline - now),
            )
        if entry.state == "cooling":
            deadline = entry.cooldown_deadline if entry.cooldown_deadline is not None else now
            return AccessResult(
                state="cooling",
                remaining_seconds=max(0.0, deadline - now),
            )
        return AccessResult(state=entry.state)

    def _promote_locked(self, now: float) -> None:
        while self._capacity_used_locked() < self._max_active_ready and self._queue:
            ip_key, _ = next(iter(self._queue.items()))
            del self._queue[ip_key]
            entry = self._entries.get(ip_key)
            if entry is None or entry.state != "queued":
                continue
            entry.state = "ready"
            entry.ready_deadline = now + READY_SECONDS

    def _sweep_locked(self, now: float) -> None:
        for ip_key, entry in list(self._entries.items()):
            if entry.state == "active":
                deadline = entry.active_deadline
                if deadline is not None and now >= deadline:
                    # 冷却从 active 到期那一刻起算，不因 sweep 延迟而延长。
                    cooldown_deadline = deadline + COOLDOWN_SECONDS
                    if now >= cooldown_deadline:
                        del self._entries[ip_key]
                    else:
                        entry.state = "cooling"
                        entry.active_deadline = None
                        entry.cooldown_deadline = cooldown_deadline
            elif entry.state == "ready":
                deadline = entry.ready_deadline
                if deadline is not None and now >= deadline:
                    del self._entries[ip_key]
            elif entry.state == "queued":
                enqueued_at = entry.enqueued_at if entry.enqueued_at is not None else now
                deadline = enqueued_at + QUEUE_TICKET_SECONDS
                if now >= deadline:
                    self._queue.pop(ip_key, None)
                    del self._entries[ip_key]
            elif entry.state == "cooling":
                deadline = entry.cooldown_deadline
                if deadline is not None and now >= deadline:
                    del self._entries[ip_key]
        self._promote_locked(now)

    # -- 公开操作 --------------------------------------------------------
    def enter(self, ip_key: str, ticket: str | None = None) -> AccessResult:
        """进入访问名额。

        - 冷却中：返回 cooling，不排队；
        - 已有 active/ready：复用现有记录；
        - 已有 queued：复用位置；
        - 有空位：创建 ready 并返回 ticket；
        - 无空位：创建 FIFO queued ticket。

        重复 enter 不产生第二个记录/名额。若已有记录且未提供 ticket，
        会轮换该记录的 ticket，让刷新/丢失 localStorage 的页面恢复；
        若提供不匹配 ticket，则 fail-closed。
        """
        key = self._valid_ip_key(ip_key)
        with self._lock:
            now = self._now_locked()
            self._sweep_locked(now)
            entry = self._entries.get(key)

            if entry is not None:
                if entry.state == "cooling":
                    return self._result_locked(key, entry, now)
                if ticket is not None:
                    if not self._matches(entry, ticket):
                        raise AccessTicketError("access ticket 无效")
                    return self._result_with_ticket(key, entry, now, ticket)
                # 无 ticket 的重复 enter：复用记录但轮换 ticket。
                new_ticket, new_hash = self._new_ticket()
                entry.ticket_hash = new_hash
                return self._result_with_ticket(key, entry, now, new_ticket)

            if self._capacity_used_locked() < self._max_active_ready:
                token, token_hash = self._new_ticket()
                entry = _Entry(
                    ticket_hash=token_hash,
                    state="ready",
                    ready_deadline=now + READY_SECONDS,
                )
                self._entries[key] = entry
                return self._result_with_ticket(key, entry, now, token)

            # 队列容量只计算当前有效 queued 条目；sweep 已在上面完成。
            if len(self._queue) >= self._max_queue:
                return AccessResult(state="queue_full")

            token, token_hash = self._new_ticket()
            entry = _Entry(
                ticket_hash=token_hash,
                state="queued",
                enqueued_at=now,
            )
            self._entries[key] = entry
            self._queue[key] = None
            return self._result_with_ticket(key, entry, now, token)

    def _result_with_ticket(
        self, ip_key: str, entry: _Entry, now: float, ticket: str
    ) -> AccessResult:
        result = self._result_locked(ip_key, entry, now)
        return AccessResult(
            state=result.state,
            ticket=ticket,
            position=result.position,
            ahead=result.ahead,
            remaining_seconds=result.remaining_seconds,
        )

    def status(self, ip_key: str, ticket: str | None) -> AccessResult:
        """查询状态；状态查询不续期任何 TTL。"""
        key = self._valid_ip_key(ip_key)
        with self._lock:
            now = self._now_locked()
            self._sweep_locked(now)
            entry = self._entries.get(key)
            if entry is None:
                return AccessResult(state="expired")
            if entry.state == "cooling":
                if ticket is not None and not self._matches(entry, ticket):
                    return AccessResult(state="expired")
                return self._result_locked(key, entry, now)
            if not self._matches(entry, ticket):
                return AccessResult(state="expired")
            return self._result_locked(key, entry, now)

    def claim(self, ip_key: str, ticket: str) -> AccessResult:
        """将 ready 转 active；等待超过 15s、错误 IP 或错误 ticket 均失败。"""
        key = self._valid_ip_key(ip_key)
        with self._lock:
            now = self._now_locked()
            self._sweep_locked(now)
            entry = self._entries.get(key)
            if entry is None or not self._matches(entry, ticket):
                raise AccessTicketError("access ticket 无效")
            if entry.state == "active":
                # 幂等：重复 claim 不重置 180s。
                return self._result_locked(key, entry, now)
            if entry.state != "ready":
                raise AccessStateError("当前状态不能 claim")
            entry.state = "active"
            entry.ready_deadline = None
            entry.active_deadline = now + ACTIVE_SECONDS
            return self._result_locked(key, entry, now)

    def leave(self, ip_key: str, ticket: str) -> AccessResult:
        """本人提前离开 active/ready，或取消 queued；不进入冷却。"""
        key = self._valid_ip_key(ip_key)
        with self._lock:
            now = self._now_locked()
            self._sweep_locked(now)
            entry = self._entries.get(key)
            if entry is None or not self._matches(entry, ticket):
                raise AccessTicketError("access ticket 无效")
            if entry.state == "cooling":
                return self._result_locked(key, entry, now)
            if entry.state not in {"active", "ready", "queued"}:
                raise AccessStateError("当前状态不能 leave")
            self._queue.pop(key, None)
            del self._entries[key]
            self._promote_locked(now)
            return AccessResult(state="cancelled")

    def cancel(self, ip_key: str, ticket: str) -> AccessResult:
        """仅取消 queued ticket；active/ready 应使用 leave。"""
        key = self._valid_ip_key(ip_key)
        with self._lock:
            now = self._now_locked()
            self._sweep_locked(now)
            entry = self._entries.get(key)
            if entry is None or not self._matches(entry, ticket):
                raise AccessTicketError("access ticket 无效")
            if entry.state != "queued":
                raise AccessStateError("当前状态不能 cancel")
            self._queue.pop(key, None)
            del self._entries[key]
            return AccessResult(state="cancelled")

    def sweep(self, now: float | None = None) -> None:
        """清理过期状态并晋升队列；可重复调用，幂等。"""
        with self._lock:
            current = self._now_locked(now)
            self._sweep_locked(current)
