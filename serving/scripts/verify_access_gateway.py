#!/usr/bin/env python
"""阶段 5B 隔离验收：access API + 业务 API gate + SSE 到期。

全部使用临时 app/临时内存 manager/mock classifier：
- 不启动 uvicorn、不占端口、不连真实 worker/Provider；
- 使用临时 TMF_DB_PATH 和运行时随机 HMAC key；
- 不写正式 serving/run/tmf.db。

运行：
    $env:PYTHONPATH='E:\\AIModels\\test\\TouManFen\\venv-gateway\\Lib\\site-packages'
    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/verify_access_gateway.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from serving.gateway.access_config import AccessConfigError, load_access_settings  # noqa: E402
from serving.gateway.access_gate import (  # noqa: E402
    ACCESS_TICKET_HEADER,
    AccessGate,
    AccessPollThrottled,
    is_access_exempt,
)
from serving.gateway.errors import (  # noqa: E402
    ERROR_ACCESS_COOLING,
    ERROR_ACCESS_QUEUE_FULL,
    ERROR_ACCESS_REQUIRED,
    ERROR_ACCESS_TICKET_INVALID,
)
from serving.gateway.main import create_app  # noqa: E402
from serving.services.access import AccessManager  # noqa: E402
from serving.services.classification import (  # noqa: E402
    StreamModelEvent,
    StreamSummaryEvent,
)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

TEST_KEY = secrets.token_hex(32)
IP = "203.0.113.10"
OTHER_IP = "198.51.100.99"
FAILURES: list[str] = []
TOTAL = 0
OUTPUTS: list[str] = []
LOG_ENTRIES: list[str] = []


class CaptureGatewayLogs(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        LOG_ENTRIES.append(
            record.getMessage()
            + " "
            + repr(
                {
                    key: value
                    for key, value in record.__dict__.items()
                    if key
                    not in {
                        "name",
                        "msg",
                        "args",
                        "levelname",
                        "levelno",
                        "pathname",
                        "filename",
                        "module",
                        "exc_info",
                        "exc_text",
                        "stack_info",
                        "lineno",
                        "funcName",
                        "created",
                        "msecs",
                        "relativeCreated",
                        "thread",
                        "threadName",
                        "processName",
                        "process",
                    }
                }
            )
        )


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def set(self, value: float) -> None:
        self.value = value

    def advance(self, value: float) -> None:
        self.value += value


class Tickets:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"access-test-ticket-{self.value:04d}"


class FakeClassifier:
    """只实现 stream 所需接口，避免任何 worker 调用。"""

    def __init__(self) -> None:
        self.stream_calls = 0

    def preflight_llm_request(self, models, client_ip) -> None:
        return None

    async def compare_stream(self, **kwargs):
        self.stream_calls += 1
        await asyncio.sleep(1.0)
        yield StreamModelEvent(
            model_id="rf",
            status="ok",
            latency_ms=1,
            pred_class="finance",
            data_source="mock",
        )
        yield StreamSummaryEvent(
            status="ok",
            total=1,
            ok=1,
            failed=0,
            elapsed_ms=1,
            total_timeout_ms=1000,
            partial=False,
        )


def check(name: str, condition: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


def code_of(response) -> str | None:
    try:
        return response.json().get("error", {}).get("code")
    except Exception:
        return None


def clear_env() -> None:
    for key in (
        "TMF_ACCESS_CONTROL_ENABLED",
        "TMF_ACCESS_STATUS_MIN_INTERVAL_MS",
        "TMF_DB_PATH",
        "TMF_LLM_QUOTA_HMAC_KEY",
        "TMF_TRUSTED_PROXY_CIDRS",
        "TMF_CORS_ORIGINS",
    ):
        os.environ.pop(key, None)


def configure_env(enabled: bool, db_path: Path) -> None:
    clear_env()
    os.environ["TMF_DB_PATH"] = str(db_path)
    os.environ["TMF_ACCESS_CONTROL_ENABLED"] = "true" if enabled else "false"
    os.environ["TMF_ACCESS_STATUS_MIN_INTERVAL_MS"] = "0"
    if enabled:
        os.environ["TMF_LLM_QUOTA_HMAC_KEY"] = TEST_KEY


def new_fake_gate(status_min_interval_seconds: float = 0.0) -> tuple[AccessGate, FakeClock]:
    clock = FakeClock()
    gate = AccessGate(
        AccessManager(clock=clock, ticket_factory=Tickets()),
        TEST_KEY.encode("utf-8"),
        status_min_interval_seconds=status_min_interval_seconds,
        clock=clock,
    )
    return gate, clock


def install_fake_classifier(app) -> FakeClassifier:
    fake = FakeClassifier()
    app.state.classifier = fake
    return fake


def enter_headers(ticket: str) -> dict[str, str]:
    return {ACCESS_TICKET_HEADER: ticket}


def test_disabled_compat(root: Path) -> None:
    print("\n=== 1. 默认关闭：旧 API 兼容、access API 不可用 ===")
    configure_env(False, root / "disabled.db")
    app = create_app()
    with TestClient(app, client=(IP, 50000)) as client:
        stats = client.get("/api/v1/stats")
        health = client.get("/api/v1/healthz")
        enter = client.post("/api/v1/access/enter")
        OUTPUTS.extend([stats.text, health.text, enter.text])
        check("disabled: /stats remains 200", stats.status_code == 200, str(stats.status_code))
        check("disabled: health remains 200", health.status_code == 200, str(health.status_code))
        check(
            "disabled: access API returns 404 ACCESS_DISABLED",
            enter.status_code == 404 and code_of(enter) == "ACCESS_DISABLED",
            f"{enter.status_code}/{code_of(enter)}",
        )
        check("disabled: no access manager created", app.state.access_gate is None)


def test_enabled_flow_and_route_coverage(root: Path) -> None:
    print("\n=== 2. 启用：enter/claim/active、业务 gate 与访问例外 ===")
    configure_env(True, root / "enabled.db")
    app = create_app()
    with TestClient(app, client=(IP, 50000)) as client:
        gate, clock = new_fake_gate()
        app.state.access_gate = gate

        enter = client.post("/api/v1/access/enter")
        check("enter returns ready", enter.status_code == 200 and enter.json().get("state") == "ready", enter.text)
        ticket = enter.json().get("ticket")
        check("enter returns a ticket", isinstance(ticket, str) and bool(ticket))

        claim = client.post("/api/v1/access/claim", headers=enter_headers(ticket or ""))
        check("claim returns active 180s", claim.status_code == 200 and claim.json().get("state") == "active", claim.text)
        check(
            "active remaining ~180s",
            abs(float(claim.json().get("remaining_seconds") or 0) - 180.0) < 0.1,
            str(claim.json().get("remaining_seconds")),
        )

        active_stats = client.get("/api/v1/stats", headers=enter_headers(ticket or ""))
        check("active ticket can access business API", active_stats.status_code == 200, str(active_stats.status_code))
        no_ticket_stats = client.get("/api/v1/stats")
        check(
            "business API without ticket denied",
            no_ticket_stats.status_code == 403 and code_of(no_ticket_stats) == ERROR_ACCESS_REQUIRED,
            f"{no_ticket_stats.status_code}/{code_of(no_ticket_stats)}",
        )
        health = client.get("/api/v1/healthz")
        check("health exempt from gate", health.status_code == 200, str(health.status_code))
        options = client.options("/api/v1/stats")
        check("OPTIONS not denied by access gate", options.status_code != 403, str(options.status_code))

        status = client.get("/api/v1/access/status", headers=enter_headers(ticket or ""))
        check("access status active", status.status_code == 200 and status.json().get("state") == "active", status.text)
        check("access status does not return ticket", status.json().get("ticket") is None)

        # 无 ticket 的 status 应可访问 access 路由，但明确票据错误。
        status_no_ticket = client.get("/api/v1/access/status")
        check(
            "status route exempt but missing ticket -> 403",
            status_no_ticket.status_code == 403
            and code_of(status_no_ticket) == ERROR_ACCESS_TICKET_INVALID,
            f"{status_no_ticket.status_code}/{code_of(status_no_ticket)}",
        )

        # 所有已注册的 /api/v1 路由中，除 health/access 外都不得是 exempt。
        registered_paths = sorted(
            path
            for path in app.openapi().get("paths", {})
            if path.startswith("/api/v1")
        )
        business_registered = [
            path
            for path in registered_paths
            if path not in {"/api/v1/healthz", "/api/v1/readyz"}
            and not path.startswith("/api/v1/access")
        ]
        check(
            "all registered business routes are gated by default",
            bool(business_registered)
            and all(not is_access_exempt(path, "GET") for path in business_registered),
            str(registered_paths),
        )

        # 所有代表性业务路由默认受门禁保护；health/access 例外。
        business_paths = [
            ("GET", "/api/v1/models"),
            ("GET", "/api/v1/stats"),
            ("GET", "/api/v1/demo/state"),
            ("GET", "/api/v1/bench/pk"),
            ("GET", "/api/v1/arena/next"),
            ("GET", "/api/v1/feedback/stats"),
            ("POST", "/api/v1/classify"),
            ("POST", "/api/v1/classify/batch"),
            ("POST", "/api/v1/compare"),
            ("GET", "/api/v1/compare/stream"),
        ]
        for method, path in business_paths:
            response = client.request(method, path)
            OUTPUTS.append(response.text)
            check(
                f"business route gated: {method} {path}",
                response.status_code == 403,
                str(response.status_code),
            )
        check(
            "all representative business paths are not exempt",
            all(not is_access_exempt(path, method) for method, path in business_paths),
        )
        check("health path is exempt", is_access_exempt("/api/v1/healthz", "GET"))
        check("readyz path is exempt", is_access_exempt("/api/v1/readyz", "GET"))
        check("access path is exempt", is_access_exempt("/api/v1/access/status", "GET"))
        check("unknown new path is gated by default", not is_access_exempt("/api/v1/new-business", "GET"))

        # batch LLM 拒绝规则仍生效（gate 已通过）。
        batch = client.post(
            "/api/v1/classify/batch",
            headers=enter_headers(ticket or ""),
            json={"model": "llm", "items": [{"item_id": "r000001", "title": "测试标题"}]},
        )
        check(
            "batch LLM still BATCH_LLM_NOT_ALLOWED after access",
            batch.status_code == 400 and code_of(batch) == "BATCH_LLM_NOT_ALLOWED",
            f"{batch.status_code}/{code_of(batch)}",
        )

        left = client.post("/api/v1/access/leave", headers=enter_headers(ticket or ""))
        check("leave returns cancelled", left.status_code == 200 and left.json().get("state") == "cancelled", left.text)
        after_leave = client.get("/api/v1/stats", headers=enter_headers(ticket or ""))
        check(
            "old ticket after leave denied",
            after_leave.status_code == 403,
            str(after_leave.status_code),
        )


def test_ttl_and_queue_api(root: Path) -> None:
    print("\n=== 3. ready/queue/active TTL、队列上限与 cooling 映射 ===")
    configure_env(True, root / "ttl.db")
    app = create_app()
    with TestClient(app, client=(IP, 50000)) as client:
        install_fake_classifier(app)

        # ready 15s 未 claim -> 403。
        gate, clock = new_fake_gate()
        app.state.access_gate = gate
        ready = client.post("/api/v1/access/enter")
        ticket = ready.json().get("ticket") or ""
        clock.set(15.0)
        late_claim = client.post("/api/v1/access/claim", headers=enter_headers(ticket))
        check(
            "ready at 15s cannot claim",
            late_claim.status_code == 403
            and code_of(late_claim) == ERROR_ACCESS_TICKET_INVALID,
            f"{late_claim.status_code}/{code_of(late_claim)}",
        )

        # queued 120s 过期 -> status 403 invalid。
        gate, clock = new_fake_gate()
        app.state.access_gate = gate
        for index in range(10):
            key = f"10.0.0.{index + 1}"
            r = gate.enter(key)
            gate.claim(key, r.ticket or "")
        queued = client.post("/api/v1/access/enter")
        q_ticket = queued.json().get("ticket") or ""
        check("11th queued position 1", queued.json().get("position") == 1, queued.text)
        q_status = client.get("/api/v1/access/status", headers=enter_headers(q_ticket))
        check(
            "queued status exposes position/ahead without ticket",
            q_status.status_code == 200
            and q_status.json().get("position") == 1
            and q_status.json().get("ahead") == 0
            and q_status.json().get("ticket") is None,
            q_status.text,
        )
        clock.set(120.0)
        expired_status = client.get("/api/v1/access/status", headers=enter_headers(q_ticket))
        check(
            "queued ticket expires at 120s",
            expired_status.status_code == 403
            and code_of(expired_status) == ERROR_ACCESS_TICKET_INVALID,
            f"{expired_status.status_code}/{code_of(expired_status)}",
        )

        # 队列满：10 active + 50 queued，第 51 个新 IP 由 HTTP enter 返回 429。
        gate, clock = new_fake_gate()
        app.state.access_gate = gate
        for index in range(10):
            key = f"10.0.1.{index + 1}"
            r = gate.enter(key)
            gate.claim(key, r.ticket or "")
        for index in range(50):
            gate.enter(f"10.0.2.{index + 1}")
        full = client.post("/api/v1/access/enter")
        check(
            "51st queue request -> 429 ACCESS_QUEUE_FULL",
            full.status_code == 429 and code_of(full) == ERROR_ACCESS_QUEUE_FULL,
            f"{full.status_code}/{code_of(full)}",
        )
        check("queue_full response does not contain ticket", full.json().get("error", {}).get("details", {}).get("ticket") is None)
        check("queue_full response does not expose raw IP", IP not in full.text)

        # active 180s -> cooling；业务 API 429；access status 200 cooling；enter 仍 cooling。
        gate, clock = new_fake_gate()
        app.state.access_gate = gate
        ready = client.post("/api/v1/access/enter")
        ticket = ready.json().get("ticket") or ""
        client.post("/api/v1/access/claim", headers=enter_headers(ticket))
        clock.set(180.0)
        cooling_stats = client.get("/api/v1/stats", headers=enter_headers(ticket))
        check(
            "active at 180s -> gate 429 ACCESS_COOLING",
            cooling_stats.status_code == 429
            and code_of(cooling_stats) == ERROR_ACCESS_COOLING,
            f"{cooling_stats.status_code}/{code_of(cooling_stats)}",
        )
        cooling_status = client.get("/api/v1/access/status", headers=enter_headers(ticket))
        check(
            "access status reports cooling 200",
            cooling_status.status_code == 200 and cooling_status.json().get("state") == "cooling",
            cooling_status.text,
        )
        cooling_enter = client.post("/api/v1/access/enter")
        check(
            "enter during cooling returns cooling",
            cooling_enter.status_code == 200 and cooling_enter.json().get("state") == "cooling",
            cooling_enter.text,
        )
        clock.set(240.0)
        after_cooldown = client.post("/api/v1/access/enter")
        check("after 60s cooling enters ready", after_cooldown.json().get("state") == "ready", after_cooldown.text)

        # cancel queued：释放位置；旧 ticket 不可再 status/cancel。
        gate, clock = new_fake_gate()
        app.state.access_gate = gate
        for index in range(10):
            key = f"10.0.3.{index + 1}"
            r = gate.enter(key)
            gate.claim(key, r.ticket or "")
        queued = client.post("/api/v1/access/enter")
        q_ticket = queued.json().get("ticket") or ""
        cancelled = client.post("/api/v1/access/cancel", headers=enter_headers(q_ticket))
        check(
            "cancel API returns cancelled",
            cancelled.status_code == 200 and cancelled.json().get("state") == "cancelled",
            cancelled.text,
        )
        cancelled_status = client.get("/api/v1/access/status", headers=enter_headers(q_ticket))
        check(
            "cancelled ticket no longer valid",
            cancelled_status.status_code == 403,
            str(cancelled_status.status_code),
        )
        replacement = client.post("/api/v1/access/enter")
        check(
            "cancel frees queue position for same IP",
            replacement.status_code == 200
            and replacement.json().get("state") == "queued"
            and replacement.json().get("position") == 1,
            replacement.text,
        )

        # access status 路由的轮询限速（默认 1s；测试注入 1s）。
        gate, clock = new_fake_gate(status_min_interval_seconds=1.0)
        app.state.access_gate = gate
        ready = client.post("/api/v1/access/enter")
        ticket = ready.json().get("ticket") or ""
        client.post("/api/v1/access/claim", headers=enter_headers(ticket))
        first = client.get("/api/v1/access/status", headers=enter_headers(ticket))
        second = client.get("/api/v1/access/status", headers=enter_headers(ticket))
        check(
            "access status HTTP poll throttle",
            first.status_code == 200
            and second.status_code == 429
            and code_of(second) == "ACCESS_POLL_THROTTLED",
            f"{first.status_code}/{second.status_code}/{code_of(second)}",
        )
        clock.advance(1.0)
        third = client.get("/api/v1/access/status", headers=enter_headers(ticket))
        check("access status allowed after poll interval", third.status_code == 200, str(third.status_code))


def test_identity_ticket_and_sse(root: Path) -> None:
    print("\n=== 4. IP/XFF、ticket 绑定、manager 故障、SSE 到期 ===")
    configure_env(True, root / "identity.db")
    app = create_app()
    with TestClient(app, client=(IP, 50000)) as client:
        fake = install_fake_classifier(app)
        gate, clock = new_fake_gate()
        app.state.access_gate = gate

        enter = client.post(
            "/api/v1/access/enter",
            headers={"X-Forwarded-For": OTHER_IP},
        )
        ticket = enter.json().get("ticket") or ""
        client.post("/api/v1/access/claim", headers=enter_headers(ticket))
        check(
            "forged XFF ignored; real peer is active",
            gate.status(IP, ticket).state == "active",
        )
        check(
            "forged XFF does not create another active identity",
            gate.status(OTHER_IP, ticket).state == "expired",
        )

        # 其他 IP 伪造 ticket 不能访问当前业务 API/状态。
        other = client.get(
            "/api/v1/stats",
            headers={ACCESS_TICKET_HEADER: "forged-ticket"},
        )
        check(
            "forged ticket denied",
            other.status_code == 403 and code_of(other) == ERROR_ACCESS_TICKET_INVALID,
            f"{other.status_code}/{code_of(other)}",
        )

        # manager 故障 -> 503，业务 handler 不执行。
        app.state.access_gate = None
        unavailable = client.get("/api/v1/stats")
        check(
            "missing manager -> 503 ACCESS_UNAVAILABLE",
            unavailable.status_code == 503 and code_of(unavailable) == "ACCESS_UNAVAILABLE",
            f"{unavailable.status_code}/{code_of(unavailable)}",
        )

        # SSE：active 剩余 0.5s，fake classifier 1s 后才产出；应在 headers 后
        # 以 ACCESS_EXPIRED 流事件结束，而不是通用网络错误。
        gate, clock = new_fake_gate()
        app.state.access_gate = gate
        ready = client.post("/api/v1/access/enter")
        ticket = ready.json().get("ticket") or ""
        client.post("/api/v1/access/claim", headers=enter_headers(ticket))
        clock.set(179.5)
        before_calls = fake.stream_calls
        stream = client.get(
            "/api/v1/compare/stream",
            params={"title": "测试标题", "models": "rf"},
            headers=enter_headers(ticket),
        )
        OUTPUTS.append(stream.text)
        check(
            "SSE active with remaining lease starts as event-stream",
            stream.status_code == 200
            and stream.headers.get("content-type", "").startswith("text/event-stream"),
            f"{stream.status_code}/{stream.headers.get('content-type')}",
        )
        check(
            "SSE closes at access deadline with ACCESS_EXPIRED",
            "ACCESS_EXPIRED" in stream.text,
            stream.text[:300],
        )
        check("SSE did not fall back to another model call", fake.stream_calls == before_calls + 1)

        # queued/no-active SSE 在 headers 前被 JSON 拒绝，不会启动 EventSource。
        gate, clock = new_fake_gate()
        app.state.access_gate = gate
        no_ticket_stream = client.get(
            "/api/v1/compare/stream",
            params={"title": "测试标题", "models": "rf"},
        )
        check(
            "SSE without ticket rejected before StreamingResponse",
            no_ticket_stream.status_code == 403
            and not no_ticket_stream.headers.get("content-type", "").startswith("text/event-stream"),
            f"{no_ticket_stream.status_code}/{no_ticket_stream.headers.get('content-type')}",
        )

        # 冷却时 POST fallback 同样被 gate 拒绝。
        gate, clock = new_fake_gate()
        app.state.access_gate = gate
        ready = client.post("/api/v1/access/enter")
        ticket = ready.json().get("ticket") or ""
        client.post("/api/v1/access/claim", headers=enter_headers(ticket))
        clock.set(180.0)
        fallback = client.post(
            "/api/v1/compare",
            headers=enter_headers(ticket),
            json={"title": "测试标题", "models": ["rf"]},
        )
        check(
            "POST fallback at expired lease denied by same gate",
            fallback.status_code == 429 and code_of(fallback) == ERROR_ACCESS_COOLING,
            f"{fallback.status_code}/{code_of(fallback)}",
        )


def test_cors_preflight(root: Path) -> None:
    print("\n=== 5. 跨源 CORS preflight：X-Access-Ticket ===")
    configure_env(True, root / "cors.db")
    origin = "http://127.0.0.1:5173"
    os.environ["TMF_CORS_ORIGINS"] = origin
    app = create_app()
    with TestClient(app, client=(IP, 50000)) as client:
        for method, path in (
            ("GET", "/api/v1/stats"),
            ("POST", "/api/v1/classify"),
        ):
            response = client.options(
                path,
                headers={
                    "Origin": origin,
                    "Access-Control-Request-Method": method,
                    "Access-Control-Request-Headers": (
                        "x-access-ticket,content-type,x-request-id"
                    ),
                },
            )
            allow_origin = response.headers.get("access-control-allow-origin", "")
            allow_methods = {
                item.strip().upper()
                for item in response.headers.get(
                    "access-control-allow-methods", ""
                ).split(",")
                if item.strip()
            }
            allow_headers = {
                item.strip().lower()
                for item in response.headers.get(
                    "access-control-allow-headers", ""
                ).split(",")
                if item.strip()
            }
            check(
                f"CORS preflight {method} allowed",
                response.status_code == 200,
                str(response.status_code),
            )
            check(
                f"CORS preflight {method} allows origin",
                allow_origin == origin,
                allow_origin,
            )
            check(
                f"CORS preflight {method} allows requested method",
                method in allow_methods,
                str(allow_methods),
            )
            check(
                f"CORS preflight {method} allows ticket/content/request-id headers",
                {"x-access-ticket", "content-type", "x-request-id"} <= allow_headers,
                str(allow_headers),
            )
            print(
                f"   preflight {method}: allow-origin={allow_origin} "
                f"allow-methods={sorted(allow_methods)} "
                f"allow-headers={sorted(allow_headers)} "
                f"max-age={response.headers.get('access-control-max-age')}"
            )


def test_config_and_poll_throttle(root: Path) -> None:
    print("\n=== 6. 配置、状态轮询限速 ===")
    try:
        load_access_settings({"TMF_ACCESS_CONTROL_ENABLED": "true"})
        check("enabled without HMAC key fails", False)
    except AccessConfigError:
        check("enabled without HMAC key fails", True)

    # 启用但无 key：网关启动 fail-closed，不能自动降级为关闭。
    configure_env(False, root / "startup-missing-key.db")
    os.environ["TMF_ACCESS_CONTROL_ENABLED"] = "true"
    os.environ.pop("TMF_LLM_QUOTA_HMAC_KEY", None)
    app = create_app()
    try:
        with TestClient(app):
            pass
        check("enabled missing-key app startup fails", False)
    except Exception:
        check("enabled missing-key app startup fails", True)
    finally:
        clear_env()

    try:
        load_access_settings(
            {
                "TMF_ACCESS_CONTROL_ENABLED": "not-a-bool",
            }
        )
        check("invalid access flag rejected", False)
    except AccessConfigError:
        check("invalid access flag rejected", True)

    clock = FakeClock()
    gate = AccessGate(
        AccessManager(clock=clock, ticket_factory=Tickets()),
        TEST_KEY.encode("utf-8"),
        status_min_interval_seconds=1.0,
        clock=clock,
    )
    ready = gate.enter("10.255.0.1")
    gate.status("10.255.0.1", ready.ticket or "")
    try:
        gate.status("10.255.0.1", ready.ticket or "")
        check("status poll throttle enforced", False)
    except AccessPollThrottled:
        check("status poll throttle enforced", True)
    clock.advance(1.0)
    check("status poll allowed after interval", gate.status("10.255.0.1", ready.ticket or "").state == "ready")


def test_privacy(root: Path) -> None:
    print("\n=== 7. 输出/日志隐私 ===")
    joined = "\n".join(OUTPUTS)
    log_text = "\n".join(LOG_ENTRIES)
    check("responses do not contain raw client IP", IP not in joined)
    check("responses do not contain test ticket", "access-test-ticket" not in joined)
    check("responses do not contain HMAC key", TEST_KEY not in joined)
    check("logs do not contain raw client IP", IP not in log_text and OTHER_IP not in log_text)
    check("logs do not contain ticket", "access-test-ticket" not in log_text)
    check("logs do not contain HMAC key", TEST_KEY not in log_text)
    check("logs do not contain title", "测试标题" not in log_text)
    check("logs do not contain traceback", "Traceback" not in log_text)


def main() -> int:
    logger = logging.getLogger("tmf.gateway")
    capture = CaptureGatewayLogs()
    logger.addHandler(capture)
    try:
        with tempfile.TemporaryDirectory(prefix="tmf-access-gateway-") as raw:
            root = Path(raw)
            test_disabled_compat(root)
            test_enabled_flow_and_route_coverage(root)
            test_ttl_and_queue_api(root)
            test_identity_ticket_and_sse(root)
            test_cors_preflight(root)
            test_config_and_poll_throttle(root)
    finally:
        logger.removeHandler(capture)
        clear_env()
    test_privacy(Path("."))

    if FAILURES:
        print(f"\nRESULT FAIL ({len(FAILURES)} failed / {TOTAL} checks)")
        for name in FAILURES:
            print(f"  FAILED: {name}")
        return 1
    print(f"\nRESULT PASS ({TOTAL} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
