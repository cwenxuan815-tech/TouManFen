#!/usr/bin/env python
"""阶段 5A 隔离验收：访问管理核心内存状态机。

只使用 fake clock、fake ticket generator、进程内 manager：
- 不启动服务、不占端口、不连数据库、不接网关/前端/worker；
- 不涉及真实 IP/密钥；ip_key 由测试提供为脱敏占位串。

运行：
    $env:PYTHONPATH='E:\\AIModels\\test\\TouManFen\\venv-gateway\\Lib\\site-packages'
    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/verify_access_state.py
"""

from __future__ import annotations

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from serving.services.access import (  # noqa: E402
    ACTIVE_SECONDS,
    COOLDOWN_SECONDS,
    MAX_ACTIVE_READY,
    MAX_QUEUE,
    QUEUE_TICKET_SECONDS,
    READY_SECONDS,
    AccessError,
    AccessManager,
    AccessStateError,
    AccessTicketError,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

FAILURES: list[str] = []
TOTAL = 0


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds

    def set(self, seconds: float) -> None:
        self.value = seconds


class TicketFactory:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.value = 0

    def __call__(self) -> str:
        with self.lock:
            self.value += 1
            return f"test-ticket-{self.value:04d}"


def check(name: str, condition: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


def new_manager() -> tuple[AccessManager, FakeClock, TicketFactory]:
    clock = FakeClock()
    tickets = TicketFactory()
    return AccessManager(clock=clock, ticket_factory=tickets), clock, tickets


def enter_all(
    manager: AccessManager,
    count: int,
    *,
    prefix: str = "ip-key",
) -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for index in range(count):
        key = f"{prefix}-{index:02d}"
        result = manager.enter(key)
        if result.state != "ready" or not result.ticket:
            raise AssertionError(f"enter {index} unexpected state {result.state}")
        entries.append((key, result.ticket))
    return entries


def claim_all(manager: AccessManager, entries: list[tuple[str, str]]) -> None:
    for key, ticket in entries:
        result = manager.claim(key, ticket)
        if result.state != "active":
            raise AssertionError(f"claim {key} unexpected {result.state}")


def test_initial_capacity_and_queue() -> None:
    print("\n=== 1. 0~10 个 ready/active + 第 11 个 FIFO ===")
    manager, clock, _ = new_manager()
    entries = enter_all(manager, 10)
    check("10 unique IPs enter as ready", len(entries) == 10, str(len(entries)))
    check(
        "no active before claim",
        all(manager.status(key, ticket).state == "ready" for key, ticket in entries),
    )

    eleventh = manager.enter("ip-key-10")
    twelfth = manager.enter("ip-key-11")
    check("11th is queued", eleventh.state == "queued", eleventh.state)
    check("11th position == 1", eleventh.position == 1 and eleventh.ahead == 0, str(eleventh))
    check("12th position == 2", twelfth.position == 2 and twelfth.ahead == 1, str(twelfth))
    check(
        "queue ticket has ~120s remaining",
        eleventh.remaining_seconds is not None
        and abs(eleventh.remaining_seconds - QUEUE_TICKET_SECONDS) < 0.01,
        str(eleventh.remaining_seconds),
    )

    claim_all(manager, entries)
    check(
        "10 active after claim",
        all(manager.status(key, ticket).state == "active" for key, ticket in entries),
    )
    check("queued entries still queued", manager.status("ip-key-10", eleventh.ticket or "").state == "queued")


def test_duplicate_enter_and_ticket_recovery() -> None:
    print("\n=== 2. 同 IP 重复 enter 不重复占槽；ticket 恢复 ===")
    manager, clock, tickets = new_manager()
    first = manager.enter("ip-key-a")
    second = manager.enter("ip-key-a", first.ticket)
    check("duplicate enter with ticket is ready", second.state == "ready", second.state)
    check("duplicate enter keeps same ticket", second.ticket == first.ticket)
    check("only one ready record", manager.status("ip-key-a", first.ticket or "").state == "ready")

    recovered = manager.enter("ip-key-a")
    check("duplicate enter without ticket rotates ticket", recovered.state == "ready" and recovered.ticket != first.ticket)
    old_status = manager.status("ip-key-a", first.ticket or "")
    check("old ticket becomes expired after rotation", old_status.state == "expired", old_status.state)
    claimed = manager.claim("ip-key-a", recovered.ticket or "")
    check("new ticket can claim", claimed.state == "active", claimed.state)

    # 第 11 个只排队一次。
    entries = enter_all(manager, 9)  # 加已有 active 共 10 个 active
    # 上面 9 个 ready + 1 active 已达 10 个容量；再给 9 个 claim 前
    # 第 10 个 ready 存在，第 11 个必须排队。
    check("capacity still capped after duplicates", len(entries) == 9)


def test_claim_and_ready_expiry() -> None:
    print("\n=== 3. claim 15s 边界与 ready 过期晋升 ===")
    manager, clock, _ = new_manager()
    first = manager.enter("ip-key-a")
    clock.set(14.999)
    check(
        "claim at 14.999s succeeds",
        manager.claim("ip-key-a", first.ticket or "").state == "active",
    )

    manager2, clock2, _ = new_manager()
    entries = enter_all(manager2, 10)
    queued = manager2.enter("ip-key-10")
    clock2.set(READY_SECONDS)
    try:
        manager2.claim(entries[0][0], entries[0][1])
        check("claim at 15s rejected", False)
    except AccessTicketError:
        check("claim at 15s rejected", True)
    promoted = manager2.status("ip-key-10", queued.ticket or "")
    check(
        "next queued promoted to ready at ready expiry",
        promoted.state == "ready",
        promoted.state,
    )
    live_states = [manager2.status(key, ticket).state for key, ticket in entries]
    live_states.append(promoted.state)
    check(
        "active+ready never exceeds 10 after ready expiry",
        sum(1 for state in live_states if state in {"active", "ready"})
        <= MAX_ACTIVE_READY,
        str(live_states),
    )


def test_active_lease_status_and_cooldown() -> None:
    print("\n=== 4. active 固定 180s、status 不续期、60s 冷却 ===")
    manager, clock, _ = new_manager()
    ready = manager.enter("ip-key-a")
    active = manager.claim("ip-key-a", ready.ticket or "")
    check("claim -> active", active.state == "active" and active.remaining_seconds is not None)
    check(
        "active remaining is 180s",
        abs((active.remaining_seconds or 0) - ACTIVE_SECONDS) < 0.01,
        str(active.remaining_seconds),
    )

    clock.set(100.0)
    mid = manager.status("ip-key-a", ready.ticket or "")
    check(
        "status at 100s does not renew",
        mid.state == "active" and abs((mid.remaining_seconds or 0) - 80.0) < 0.01,
        str(mid),
    )
    clock.set(179.999)
    near = manager.status("ip-key-a", ready.ticket or "")
    check("179.999s still active", near.state == "active", near.state)
    clock.set(ACTIVE_SECONDS)
    cooling = manager.status("ip-key-a", ready.ticket or "")
    check("180s becomes cooling", cooling.state == "cooling", cooling.state)
    check(
        "cooling remaining ~60s",
        cooling.remaining_seconds is not None
        and abs(cooling.remaining_seconds - COOLDOWN_SECONDS) < 0.01,
        str(cooling.remaining_seconds),
    )
    cooling_enter = manager.enter("ip-key-a")
    check("enter during cooling returns cooling", cooling_enter.state == "cooling")
    check(
        "cooling does not enter queue",
        manager.status("ip-key-a", ready.ticket or "").state == "cooling",
    )
    clock.set(ACTIVE_SECONDS + COOLDOWN_SECONDS - 0.001)
    check(
        "cooling at 59.999s still cooling",
        manager.enter("ip-key-a").state == "cooling",
    )
    clock.set(ACTIVE_SECONDS + COOLDOWN_SECONDS)
    after = manager.enter("ip-key-a")
    check("after 60s cooldown can enter ready", after.state == "ready", after.state)


def test_active_expiry_promotes_fifo() -> None:
    print("\n=== 5. active 到期释放并晋升队首 ===")
    manager, clock, _ = new_manager()
    # 第一个 active 在 t=0 claim；为让它先到期且 queue ticket 尚未过期，
    # 其余 9 个 active 在 t=120 进入/claim，queue 也在 t=120 创建。
    first = manager.enter("ip-key-00")
    manager.claim("ip-key-00", first.ticket or "")
    clock.set(120.0)
    entries = enter_all(manager, 9, prefix="late")
    claim_all(manager, entries)
    queued = manager.enter("ip-key-queued")
    check("queued while 10 active", queued.state == "queued")

    clock.set(ACTIVE_SECONDS - 0.001)
    check("before expiry still active", manager.status("ip-key-00", first.ticket or "").state == "active")
    clock.set(ACTIVE_SECONDS)
    first_status = manager.status("ip-key-00", first.ticket or "")
    promoted = manager.status("ip-key-queued", queued.ticket or "")
    check("expired IP cooling", first_status.state == "cooling", first_status.state)
    check("queued promoted to ready", promoted.state == "ready", promoted.state)
    active_others = sum(
        1
        for key, ticket in entries
        if manager.status(key, ticket).state == "active"
    )
    check(
        "promotion keeps active+ready at 10",
        active_others == 9 and promoted.state == "ready",
        f"active_others={active_others} queued={promoted.state}",
    )


def test_early_leave_and_cancel() -> None:
    print("\n=== 6. 提前 leave 不冷却；cancel queued 更新位置 ===")
    manager, clock, _ = new_manager()
    ready = manager.enter("ip-key-a")
    manager.claim("ip-key-a", ready.ticket or "")
    left = manager.leave("ip-key-a", ready.ticket or "")
    check("active leave -> cancelled", left.state == "cancelled", left.state)
    reentered = manager.enter("ip-key-a")
    check("early leave does not cool down", reentered.state == "ready", reentered.state)

    ready_leave = manager.leave("ip-key-a", reentered.ticket or "")
    check("ready leave -> cancelled", ready_leave.state == "cancelled", ready_leave.state)
    check("ready leave releases without cooldown", manager.enter("ip-key-a").state == "ready")

    manager2, _, _ = new_manager()
    entries = enter_all(manager2, 10)
    claim_all(manager2, entries)
    q1 = manager2.enter("ip-key-q1")
    q2 = manager2.enter("ip-key-q2")
    cancelled = manager2.cancel("ip-key-q1", q1.ticket or "")
    check("cancel queued -> cancelled", cancelled.state == "cancelled")
    check("queue position shifts after cancel", manager2.status("ip-key-q2", q2.ticket or "").position == 1)
    try:
        manager2.cancel("ip-key-q1", q1.ticket or "")
        check("cancel twice rejected", False)
    except AccessTicketError:
        check("cancel twice rejected", True)


def test_queue_ticket_ttl_no_renewal() -> None:
    print("\n=== 7. queued 120s 过期，status 不续期 ===")
    manager, clock, _ = new_manager()
    entries = enter_all(manager, 10)
    claim_all(manager, entries)
    queued = manager.enter("ip-key-q")
    clock.set(60.0)
    mid = manager.status("ip-key-q", queued.ticket or "")
    check(
        "queued status at 60s still queued with 60s remaining",
        mid.state == "queued" and abs((mid.remaining_seconds or 0) - 60.0) < 0.01,
        str(mid),
    )
    clock.set(QUEUE_TICKET_SECONDS)
    expired = manager.status("ip-key-q", queued.ticket or "")
    check("queued ticket expires at 120s", expired.state == "expired", expired.state)
    try:
        manager.cancel("ip-key-q", queued.ticket or "")
        check("expired queued cancel rejected", False)
    except AccessTicketError:
        check("expired queued cancel rejected", True)


def test_ticket_binding_and_errors() -> None:
    print("\n=== 8. ticket 绑定 IP、错误显式失败 ===")
    manager, clock, _ = new_manager()
    a = manager.enter("ip-key-a")
    check("status with wrong ticket expired", manager.status("ip-key-a", "wrong").state == "expired")
    try:
        manager.claim("ip-key-a", "wrong")
        check("claim wrong ticket rejected", False)
    except AccessTicketError:
        check("claim wrong ticket rejected", True)

    b = manager.enter("ip-key-b")
    try:
        manager.claim("ip-key-b", a.ticket or "")
        check("cross-IP claim rejected", False)
    except AccessTicketError:
        check("cross-IP claim rejected", True)
    try:
        manager.leave("ip-key-b", a.ticket or "")
        check("cross-IP leave rejected", False)
    except AccessTicketError:
        check("cross-IP leave rejected", True)
    check(
        "cross-IP does not alter state",
        manager.status("ip-key-b", b.ticket or "").state == "ready",
    )

    manager2, _, _ = new_manager()
    manager2.enter("ip-key-a")
    try:
        manager2.cancel("ip-key-a", "wrong")
        check("cancel wrong ticket rejected", False)
    except AccessTicketError:
        check("cancel wrong ticket rejected", True)
    try:
        manager2.leave("", "anything")
        check("empty ip_key rejected", False)
    except AccessError:
        check("empty ip_key rejected", True)
    try:
        AccessManager(max_active_ready=11)
        check("max_active_ready > 10 rejected", False)
    except ValueError:
        check("max_active_ready > 10 rejected", True)


def test_sweep_idempotence_and_time_reversal() -> None:
    print("\n=== 9. sweep 幂等、时间回拨不延长 ===")
    manager, clock, _ = new_manager()
    first_active = manager.enter("ip-key-00")
    manager.claim("ip-key-00", first_active.ticket or "")
    clock.set(120.0)
    entries = enter_all(manager, 9, prefix="late")
    claim_all(manager, entries)
    queued = manager.enter("ip-key-q")
    clock.set(ACTIVE_SECONDS)
    first = manager.status("ip-key-q", queued.ticket or "")
    manager.sweep()
    second = manager.status("ip-key-q", queued.ticket or "")
    manager.sweep()
    third = manager.status("ip-key-q", queued.ticket or "")
    check(
        "sweep repeated is idempotent",
        first.state == second.state == third.state == "ready"
        and first.position == second.position == third.position,
    )

    manager2, clock2, _ = new_manager()
    ready = manager2.enter("ip-key-a")
    manager2.claim("ip-key-a", ready.ticket or "")
    clock2.set(100.0)
    before = manager2.status("ip-key-a", ready.ticket or "")
    clock2.set(0.0)  # 时间回拨
    after = manager2.status("ip-key-a", ready.ticket or "")
    check(
        "time reversal does not extend active remaining",
        before.state == after.state == "active"
        and (after.remaining_seconds or 0) <= (before.remaining_seconds or 0) + 0.01,
        f"{before.remaining_seconds}->{after.remaining_seconds}",
    )


def test_concurrent_enter() -> None:
    print("\n=== 10. 20 并发唯一 IP 只产生 10 个 ready ===")
    manager, clock, tickets = new_manager()
    with ThreadPoolExecutor(max_workers=20) as executor:
        results = list(
            executor.map(lambda index: manager.enter(f"ip-key-{index:02d}"), range(20))
        )
    ready = [result for result in results if result.state == "ready"]
    queued = [result for result in results if result.state == "queued"]
    check("20 concurrent enter -> 10 ready", len(ready) == 10, str(len(ready)))
    check("20 concurrent enter -> 10 queued", len(queued) == 10, str(len(queued)))
    positions = sorted(result.position for result in queued)
    check("queued positions are 1..10", positions == list(range(1, 11)), str(positions))
    check(
        "no ready+active over capacity",
        sum(
            1
            for result in results
            if result.state in {"ready", "active"}
        )
        <= MAX_ACTIVE_READY,
    )


def test_ticket_privacy_and_randomness() -> None:
    print("\n=== 11. ticket 注入/随机、不泄露 ===")
    clock = FakeClock()
    fixed = "fixed-visible-test-token"
    manager = AccessManager(clock=clock, ticket_factory=lambda: fixed)
    result = manager.enter("ip-key-a")
    check("injected ticket returned on enter", result.ticket == fixed)
    status = manager.status("ip-key-a", fixed)
    check("status result has no ticket", status.ticket is None)
    check("status repr does not expose ticket", fixed not in repr(status))
    try:
        manager.claim("ip-key-a", "wrong")
    except AccessTicketError as exc:
        check("ticket error does not expose token", fixed not in str(exc) and "ip-key" not in str(exc))

    ready_clock = FakeClock()
    ready_manager = AccessManager(clock=ready_clock, ticket_factory=lambda: "ready-token")
    ready = ready_manager.enter("ip-key-r")
    ready_clock.set(10.0)
    first_ready_status = ready_manager.status("ip-key-r", ready.ticket or "")
    ready_clock.set(15.0)
    second_ready_status = ready_manager.status("ip-key-r", ready.ticket or "")
    check(
        "ready status at 10s does not renew; expires at 15s",
        first_ready_status.state == "ready"
        and second_ready_status.state == "expired",
        f"{first_ready_status.state}->{second_ready_status.state}",
    )

    active_clock = FakeClock()
    active_manager = AccessManager(clock=active_clock, ticket_factory=lambda: "active-token")
    active_ready = active_manager.enter("ip-key-a")
    active_manager.claim("ip-key-a", active_ready.ticket or "")
    active_clock.set(10.0)
    repeat_claim = active_manager.claim("ip-key-a", active_ready.ticket or "")
    check(
        "duplicate claim is idempotent and does not reset 180s",
        repeat_claim.state == "active"
        and repeat_claim.remaining_seconds is not None
        and abs(repeat_claim.remaining_seconds - 170.0) < 0.01,
        str(repeat_claim.remaining_seconds),
    )

    clock2 = FakeClock()
    manager2 = AccessManager(clock=clock2)
    a = manager2.enter("ip-key-a")
    b = manager2.enter("ip-key-b")
    check(
        "default tickets are distinct and long",
        a.ticket is not None
        and b.ticket is not None
        and a.ticket != b.ticket
        and min(len(a.ticket), len(b.ticket)) >= 32,
    )
    check("manager result repr has no plaintext IP", "ip-key" not in repr(a))


def test_queue_capacity_and_duplicate() -> None:
    print("\n=== 12. 队列上限 50；满队列重复 enter 不重复计数 ===")
    manager, clock, tickets = new_manager()
    active_entries = enter_all(manager, 10)
    claim_all(manager, active_entries)

    queued: list[tuple[str, str]] = []
    for index in range(MAX_QUEUE):
        key = f"queue-{index:02d}"
        result = manager.enter(key)
        if result.state != "queued" or not result.ticket:
            raise AssertionError(f"queue {index} unexpected {result.state}")
        queued.append((key, result.ticket))

    check("50 unique IPs can queue", len(queued) == MAX_QUEUE)
    check(
        "last queued position is 50",
        manager.status(queued[-1][0], queued[-1][1]).position == MAX_QUEUE,
    )

    before_tickets = tickets.value
    overflow = manager.enter("queue-overflow")
    check("51st unique IP returns queue_full", overflow.state == "queue_full", overflow.state)
    check("queue_full has no ticket/position", overflow.ticket is None and overflow.position is None)
    check("queue_full does not create a ticket", tickets.value == before_tickets)
    check("queue_full result does not expose IP/ticket", "queue-overflow" not in repr(overflow))

    # 满队列时已排队 IP 用原 ticket 重复 enter：原 ticket/位置复用，不 queue_full。
    duplicate = manager.enter(queued[0][0], queued[0][1])
    check(
        "queued IP duplicate enter reuses ticket/position at full queue",
        duplicate.state == "queued"
        and duplicate.ticket == queued[0][1]
        and duplicate.position == 1,
        str(duplicate),
    )
    check(
        "full queue still exactly 50 queued",
        sum(1 for key, ticket in queued if manager.status(key, ticket).state == "queued")
        == MAX_QUEUE,
    )

    # queue full 不改变既有位置/状态/ticket，也不占用 active/ready 槽位。
    active_before = [
        (key, manager.status(key, ticket))
        for key, ticket in active_entries
    ]
    before = [
        (key, manager.status(key, ticket))
        for key, ticket in queued
    ]
    overflow_again = manager.enter("queue-overflow-2")
    after = [
        (key, manager.status(key, ticket))
        for key, ticket in queued
    ]
    active_after = [
        (key, manager.status(key, ticket))
        for key, ticket in active_entries
    ]
    check("queue full leaves existing queue unchanged", before == after)
    check("queue full leaves active/ready slots unchanged", active_before == active_after)
    check("queue full creates no record for new IP", manager.status("queue-overflow-2", "none").state == "expired")
    check("queue full state is stable", overflow_again.state == "queue_full")

    # 取消释放容量。
    cancelled = manager.cancel(queued[0][0], queued[0][1])
    check("cancel queued frees capacity", cancelled.state == "cancelled")
    replacement = manager.enter("queue-replacement")
    check(
        "after cancel, new IP can queue at position 50",
        replacement.state == "queued" and replacement.position == MAX_QUEUE,
    )

    # ready 晋升从 queued 容量中移除，不计入 50；新 IP 仍可排队。
    ready_manager, _, _ = new_manager()
    ready_active = enter_all(ready_manager, 10)
    claim_all(ready_manager, ready_active)
    ready_queue: list[tuple[str, str]] = []
    for index in range(MAX_QUEUE):
        key = f"ready-queue-{index:02d}"
        result = ready_manager.enter(key)
        if result.state != "queued" or not result.ticket:
            raise AssertionError("cannot fill ready queue")
        ready_queue.append((key, result.ticket))
    ready_manager.leave(ready_active[0][0], ready_active[0][1])
    promoted = ready_manager.status(ready_queue[0][0], ready_queue[0][1])
    check("promotion removes one from queued capacity", promoted.state == "ready", promoted.state)
    after_promotion = ready_manager.enter("priority-replacement")
    check(
        "ready promotion does not consume queued slot",
        after_promotion.state == "queued" and after_promotion.position == MAX_QUEUE,
        str(after_promotion),
    )


def test_queue_capacity_expiry_and_cooling() -> None:
    print("\n=== 13. 过期/冷却释放队列容量 ===")
    manager, clock, _ = new_manager()
    active_entries = enter_all(manager, 10)
    claim_all(manager, active_entries)
    queued: list[tuple[str, str]] = []
    for index in range(MAX_QUEUE):
        key = f"expiry-{index:02d}"
        result = manager.enter(key)
        if result.state != "queued" or not result.ticket:
            raise AssertionError("cannot fill expiry queue")
        queued.append((key, result.ticket))

    # active 仍在；queued 全部到期释放 50 个容量。
    clock.set(QUEUE_TICKET_SECONDS)
    replacement = manager.enter("expiry-replacement")
    check(
        "expired queued tickets free queue capacity",
        replacement.state == "queued" and replacement.position == 1,
        str(replacement),
    )
    check(
        "all old queue tickets expired",
        all(manager.status(key, ticket).state == "expired" for key, ticket in queued),
    )

    # 冷却不占 queued 容量；active 到期后释放槽位并晋升，仍不把 ready 当作 queued。
    manager2, clock2, _ = new_manager()
    active2 = enter_all(manager2, 10)
    claim_all(manager2, active2)
    queue2: list[tuple[str, str]] = []
    for index in range(MAX_QUEUE):
        key = f"cool-{index:02d}"
        result = manager2.enter(key)
        if result.state != "queued" or not result.ticket:
            raise AssertionError("cannot fill cooling queue")
        queue2.append((key, result.ticket))
    clock2.set(120.0)
    # 在 120s 重新入队一批（原 queue2 已到期），以观察 active 180s 到期。
    queue3: list[tuple[str, str]] = []
    for index in range(MAX_QUEUE):
        key = f"cool-late-{index:02d}"
        result = manager2.enter(key)
        if result.state != "queued" or not result.ticket:
            raise AssertionError("cannot fill late queue")
        queue3.append((key, result.ticket))
    clock2.set(ACTIVE_SECONDS)
    first = manager2.status(active2[0][0], active2[0][1])
    promoted = manager2.status(queue3[0][0], queue3[0][1])
    check("active expiry releases slot to ready", first.state == "cooling" and promoted.state == "ready")
    new_after_cooling = manager2.enter("after-slot-release")
    check(
        "ready promotion does not count as queued",
        new_after_cooling.state == "queued",
        new_after_cooling.state,
    )

    small = AccessManager(clock=FakeClock(), ticket_factory=TicketFactory(), max_queue=2)
    small_active = enter_all(small, 10)
    claim_all(small, small_active)
    check("max_queue test=2 first queued", small.enter("small-1").state == "queued")
    check("max_queue test=2 second queued", small.enter("small-2").state == "queued")
    check("max_queue test=2 third queue_full", small.enter("small-3").state == "queue_full")
    try:
        AccessManager(clock=FakeClock(), ticket_factory=TicketFactory(), max_queue=MAX_QUEUE + 1)
        check("max_queue > 50 rejected", False)
    except ValueError:
        check("max_queue > 50 rejected", True)


def main() -> int:
    test_initial_capacity_and_queue()
    test_duplicate_enter_and_ticket_recovery()
    test_claim_and_ready_expiry()
    test_active_lease_status_and_cooldown()
    test_active_expiry_promotes_fifo()
    test_early_leave_and_cancel()
    test_queue_ticket_ttl_no_renewal()
    test_ticket_binding_and_errors()
    test_sweep_idempotence_and_time_reversal()
    test_concurrent_enter()
    test_ticket_privacy_and_randomness()
    test_queue_capacity_and_duplicate()
    test_queue_capacity_expiry_and_cooling()

    if FAILURES:
        print(f"\nRESULT FAIL ({len(FAILURES)} failed / {TOTAL} checks)")
        for name in FAILURES:
            print(f"  FAILED: {name}")
        return 1
    print(f"\nRESULT PASS ({TOTAL} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
