#!/usr/bin/env python
"""B6 验收脚本：``GET /api/v1/compare/stream``（SSE 渐进返回 + 总截止）。

运行环境（网关依赖，无需真实 worker / 不调用 LLM provider）：
    $env:PYTHONPATH='E:\\AIModels\\test\\TouManFen\\venv-gateway\\Lib\\site-packages'
    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/verify_compare_stream.py

覆盖：
1. 配置：``TMF_COMPARE_TOTAL_TIMEOUT_MS`` 默认 18000 / 环境变量覆盖 / 非法值拒绝；
   心跳默认 15s；并发上限默认 3（B12 起可配，完整验收见 verify_concurrency.py）。
2. SSE 契约：响应头、事件名/数量、按完成顺序、JSON 字段形状、summary/done 顺序、
   request_id 与 X-Request-ID 一致（TestClient + fake adapter + 临时 TMF_DB_PATH）。
3. 逐模型失败隔离：单模型 error → 其他模型仍成功、summary=partial。
4. 总截止：200ms + 极慢 adapter → 已完成 model 事件保留、summary/done=partial，
   绝不误报 ok；未完成网关侧 task 被取消。
5. 心跳：缩短内部心跳常量验证 ``: heartbeat`` 注释行，且不延长总截止。
6. 非法 title/models：HTTP 200 + ``event: error``，不发 model/summary/done。
7. 服务层生成器被关闭（模拟断连）→ 未完成 task 取消、无 summary 误报。
8. 真实 127.0.0.1 临时端口 uvicorn：流式增量到达 + 客户端断连后取消网关侧 task、
   无 unhandled task 异常、端口释放（不触碰用户 stage2 服务/端口）。
9. 旧 ``POST /api/v1/compare`` 契约不变。

不写默认 DB（临时 TMF_DB_PATH）；不发起真实 worker / LLM 请求。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import socket
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from serving.adapters import AdapterResult  # noqa: E402
from serving.gateway.errors import ConfigError  # noqa: E402
from serving.gateway.settings import (  # noqa: E402
    DEFAULT_COMPARE_CONCURRENCY,
    DEFAULT_COMPARE_TOTAL_TIMEOUT_MS,
    load_settings,
)
from serving.gateway.stats import StatsCollector  # noqa: E402
from serving.models.registry import ModelRegistry  # noqa: E402
from serving.services.classification import (  # noqa: E402
    COMPARE_STREAM_HEARTBEAT_SECONDS,
    ClassificationService,
    StreamModelEvent,
    StreamSummaryEvent,
)

FAILURES: list[str] = []
DEMO_TITLE = "北京二手房成交量创年内新高"
# 运行时临时 key；仅用于启动 quota infra，不写仓库、不输出。
TEST_HMAC_KEY = secrets.token_hex(32)

ENV_KEYS = (
    "TMF_COMPARE_TOTAL_TIMEOUT_MS",
    "TMF_COMPARE_CONCURRENCY",
    "TMF_DB_PATH",
    "TMF_LLM_QUOTA_HMAC_KEY",
    "TMF_MAX_TITLE_CHARS",
    "TMF_WORKER_HOST",
    "TMF_WORKER_URL_RF",
    "TMF_WORKER_URL_LLM",
    "TMF_CORS_ORIGINS",
    "TMF_GATEWAY_HOST",
    "TMF_GATEWAY_PORT",
    "TMF_FRONTEND_HOST",
    "TMF_FRONTEND_PORT",
)

MODEL_KEYS = {
    "request_id",
    "model_id",
    "status",
    "latency_ms",
    "model_version",
    "pred_class",
    # B11：SSE model 事件新增 data_source（向后兼容的增量字段）。
    "data_source",
    "error",
}
SUMMARY_KEYS = {
    "request_id",
    "status",
    "total",
    "ok",
    "failed",
    "elapsed_ms",
    "total_timeout_ms",
    "partial",
}
DONE_KEYS = {"request_id", "status", "total", "ok", "failed"}
ERROR_KEYS = {"request_id", "code", "message"}


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


def clear_env() -> None:
    for key in ENV_KEYS:
        os.environ.pop(key, None)


# ---------------------------------------------------------------------------
# fake adapter / 结果构造
# ---------------------------------------------------------------------------
class FakeAdapter:
    """仅实现 ModelAdapter 协议的测试适配器，记录调用与取消。"""

    def __init__(
        self,
        model_id: str,
        *,
        delay: float = 0.0,
        result: AdapterResult | None = None,
    ) -> None:
        self.model_id = model_id
        self.model_version: str | None = None
        self.delay = delay
        self.result = result if result is not None else ok_result(model_id)
        self.calls = 0
        self.cancelled = False
        self.cancel_count = 0
        self.tasks: set[asyncio.Task] = set()

    async def predict(self, title: str, request_id: str) -> AdapterResult:
        self.calls += 1
        # 断言用：不记录标题内容，只记录任务本身。
        task = asyncio.current_task()
        if task is not None:
            self.tasks.add(task)
        try:
            if self.delay:
                await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            self.cancel_count += 1
            raise
        return self.result


def ok_result(
    model_id: str,
    pred: str = "sports",
    latency: int = 12,
    data_source: str | None = None,
) -> AdapterResult:
    return AdapterResult(
        model_id=model_id,
        status="ok",
        latency_ms=latency,
        pred_class=pred,
        data_source=data_source,
    )


def error_result(
    model_id: str,
    code: str = "WORKER_TIMEOUT",
    message: str = "所选模型未在配置的时间内返回",
    data_source: str | None = None,
) -> AdapterResult:
    return AdapterResult(
        model_id=model_id,
        status="error",
        latency_ms=3,
        error_code=code,
        error_message=message,
        data_source=data_source,
    )


def make_service(adapters: dict[str, Any], **overrides: Any) -> ClassificationService:
    settings = load_settings()
    kwargs: dict[str, Any] = {
        "registry": ModelRegistry(settings),
        "adapters": adapters,
        "classes": settings.classes,
        "max_title_chars": settings.max_title_chars,
        "stats": StatsCollector(),
        "logger": logging.getLogger("verify-compare-stream"),
    }
    kwargs.update(overrides)
    return ClassificationService(**kwargs)


# ---------------------------------------------------------------------------
# SSE 解析
# ---------------------------------------------------------------------------
def parse_sse(body: str) -> dict[str, Any]:
    """把 SSE 文本解析为事件列表 / 心跳注释行 / 异常行。"""
    events: list[dict[str, Any]] = []
    heartbeats: list[str] = []
    malformed: list[str] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        event_name: str | None = None
        data_lines: list[str] = []
        comment_block = False
        for line in block.split("\n"):
            if not line:
                continue
            if line.startswith(":"):
                comment_block = True
                heartbeats.append(line)
                continue
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                data_lines.append(line[len("data: ") :])
            else:
                malformed.append(line)
        if comment_block and event_name is None:
            continue
        if event_name is None:
            malformed.append(block)
            continue
        payload: dict[str, Any] | None = None
        if len(data_lines) == 1:
            try:
                payload = json.loads(data_lines[0])
            except json.JSONDecodeError:
                malformed.append(block)
        else:
            malformed.append(block)
        events.append(
            {"event": event_name, "data": payload, "data_lines": len(data_lines)}
        )
    return {"events": events, "heartbeats": heartbeats, "malformed": malformed}


def names(parsed: dict[str, Any]) -> list[str]:
    return [event["event"] for event in parsed["events"]]


def payloads(parsed: dict[str, Any], event_name: str) -> list[dict[str, Any]]:
    return [
        event["data"]
        for event in parsed["events"]
        if event["event"] == event_name
    ]


# ---------------------------------------------------------------------------
# 1. 配置
# ---------------------------------------------------------------------------
def config_checks() -> None:
    print("\n=== 1. 总截止/心跳默认值与环境变量 ===")
    clear_env()
    settings = load_settings()
    check(
        "default TMF_COMPARE_TOTAL_TIMEOUT_MS == 18000",
        settings.compare_total_timeout_ms == 18000
        and DEFAULT_COMPARE_TOTAL_TIMEOUT_MS == 18000,
        str(settings.compare_total_timeout_ms),
    )
    check(
        "heartbeat default is 15s",
        COMPARE_STREAM_HEARTBEAT_SECONDS == 15.0,
        str(COMPARE_STREAM_HEARTBEAT_SECONDS),
    )
    check(
        "compare concurrency default is 3 (B12: TMF_COMPARE_CONCURRENCY)",
        DEFAULT_COMPARE_CONCURRENCY == 3 and settings.compare_concurrency == 3,
        f"{DEFAULT_COMPARE_CONCURRENCY} {settings.compare_concurrency}",
    )

    os.environ["TMF_COMPARE_TOTAL_TIMEOUT_MS"] = "250"
    try:
        overridden = load_settings()
        check(
            "env TMF_COMPARE_TOTAL_TIMEOUT_MS overrides",
            overridden.compare_total_timeout_ms == 250,
            str(overridden.compare_total_timeout_ms),
        )
    finally:
        clear_env()

    for value in ("0", "-5", "abc", "999999"):
        os.environ["TMF_COMPARE_TOTAL_TIMEOUT_MS"] = value
        try:
            load_settings()
            check(f"invalid TMF_COMPARE_TOTAL_TIMEOUT_MS={value} rejected", False)
        except ConfigError:
            check(f"invalid TMF_COMPARE_TOTAL_TIMEOUT_MS={value} rejected", True)
        except Exception as exc:  # noqa: BLE001
            check(
                f"invalid TMF_COMPARE_TOTAL_TIMEOUT_MS={value} rejected",
                False,
                type(exc).__name__,
            )
        finally:
            clear_env()


# ---------------------------------------------------------------------------
# 2-6 / 9：TestClient 下的 SSE 契约（临时 TMF_DB_PATH，不写默认 DB）
# ---------------------------------------------------------------------------
def _install_classifier(
    app, adapters: dict[str, FakeAdapter], **overrides: Any
) -> ClassificationService:
    settings = app.state.settings
    service = ClassificationService(
        registry=app.state.registry,
        adapters=adapters,
        classes=settings.classes,
        max_title_chars=settings.max_title_chars,
        stats=app.state.stats,
        logger=logging.getLogger("verify-compare-stream-api"),
        **overrides,
    )
    app.state.classifier = service
    return service


def _sse_get(client: TestClient, params: dict[str, Any]):
    return client.get("/api/v1/compare/stream", params=params)


def _check_sse_headers(response) -> None:
    headers = {key.lower(): value for key, value in response.headers.items()}
    check(
        "SSE content-type is text/event-stream; charset=utf-8",
        headers.get("content-type", "").startswith("text/event-stream")
        and "charset=utf-8" in headers.get("content-type", ""),
        str(headers.get("content-type")),
    )
    check(
        "SSE Cache-Control: no-cache",
        headers.get("cache-control") == "no-cache",
        str(headers.get("cache-control")),
    )
    check(
        "SSE X-Accel-Buffering: no",
        headers.get("x-accel-buffering") == "no",
        str(headers.get("x-accel-buffering")),
    )
    check(
        "SSE Connection: keep-alive",
        headers.get("connection", "").lower() == "keep-alive",
        str(headers.get("connection")),
    )


def _check_model_event(payload: dict[str, Any], *, expected_id: str) -> None:
    check(
        f"model event field shape ({expected_id})",
        set(payload) == MODEL_KEYS
        and payload.get("model_id") == expected_id
        and payload.get("status") in {"ok", "error"}
        and isinstance(payload.get("latency_ms"), int)
        and payload.get("latency_ms") >= 0,
        str(payload)[:300],
    )


def order_and_contract_checks(client: TestClient, app) -> str:
    print("\n=== 2. SSE 响应头 / 完成顺序 / 事件数量与字段 ===")
    settings = app.state.settings
    adapters = {
        "rf": FakeAdapter(
            "rf", delay=0.15, result=ok_result("rf", data_source="live")
        ),
        "llm": FakeAdapter(
            "llm", delay=0.10, result=ok_result("llm", data_source="live")
        ),
        "bert": FakeAdapter(
            "bert", delay=0.01, result=ok_result("bert", data_source="live")
        ),
    }
    _install_classifier(app, adapters)
    response = _sse_get(
        client, {"title": DEMO_TITLE, "models": "rf,llm,bert"}
    )
    parsed = parse_sse(response.text)
    check("SSE request status 200", response.status_code == 200, str(response.status_code))
    _check_sse_headers(response)
    check(
        "SSE has no malformed frames / no stray comments",
        not parsed["malformed"] and not parsed["heartbeats"],
        f"{parsed['malformed'][:2]} {parsed['heartbeats'][:2]}",
    )
    check(
        "event name sequence is 3x model + summary + done",
        names(parsed) == ["model", "model", "model", "summary", "done"],
        str(names(parsed)),
    )
    check(
        "each frame has exactly one data line",
        all(event["data_lines"] == 1 for event in parsed["events"]),
        str([event["data_lines"] for event in parsed["events"]]),
    )
    model_events = payloads(parsed, "model")
    check(
        "model events arrive in completion order (fast bert first, slow rf last)",
        [payload["model_id"] for payload in model_events] == ["bert", "llm", "rf"],
        str([payload["model_id"] for payload in model_events]),
    )
    check(
        "each model appears at most once",
        len({payload["model_id"] for payload in model_events}) == 3,
        str([payload["model_id"] for payload in model_events]),
    )
    for payload in model_events:
        _check_model_event(payload, expected_id=payload["model_id"])
    check(
        "ok model event has error=null, pred_class and data_source passthrough",
        all(
            payload["status"] == "ok"
            and payload["error"] is None
            and payload["pred_class"] == "sports"
            and payload["model_version"] is None
            and payload["data_source"] == "live"
            for payload in model_events
        ),
        str(model_events)[:300],
    )

    summary = payloads(parsed, "summary")
    done = payloads(parsed, "done")
    check("exactly one summary and one done", len(summary) == 1 and len(done) == 1)
    summary_payload = summary[0] if summary else {}
    done_payload = done[0] if done else {}
    check(
        "summary field shape / values (ok, total=3)",
        set(summary_payload) == SUMMARY_KEYS
        and summary_payload.get("status") == "ok"
        and summary_payload.get("total") == 3
        and summary_payload.get("ok") == 3
        and summary_payload.get("failed") == 0
        and summary_payload.get("partial") is False
        and summary_payload.get("total_timeout_ms") == 18000
        and isinstance(summary_payload.get("elapsed_ms"), int),
        str(summary_payload)[:300],
    )
    check(
        "done field shape / values / follows summary",
        set(done_payload) == DONE_KEYS
        and done_payload.get("status") == "ok"
        and done_payload.get("total") == 3
        and done_payload.get("ok") == 3
        and done_payload.get("failed") == 0
        and names(parsed)[-1] == "done"
        and names(parsed)[-2] == "summary",
        str(done_payload)[:300],
    )
    request_ids = {
        event["data"].get("request_id") for event in parsed["events"]
    }
    check(
        "request_id consistent across events and X-Request-ID",
        len(request_ids) == 1
        and response.headers.get("X-Request-ID") == next(iter(request_ids)),
        f"{request_ids} {response.headers.get('X-Request-ID')}",
    )

    # models 省略 -> 沿用 compare 默认集合（本仓库配置为全部启用模型）。
    default_adapters = {
        model_id: FakeAdapter(model_id, delay=0.01)
        for model_id in app.state.registry.compare_default_model_ids()
    }
    _install_classifier(app, default_adapters)
    response = _sse_get(client, {"title": DEMO_TITLE})
    parsed = parse_sse(response.text)
    default_ids = set(app.state.registry.compare_default_model_ids())
    check(
        "models omitted uses compare default set",
        response.status_code == 200
        and {payload["model_id"] for payload in payloads(parsed, "model")}
        == default_ids
        and payloads(parsed, "summary")[0]["total"] == len(default_ids)
        and payloads(parsed, "summary")[0]["status"] == "ok",
        f"{names(parsed)} {[p['model_id'] for p in payloads(parsed, 'model')]}",
    )

    # 空白字符应被容忍（EventSource 前端可能带空格）。
    response = _sse_get(client, {"title": DEMO_TITLE, "models": " rf , bert "})
    parsed = parse_sse(response.text)
    check(
        "models query tolerates surrounding whitespace",
        response.status_code == 200
        and {p["model_id"] for p in payloads(parsed, "model")} == {"rf", "bert"}
        and len(payloads(parsed, "model")) == 2
        and payloads(parsed, "summary")[0]["status"] == "ok",
        str(names(parsed)),
    )
    return DEMO_TITLE


def partial_checks(client: TestClient, app) -> None:
    print("\n=== 3. 单模型 error 隔离 -> summary partial ===")
    adapters = {
        "rf": FakeAdapter("rf", delay=0.02),
        "llm": FakeAdapter("llm", delay=0.03),
        "bert": FakeAdapter("bert", delay=0.01, result=error_result("bert")),
    }
    _install_classifier(app, adapters)
    response = _sse_get(
        client, {"title": DEMO_TITLE, "models": "rf,bert,llm"}
    )
    parsed = parse_sse(response.text)
    model_events = {payload["model_id"]: payload for payload in payloads(parsed, "model")}
    summary = payloads(parsed, "summary")[0]
    done = payloads(parsed, "done")[0]
    check(
        "single model error isolated (rf/llm still ok)",
        response.status_code == 200
        and model_events.get("rf", {}).get("status") == "ok"
        and model_events.get("llm", {}).get("status") == "ok",
        str(names(parsed)),
    )
    bert = model_events.get("bert", {})
    check(
        "error model event shape (code/message, null version/pred)",
        bert.get("status") == "error"
        and bert.get("model_version") is None
        and bert.get("pred_class") is None
        and isinstance(bert.get("error"), dict)
        and set(bert.get("error") or {}) == {"code", "message"}
        and bert["error"].get("code") == "WORKER_TIMEOUT"
        and bool(bert["error"].get("message")),
        str(bert)[:300],
    )
    check(
        "summary=partial (total 3 / ok 2 / failed 1) with no stream-level error",
        summary.get("status") == "partial"
        and summary.get("total") == 3
        and summary.get("ok") == 2
        and summary.get("failed") == 1
        and summary.get("partial") is True
        and names(parsed).count("error") == 0,
        f"{summary} {names(parsed)}",
    )
    check(
        "done mirrors partial summary",
        done.get("status") == "partial"
        and done.get("total") == 3
        and done.get("ok") == 2
        and done.get("failed") == 1,
        str(done),
    )


def deadline_checks(client: TestClient, app) -> None:
    print("\n=== 4. 总截止 200ms + 极慢 adapter（不得误报 ok） ===")
    fast = FakeAdapter("rf", delay=0.01)
    slow_llm = FakeAdapter("llm", delay=5.0)
    slow_bert = FakeAdapter("bert", delay=5.0)
    _install_classifier(
        app,
        {"rf": fast, "llm": slow_llm, "bert": slow_bert},
        compare_total_timeout_ms=200,
    )
    started = time.perf_counter()
    response = _sse_get(
        client, {"title": DEMO_TITLE, "models": "rf,llm,bert"}
    )
    duration = time.perf_counter() - started
    parsed = parse_sse(response.text)
    elapsed_ms = payloads(parsed, "summary")[0]["elapsed_ms"]
    summary = payloads(parsed, "summary")[0]
    done = payloads(parsed, "done")[0]
    model_ids = [payload["model_id"] for payload in payloads(parsed, "model")]
    check(
        "deadline returns quickly (200ms total, not waiting for 5s worker)",
        duration < 3.0,
        f"{duration:.2f}s",
    )
    check(
        "completed model event kept (rf only)",
        model_ids == ["rf"]
        and payloads(parsed, "model")[0]["status"] == "ok",
        str(model_ids),
    )
    check(
        "summary/done=partial, no ok misreport, timeout metadata",
        summary.get("status") == "partial"
        and summary.get("total") == 3
        and summary.get("ok") == 1
        and summary.get("failed") == 2
        and summary.get("partial") is True
        and summary.get("total_timeout_ms") == 200
        and done.get("status") == "partial"
        and done.get("failed") == 2,
        f"{summary} {done}",
    )
    check(
        "deadline hit near configured 200ms",
        150 <= elapsed_ms <= 3000,
        str(elapsed_ms),
    )
    check(
        "unfinished gateway tasks cancelled at deadline",
        slow_llm.cancelled and slow_bert.cancelled,
        f"llm={slow_llm.cancelled} bert={slow_bert.cancelled}",
    )
    check(
        "cancelled model tasks are finished (no task leak)",
        all(task.done() for task in slow_llm.tasks)
        and all(task.done() for task in slow_bert.tasks),
        f"{[t.done() for t in slow_llm.tasks]} {[t.done() for t in slow_bert.tasks]}",
    )


def heartbeat_checks(client: TestClient, app) -> None:
    print("\n=== 5. 心跳注释行（缩短内部常量）且不延长总截止 ===")
    slow_rf = FakeAdapter("rf", delay=5.0)
    slow_llm = FakeAdapter("llm", delay=5.0)
    _install_classifier(
        app,
        {"rf": slow_rf, "llm": slow_llm},
        compare_total_timeout_ms=400,
        compare_heartbeat_seconds=0.05,
    )
    started = time.perf_counter()
    response = _sse_get(client, {"title": DEMO_TITLE, "models": "rf,llm"})
    duration = time.perf_counter() - started
    parsed = parse_sse(response.text)
    summary = payloads(parsed, "summary")[0]
    check(
        "heartbeat lines are SSE comments, not model events",
        len(parsed["heartbeats"]) >= 4
        and all(line == ": heartbeat" for line in parsed["heartbeats"])
        and "heartbeat" not in names(parsed)
        and "event: heartbeat" not in response.text,
        f"count={len(parsed['heartbeats'])} names={names(parsed)}",
    )
    check(
        "heartbeat does not extend total deadline",
        duration < 3.0
        and summary.get("elapsed_ms", 0) < 3000
        and payloads(parsed, "model") == [],
        f"{duration:.2f}s elapsed_ms={summary.get('elapsed_ms')}",
    )
    check(
        "deadline with zero success -> all_failed (not partial/ok)",
        names(parsed) == ["summary", "done"]
        and summary.get("status") == "all_failed"
        and summary.get("ok") == 0
        and summary.get("failed") == 2
        and summary.get("partial") is False
        and summary.get("total_timeout_ms") == 400,
        str(summary),
    )
    check(
        "heartbeat case cancelled unfinished tasks",
        slow_rf.cancelled and slow_llm.cancelled,
        f"rf={slow_rf.cancelled} llm={slow_llm.cancelled}",
    )


def invalid_param_checks(client: TestClient, app) -> None:
    print("\n=== 6. 非法 title/models -> SSE error 事件 ===")
    adapters = {
        model_id: FakeAdapter(model_id, delay=0.01)
        for model_id in app.state.registry.all_model_ids()
    }
    _install_classifier(app, adapters)
    max_chars = app.state.settings.max_title_chars
    cases: list[tuple[str, dict[str, Any], str]] = [
        ("missing title", {"models": "rf"}, "INVALID_TITLE"),
        ("empty title", {"title": "   ", "models": "rf"}, "INVALID_TITLE"),
        (
            "title too long",
            {"title": "x" * (max_chars + 1), "models": "rf"},
            "TITLE_TOO_LONG",
        ),
        ("empty models", {"title": DEMO_TITLE, "models": ""}, "INVALID_MODELS"),
        ("duplicate models", {"title": DEMO_TITLE, "models": "rf,rf"}, "INVALID_MODELS"),
        (
            "malformed models list",
            {"title": DEMO_TITLE, "models": "rf,,bert"},
            "INVALID_MODELS",
        ),
        ("unknown model", {"title": DEMO_TITLE, "models": "nope"}, "UNKNOWN_MODEL"),
    ]
    for label, params, expected_code in cases:
        response = _sse_get(client, params)
        parsed = parse_sse(response.text)
        payload = payloads(parsed, "error")
        check(
            f"invalid param returns SSE error event ({label})",
            response.status_code == 200
            and response.headers.get("content-type", "").startswith(
                "text/event-stream"
            )
            and names(parsed) == ["error"]
            and len(payload) == 1
            and set(payload[0]) == ERROR_KEYS
            and payload[0].get("code") == expected_code
            and isinstance(payload[0].get("message"), str)
            and bool(payload[0].get("message"))
            and not parsed["malformed"],
            f"{response.status_code} {response.text[:300]}",
        )
    check(
        "stream-level error does not call fake adapters",
        all(adapter.calls == 0 for adapter in adapters.values()),
        str({model_id: adapter.calls for model_id, adapter in adapters.items()}),
    )


def legacy_compare_checks(client: TestClient, app) -> None:
    print("\n=== 9. 旧 POST /api/v1/compare 行为不变 ===")
    adapters = {
        "rf": FakeAdapter("rf", result=ok_result("rf")),
        "bert": FakeAdapter("bert", result=error_result("bert")),
    }
    _install_classifier(app, adapters)
    response = client.post(
        "/api/v1/compare", json={"title": DEMO_TITLE, "models": ["rf", "bert"]}
    )
    body = response.json()
    check(
        "POST /compare partial contract unchanged",
        response.status_code == 200
        and body.get("summary") == {"total": 2, "ok": 1, "failed": 1, "status": "partial"}
        and body.get("results", [{}])[0].get("status") == "ok"
        and body.get("results", [{}, {}])[1].get("status") == "error"
        and "pred_class" not in body["results"][1]
        and body["results"][1]["error"]["code"] == "WORKER_TIMEOUT",
        str(body)[:300],
    )
    check(
        "POST /compare still returns application/json",
        response.headers.get("content-type", "").startswith("application/json"),
        str(response.headers.get("content-type")),
    )


def api_checks() -> None:
    print("\n=== TestClient：SSE 契约（临时 TMF_DB_PATH，不写默认 DB） ===")
    clear_env()
    from serving.gateway.main import create_app

    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["TMF_DB_PATH"] = str(Path(temp_dir) / "verify_b6.db")
        # quota infra 需要显式临时 key；本测试不调用 LLM。
        os.environ["TMF_LLM_QUOTA_HMAC_KEY"] = TEST_HMAC_KEY
        # 环境变量 -> Settings -> create_app -> ClassificationService 的装配链。
        os.environ["TMF_COMPARE_TOTAL_TIMEOUT_MS"] = "4321"
        try:
            app = create_app()
            with TestClient(app) as client:
                check(
                    "TMF_COMPARE_TOTAL_TIMEOUT_MS wired into app classifier",
                    app.state.settings.compare_total_timeout_ms == 4321
                    and app.state.classifier.compare_total_timeout_ms == 4321,
                    f"{getattr(app.state.settings, 'compare_total_timeout_ms', None)} "
                    f"{getattr(app.state.classifier, 'compare_total_timeout_ms', None)}",
                )
                order_and_contract_checks(client, app)
                partial_checks(client, app)
                deadline_checks(client, app)
                heartbeat_checks(client, app)
                invalid_param_checks(client, app)
                legacy_compare_checks(client, app)
        finally:
            os.environ.pop("TMF_DB_PATH", None)
            os.environ.pop("TMF_COMPARE_TOTAL_TIMEOUT_MS", None)


# ---------------------------------------------------------------------------
# 7. 服务层生成器被关闭（等价断连）-> 未完成 task 取消
# ---------------------------------------------------------------------------
def generator_checks() -> None:
    print("\n=== 7. 生成器 aclose / is_disconnected 回调（任务取消，无 summary 误报） ===")
    clear_env()
    settings = load_settings()

    async def run() -> None:
        # a) 消费者关闭生成器（模拟客户端断开）
        fast = FakeAdapter("rf", delay=0.01)
        slow = FakeAdapter("bert", delay=5.0)
        service = make_service({"rf": fast, "bert": slow})
        generator = service.compare_stream(DEMO_TITLE, ["rf", "bert"], "rid-aclose")
        first = await asyncio.wait_for(generator.__anext__(), timeout=2)
        check(
            "generator yields first completed model event",
            isinstance(first, StreamModelEvent) and first.model_id == "rf",
            str(first),
        )
        await generator.aclose()
        await asyncio.sleep(0.05)
        check(
            "aclose cancels unfinished model task",
            slow.cancelled
            and all(task.done() for task in slow.tasks)
            and all(task.done() for task in fast.tasks),
            f"cancelled={slow.cancelled} done={[t.done() for t in slow.tasks]}",
        )

        # b) request.is_disconnected 等价回调：检测到断连后不再 push summary
        fast2 = FakeAdapter("rf", delay=0.01)
        slow2 = FakeAdapter("bert", delay=5.0)
        service2 = make_service(
            {"rf": fast2, "bert": slow2},
            compare_disconnect_poll_seconds=0.05,
        )
        polls = {"count": 0}

        async def is_disconnected() -> bool:
            polls["count"] += 1
            return polls["count"] >= 2

        observed: list[Any] = []

        async def collect() -> None:
            async for event in service2.compare_stream(
                DEMO_TITLE,
                ["rf", "bert"],
                "rid-disconnect",
                is_disconnected=is_disconnected,
            ):
                observed.append(event)

        await asyncio.wait_for(collect(), timeout=3)
        check(
            "disconnect callback stops push (no summary/done)",
            not any(isinstance(event, StreamSummaryEvent) for event in observed)
            and any(
                isinstance(event, StreamModelEvent) and event.model_id == "rf"
                for event in observed
            )
            and slow2.cancelled
            and polls["count"] >= 2,
            f"events={observed} cancelled={slow2.cancelled} polls={polls['count']}",
        )

    asyncio.run(run())


# ---------------------------------------------------------------------------
# 8. 真实 127.0.0.1 临时端口 uvicorn：增量 + 断连清理 + 端口释放
# ---------------------------------------------------------------------------
def real_server_checks() -> None:
    print("\n=== 8. 真实临时端口 uvicorn：流式增量 + 客户端断连清理 ===")
    clear_env()

    async def run() -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            os.environ["TMF_DB_PATH"] = str(Path(temp_dir) / "verify_b6_http.db")
            os.environ["TMF_LLM_QUOTA_HMAC_KEY"] = TEST_HMAC_KEY
            try:
                from serving.gateway.main import create_app

                app = create_app()
                fast = FakeAdapter("rf", delay=0.01)
                slow_llm = FakeAdapter("llm", delay=5.0)
                slow_bert = FakeAdapter("bert", delay=5.0)

                loop = asyncio.get_running_loop()
                problems: list[dict[str, Any]] = []
                previous_handler = loop.get_exception_handler()
                loop.set_exception_handler(
                    lambda _loop, context: problems.append(context)
                )

                error_records: list[logging.LogRecord] = []

                class CaptureErrors(logging.Handler):
                    def emit(self, record: logging.LogRecord) -> None:
                        if record.levelno >= logging.ERROR:
                            error_records.append(record)

                capture = CaptureErrors()
                watched = [
                    logging.getLogger("uvicorn.error"),
                    logging.getLogger("uvicorn"),
                    logging.getLogger("tmf.gateway"),
                ]
                for logger in watched:
                    logger.addHandler(capture)

                listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind(("127.0.0.1", 0))
                listener.listen(64)
                listener.setblocking(False)
                port = listener.getsockname()[1]

                server = uvicorn.Server(
                    uvicorn.Config(
                        app,
                        log_level="warning",
                        access_log=False,
                        lifespan="on",
                    )
                )
                server_task = asyncio.create_task(server.serve(sockets=[listener]))
                try:
                    for _ in range(500):
                        if server.started:
                            break
                        await asyncio.sleep(0.01)
                    check(
                        "temporary uvicorn server started on 127.0.0.1",
                        server.started,
                        f"port={port}",
                    )
                    # lifespan 已执行，此时才能拿到 settings/registry 并替换分类器。
                    settings = app.state.settings
                    app.state.classifier = ClassificationService(
                        registry=app.state.registry,
                        adapters={"rf": fast, "llm": slow_llm, "bert": slow_bert},
                        classes=settings.classes,
                        max_title_chars=settings.max_title_chars,
                        stats=app.state.stats,
                        logger=logging.getLogger("verify-compare-stream-http"),
                        compare_total_timeout_ms=18000,
                        compare_heartbeat_seconds=0.05,
                    )
                    url = (
                        f"http://127.0.0.1:{port}/api/v1/compare/stream"
                    )
                    response_headers: dict[str, str] = {}
                    lines: list[str] = []
                    first_model_at: float | None = None
                    heartbeat_seen = False
                    started = time.perf_counter()
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        async with client.stream(
                            "GET",
                            url,
                            params={
                                "title": DEMO_TITLE,
                                "models": "rf,llm,bert",
                            },
                        ) as response:
                            response_headers = {
                                key.lower(): value
                                for key, value in response.headers.items()
                            }
                            async for line in response.aiter_lines():
                                if not line:
                                    continue
                                lines.append(line)
                                if line == "event: model" and first_model_at is None:
                                    first_model_at = time.perf_counter() - started
                                if line == ": heartbeat":
                                    heartbeat_seen = True
                                if first_model_at is not None and heartbeat_seen:
                                    break
                    # Connection 是 hop-by-hop 头：TestClient 已在 ASGI 层断言应用
                    # 确实发送 keep-alive；真实链路上可能被 HTTP 客户端/中介重写
                    # （本机实测 httpx 侧显示为 close），故此处只断言应用可控的头。
                    check(
                        "real SSE response headers",
                        response_headers.get("content-type", "").startswith(
                            "text/event-stream"
                        )
                        and "charset=utf-8" in response_headers.get("content-type", "")
                        and response_headers.get("cache-control") == "no-cache"
                        and response_headers.get("x-accel-buffering") == "no",
                        str(response_headers),
                    )
                    check(
                        "real SSE pushes first model before slow workers finish",
                        first_model_at is not None and first_model_at < 2.0,
                        str(first_model_at),
                    )
                    check(
                        "real SSE heartbeat comment line pushed",
                        heartbeat_seen,
                        str(lines[-6:]),
                    )
                    check(
                        "real SSE line sequence is valid SSE",
                        all(
                            line.startswith(("event: ", "data: ", ": "))
                            for line in lines
                        ),
                        str(lines)[:300],
                    )
                    for _ in range(300):
                        if slow_llm.cancelled and slow_bert.cancelled:
                            break
                        await asyncio.sleep(0.01)
                    check(
                        "client disconnect cancels gateway-side unfinished tasks",
                        slow_llm.cancelled and slow_bert.cancelled,
                        f"llm={slow_llm.cancelled} bert={slow_bert.cancelled}",
                    )
                    check(
                        "cancelled adapter tasks finished (no task leak)",
                        all(task.done() for task in slow_llm.tasks)
                        and all(task.done() for task in slow_bert.tasks),
                        f"{[t.done() for t in slow_llm.tasks]} "
                        f"{[t.done() for t in slow_bert.tasks]}",
                    )
                    check(
                        "disconnected stream emits no summary/done",
                        not any(
                            line.startswith(("event: summary", "event: done"))
                            for line in lines
                        ),
                        str(lines)[:300],
                    )
                finally:
                    server.should_exit = True
                    await asyncio.wait_for(server_task, timeout=10)
                    listener.close()
                    for logger in watched:
                        logger.removeHandler(capture)
                    loop.set_exception_handler(previous_handler)

                unretrieved = [
                    context
                    for context in problems
                    if "never retrieved" in str(context.get("message", ""))
                    or "never awaited" in str(context.get("message", ""))
                ]
                check(
                    "no unhandled asyncio task exception on disconnect",
                    not unretrieved,
                    str(unretrieved)[:300],
                )
                check(
                    "no ERROR log (traceback) during stream/disconnect",
                    not error_records,
                    str([record.getMessage() for record in error_records])[:300],
                )
                released = False
                try:
                    probe = socket.create_connection(("127.0.0.1", port), timeout=0.5)
                    probe.close()
                except OSError:
                    released = True
                check(
                    "temporary uvicorn port released",
                    released,
                    str(port),
                )
            finally:
                os.environ.pop("TMF_DB_PATH", None)

    asyncio.run(run())


def main() -> int:
    clear_env()
    print("=== B6 /compare/stream SSE 验收（fake adapter，不启动真实 worker/LLM） ===")
    config_checks()
    api_checks()
    generator_checks()
    real_server_checks()
    clear_env()
    if FAILURES:
        print(f"RESULT FAIL ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
