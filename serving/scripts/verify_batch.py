#!/usr/bin/env python
"""阶段 2A 批量分类隔离验收（不启动服务、不调用真实模型/LLM）。

运行环境（与现有网关脚本一致）：
    $env:PYTHONPATH='E:\\AIModels\\test\\TouManFen\\venv-gateway\\Lib\\site-packages'
    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/verify_batch.py

覆盖：
1. 合法单条 / 多条 / 重复标题；
2. 逐条成功、部分失败、全部失败、顺序与 item_id 映射；
3. 空 items、101 条、单条超长、空标题、非法/重复 item_id、非法模型；
4. 批次总字符上限（默认 20,000 与收紧配置两条边界）；
5. LLM 在 worker 调用前被拒绝，LLM fake adapter 调用数为 0；
6. 30s/可配总截止、进程级并发峰值、取消/断连停止调度；
7. 批量不触发 feedback hint；
8. 日志不含标题；
9. /classify、/compare、/compare/stream 基本回归。

全部使用 fake adapter + 最小 TestClient app；不读写真实 DB、不启动服务。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from serving.adapters import (  # noqa: E402
    AdapterResult,
    ModelAdapter,
    ScoreEntry,
)
from serving.gateway.errors import (  # noqa: E402
    ApiError,
    ERROR_BATCH_CANCELLED,
    ERROR_BATCH_INVALID,
    ERROR_BATCH_LLM_NOT_ALLOWED,
    ERROR_BATCH_TOTAL_TIMEOUT,
    ERROR_BATCH_TOO_LARGE,
    ERROR_INTERNAL,
    ERROR_WORKER_UNAVAILABLE,
    error_payload,
)
from serving.gateway.routers import classify as classify_router  # noqa: E402
from serving.gateway.routers import stream as stream_router  # noqa: E402
from serving.gateway.settings import load_settings  # noqa: E402
from serving.gateway.stats import StatsCollector  # noqa: E402
from serving.models.registry import ModelRegistry  # noqa: E402
from serving.services.classification import (  # noqa: E402
    BatchItemInput,
    ClassificationService,
)

FAILURES: list[str] = []
ENV_KEYS = (
    "TMF_BATCH_MAX_ITEMS",
    "TMF_BATCH_MAX_TOTAL_CHARS",
    "TMF_BATCH_CONCURRENCY",
    "TMF_BATCH_TOTAL_TIMEOUT_MS",
    "TMF_MAX_TITLE_CHARS",
)


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


def clear_env() -> None:
    for key in ENV_KEYS:
        os.environ.pop(key, None)


class FakeAdapter:
    """可计数、可延迟、可制造逐条失败的 ModelAdapter。"""

    def __init__(
        self,
        model_id: str,
        *,
        delay_s: float = 0.0,
        fail_titles: bool = False,
    ) -> None:
        self.model_id = model_id
        self.model_version: str | None = None
        self.delay_s = delay_s
        self.fail_titles = fail_titles
        self.calls = 0
        self.active = 0
        self.max_active = 0

    async def predict(self, title: str, request_id: str) -> AdapterResult:
        self.calls += 1
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            # "FAST" 条目立即返回，用于验证总截止时已完成结果被保留。
            if self.delay_s and "FAST" not in title:
                await asyncio.sleep(self.delay_s)
            if self.fail_titles and "FAIL" in title:
                return AdapterResult(
                    model_id=self.model_id,
                    status="error",
                    latency_ms=1,
                    error_code=ERROR_WORKER_UNAVAILABLE,
                    error_message="fake worker unavailable",
                    data_source="mock",
                )
            return AdapterResult(
                model_id=self.model_id,
                status="ok",
                latency_ms=1,
                pred_class="finance",
                scores=(
                    ScoreEntry("finance", 0.9),
                    ScoreEntry("sports", 0.1),
                ),
                score_kind="raw_proba",
                top_k=(
                    ScoreEntry("finance", 0.9),
                    ScoreEntry("sports", 0.1),
                ),
                data_source="mock",
            )
        finally:
            self.active -= 1


class FakeFeedback:
    def __init__(self) -> None:
        self.calls = 0

    async def similar_hint(self, title: str):
        self.calls += 1
        return None


class CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())
        self.messages.append(str(record.__dict__))


def make_service(
    *,
    delay_s: float = 0.0,
    fail_titles: bool = False,
    batch_max_items: int | None = None,
    batch_max_total_chars: int | None = None,
    batch_concurrency: int | None = None,
    batch_total_timeout_ms: int | None = None,
):
    settings = load_settings()
    registry = ModelRegistry(settings)
    adapters: dict[str, FakeAdapter] = {
        model_id: FakeAdapter(
            model_id, delay_s=delay_s, fail_titles=fail_titles
        )
        for model_id in settings.models
    }
    handler = CapturingHandler()
    logger = logging.getLogger(f"verify-batch-{uuid.uuid4().hex}")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    service = ClassificationService(
        registry=registry,
        adapters={key: value for key, value in adapters.items()},
        classes=settings.classes,
        max_title_chars=settings.max_title_chars,
        stats=StatsCollector(),
        logger=logger,
        reject=settings.reject,
        batch_max_items=(
            settings.batch_max_items
            if batch_max_items is None
            else batch_max_items
        ),
        batch_max_total_chars=(
            settings.batch_max_total_chars
            if batch_max_total_chars is None
            else batch_max_total_chars
        ),
        batch_concurrency=(
            settings.batch_concurrency
            if batch_concurrency is None
            else batch_concurrency
        ),
        batch_total_timeout_ms=(
            settings.batch_total_timeout_ms
            if batch_total_timeout_ms is None
            else batch_total_timeout_ms
        ),
    )
    return service, adapters, handler


def build_app(service: ClassificationService, feedback=None) -> FastAPI:
    """最小应用：只挂分类路由、request_id 中间件与统一错误处理。"""
    app = FastAPI()
    app.state.classifier = service
    if feedback is not None:
        app.state.feedback = feedback

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(
                request_id, exc.code, exc.message, exc.details
            ),
            headers={"X-Request-ID": request_id},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError):
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        return JSONResponse(
            status_code=422,
            content=error_payload(
                request_id,
                "INVALID_REQUEST",
                "请求参数校验失败",
            ),
            headers={"X-Request-ID": request_id},
        )

    app.include_router(classify_router.router)
    app.include_router(stream_router.router)
    return app


def post_batch(service: ClassificationService, payload: dict, checks):
    with TestClient(build_app(service)) as client:
        return client.post("/api/v1/classify/batch", json=payload)


def assert_error(response, status: int, code: str, label: str, checks) -> None:
    body = response.json() if response.content else {}
    checks(
        f"{label}: HTTP {status} + {code}",
        response.status_code == status
        and body.get("error", {}).get("code") == code,
        f"{response.status_code} {body}",
    )


def api_checks() -> None:
    print("\n=== 1. API 契约与批量成功/失败 ===")
    service, adapters, _ = make_service()
    payload = {
        "model": "fasttext",
        "batch_id": "paste-test-1",
        "items": [
            {"item_id": "r0001", "title": "北京二手房成交量创年内新高"},
            {"item_id": "r0002", "title": "Zacks调升苹果目标股价至200美元"},
        ],
    }
    response = post_batch(service, payload, check)
    body = response.json()
    check("valid batch: HTTP 200", response.status_code == 200, response.text[:300])
    check("valid batch: request_id/batch_id/model_id", isinstance(body.get("request_id"), str) and body.get("batch_id") == "paste-test-1" and body.get("model_id") == "fasttext", str(body)[:300])
    check("valid batch: summary total=2 ok=2 failed=0 status=ok", body.get("summary") == {"total": 2, "ok": 2, "failed": 0, "status": "ok", "elapsed_ms": body.get("summary", {}).get("elapsed_ms"), "total_timeout_ms": 30000}, str(body.get("summary")))
    check("valid batch: result order/item_id mapping", [item.get("item_id") for item in body.get("results", [])] == ["r0001", "r0002"] and [item.get("index") for item in body.get("results", [])] == [0, 1], str(body.get("results"))[:300])
    check("valid batch: success fields include core classification", all(item.get("status") == "ok" and item.get("pred_class") == "finance" and item.get("latency_ms") == 1 and item.get("model_version") is None and item.get("data_source") == "mock" and item.get("top_k") for item in body.get("results", [])), str(body.get("results"))[:500])
    check("valid batch: no human_feedback_hint", all("human_feedback_hint" not in item for item in body.get("results", [])) and "human_feedback_hint" not in body, str(body)[:500])
    check("valid batch: exactly 2 adapter calls", adapters["fasttext"].calls == 2, str(adapters["fasttext"].calls))

    repeated_service, repeated_adapters, _ = make_service()
    repeated = {
        "model": "fasttext",
        "items": [
            {"item_id": "dup-a", "title": "重复标题"},
            {"item_id": "dup-b", "title": "重复标题"},
        ],
    }
    repeated_response = post_batch(repeated_service, repeated, check)
    repeated_body = repeated_response.json()
    check("duplicate title allowed and separately classified", repeated_response.status_code == 200 and repeated_body.get("summary", {}).get("ok") == 2 and [item.get("item_id") for item in repeated_body.get("results", [])] == ["dup-a", "dup-b"], str(repeated_body)[:300])
    check("duplicate title: two adapter calls", repeated_adapters["fasttext"].calls == 2, str(repeated_adapters["fasttext"].calls))

    partial_service, partial_adapters, _ = make_service(fail_titles=True)
    partial_payload = {
        "model": "fasttext",
        "items": [
            {"item_id": "ok-1", "title": "正常标题"},
            {"item_id": "fail-1", "title": "FAIL 标题"},
            {"item_id": "ok-2", "title": "另一个正常标题"},
        ],
    }
    partial_response = post_batch(partial_service, partial_payload, check)
    partial_body = partial_response.json()
    partial_results = partial_body.get("results", [])
    check("partial: HTTP 200 summary partial ok=2 failed=1", partial_response.status_code == 200 and partial_body.get("summary", {}).get("status") == "partial" and partial_body.get("summary", {}).get("ok") == 2 and partial_body.get("summary", {}).get("failed") == 1, str(partial_body.get("summary")))
    check("partial: failed item has stable error and no pred_class", partial_results[1].get("status") == "error" and partial_results[1].get("error", {}).get("code") == ERROR_WORKER_UNAVAILABLE and "pred_class" not in partial_results[1], str(partial_results[1]))
    check("partial: successful items retained and ordered", partial_results[0].get("status") == "ok" and partial_results[2].get("status") == "ok" and [item.get("item_id") for item in partial_results] == ["ok-1", "fail-1", "ok-2"], str([item.get("status") for item in partial_results]))
    check("partial: adapter calls = 3", partial_adapters["fasttext"].calls == 3, str(partial_adapters["fasttext"].calls))

    all_fail_service, _, _ = make_service(fail_titles=True)
    all_fail_payload = {
        "model": "fasttext",
        "items": [
            {"item_id": "f1", "title": "FAIL 1"},
            {"item_id": "f2", "title": "FAIL 2"},
        ],
    }
    all_fail_response = post_batch(all_fail_service, all_fail_payload, check)
    all_fail_body = all_fail_response.json()
    check("all failed: HTTP 200 all_failed and no fake pred_class", all_fail_response.status_code == 200 and all_fail_body.get("summary", {}).get("status") == "all_failed" and all(item.get("status") == "error" and "pred_class" not in item for item in all_fail_body.get("results", [])), str(all_fail_body)[:500])


def preflight_checks() -> None:
    print("\n=== 2. 整批预检：失败时 worker 调用数必须为 0 ===")
    cases = [
        ("empty items", {"model": "fasttext", "items": []}, 400, ERROR_BATCH_INVALID),
        (
            "101 items",
            {
                "model": "fasttext",
                "items": [
                    {"item_id": f"r{i}", "title": "x"} for i in range(101)
                ],
            },
            400,
            ERROR_BATCH_TOO_LARGE,
        ),
        (
            "title over 200 code points",
            {
                "model": "fasttext",
                "items": [{"item_id": "r1", "title": "x" * 201}],
            },
            400,
            ERROR_BATCH_INVALID,
        ),
        (
            "empty title",
            {
                "model": "fasttext",
                "items": [{"item_id": "r1", "title": "   "}],
            },
            400,
            ERROR_BATCH_INVALID,
        ),
        (
            "invalid item_id",
            {
                "model": "fasttext",
                "items": [{"item_id": "bad id!", "title": "x"}],
            },
            400,
            ERROR_BATCH_INVALID,
        ),
        (
            "duplicate item_id",
            {
                "model": "fasttext",
                "items": [
                    {"item_id": "same", "title": "a"},
                    {"item_id": "same", "title": "b"},
                ],
            },
            400,
            ERROR_BATCH_INVALID,
        ),
        (
            "unknown model",
            {
                "model": "no-such-model",
                "items": [{"item_id": "r1", "title": "x"}],
            },
            404,
            "UNKNOWN_MODEL",
        ),
        (
            "invalid batch_id",
            {
                "model": "fasttext",
                "batch_id": "bad/id",
                "items": [{"item_id": "r1", "title": "x"}],
            },
            400,
            ERROR_BATCH_INVALID,
        ),
    ]
    for label, payload, status, code in cases:
        service, adapters, _ = make_service()
        response = post_batch(service, payload, check)
        assert_error(response, status, code, label, check)
        calls = sum(adapter.calls for adapter in adapters.values())
        check(f"{label}: no worker call", calls == 0, str(calls))

    print("\n=== 3. 总字符上限（默认上限与收紧配置） ===")
    # 已确认上限 100 条 × 200 字符 = 20,000，刚好等于总字符上限；因此默认
    # 配置下不存在“同时满足条数/单条上限但总字符 >20,000”的合法载荷。
    # 这里把总字符上限收紧到 19,999，用 100×200 证明总字符闸门确实生效。
    over_service, over_adapters, _ = make_service(batch_max_total_chars=19999)
    over_payload = {
        "model": "fasttext",
        "items": [
            {"item_id": f"r{i}", "title": "x" * 200} for i in range(100)
        ],
    }
    over_response = post_batch(over_service, over_payload, check)
    assert_error(
        over_response,
        400,
        ERROR_BATCH_TOO_LARGE,
        "total chars over configured 19,999 limit",
        check,
    )
    check(
        "total chars over configured limit: no worker call",
        sum(adapter.calls for adapter in over_adapters.values()) == 0,
        str(sum(adapter.calls for adapter in over_adapters.values())),
    )

    max_service, max_adapters, _ = make_service(batch_max_total_chars=20000)
    max_payload = {
        "model": "fasttext",
        "items": [
            {"item_id": f"m{i}", "title": "x" * 200} for i in range(100)
        ],
    }
    max_response = post_batch(max_service, max_payload, check)
    max_body = max_response.json()
    check("100 x 200 code points accepted at default boundaries", max_response.status_code == 200 and max_body.get("summary", {}).get("total") == 100 and max_body.get("summary", {}).get("ok") == 100, str(max_body.get("summary")))
    check("100-item batch calls worker 100 times", max_adapters["fasttext"].calls == 100, str(max_adapters["fasttext"].calls))


def llm_checks() -> None:
    print("\n=== 4. LLM 在 worker 调用前拒绝 ===")
    service, adapters, _ = make_service()
    payload = {
        "model": "llm",
        "items": [{"item_id": "r1", "title": "x"}],
    }
    response = post_batch(service, payload, check)
    assert_error(response, 400, ERROR_BATCH_LLM_NOT_ALLOWED, "model=llm", check)
    check("model=llm: llm adapter calls = 0", adapters["llm"].calls == 0, str(adapters["llm"].calls))
    check("model=llm: all adapter calls = 0", sum(adapter.calls for adapter in adapters.values()) == 0, str({key: adapter.calls for key, adapter in adapters.items()}))


async def direct_checks() -> None:
    print("\n=== 5. 总截止、并发峰值、取消与日志隐私 ===")
    timeout_service, _, _ = make_service(
        delay_s=1.0,
        batch_total_timeout_ms=250,
        batch_concurrency=2,
    )
    started = asyncio.get_running_loop().time()
    outcome = await timeout_service.classify_batch(
        model="fasttext",
        batch_id="timeout-batch",
        items=[
            BatchItemInput("fast", "FAST 已完成"),
            BatchItemInput("t1", "slow 1"),
            BatchItemInput("t2", "slow 2"),
            BatchItemInput("t3", "slow 3"),
        ],
        request_id="rid-timeout",
    )
    elapsed = asyncio.get_running_loop().time() - started
    check("deadline: quick partial response", elapsed < 2.0, f"{elapsed:.2f}s")
    check(
        "deadline: completed success preserved and unprocessed marked timeout",
        outcome.status == "partial"
        and outcome.items[0].status == "ok"
        and all(
            item.status == "error"
            and item.error_code == ERROR_BATCH_TOTAL_TIMEOUT
            for item in outcome.items[1:]
        ),
        str([(item.status, item.error_code) for item in outcome.items]),
    )
    await asyncio.sleep(0.1)
    check(
        "deadline: no fake adapter still active",
        all(adapter.active == 0 for adapter in timeout_service._adapters.values()),
        "active",
    )

    concurrency_service, concurrency_adapters, _ = make_service(
        delay_s=0.05,
        batch_concurrency=2,
    )
    batch_a = [
        BatchItemInput(f"a{i}", f"a {i}") for i in range(10)
    ]
    batch_b = [
        BatchItemInput(f"b{i}", f"b {i}") for i in range(10)
    ]
    await asyncio.gather(
        concurrency_service.classify_batch(
            model="fasttext",
            batch_id="batch-a",
            items=batch_a,
            request_id="rid-a",
        ),
        concurrency_service.classify_batch(
            model="fasttext",
            batch_id="batch-b",
            items=batch_b,
            request_id="rid-b",
        ),
    )
    peak = max(adapter.max_active for adapter in concurrency_adapters.values())
    check("concurrency: process-wide peak <= 2 across two batches", peak <= 2, str(peak))
    check("concurrency: all 20 items called", concurrency_adapters["fasttext"].calls == 20, str(concurrency_adapters["fasttext"].calls))

    cancel_service, cancel_adapters, _ = make_service(delay_s=0.5)
    calls = {"count": 0}

    async def disconnected() -> bool:
        calls["count"] += 1
        return calls["count"] >= 2

    cancel_outcome = await cancel_service.classify_batch(
        model="fasttext",
        batch_id="cancel-batch",
        items=[
            BatchItemInput(f"c{i}", f"cancel {i}") for i in range(10)
        ],
        request_id="rid-cancel",
        is_disconnected=disconnected,
    )
    check("cancel: stopped scheduling and marked cancelled", cancel_outcome.status in {"partial", "all_failed"} and any(item.error_code == ERROR_BATCH_CANCELLED for item in cancel_outcome.items), str([(item.status, item.error_code) for item in cancel_outcome.items]))
    check("cancel: not all 10 items called", cancel_adapters["fasttext"].calls < 10, str(cancel_adapters["fasttext"].calls))

    log_service, _, log_handler = make_service()
    await log_service.classify_batch(
        model="fasttext",
        batch_id="log-batch",
        items=[
            BatchItemInput("log-1", "SECRET_TITLE_SHOULD_NOT_APPEAR"),
        ],
        request_id="rid-log",
    )
    joined = "\n".join(log_handler.messages)
    check("logs: title body not present", "SECRET_TITLE_SHOULD_NOT_APPEAR" not in joined, joined[:500])


def feedback_checks() -> None:
    print("\n=== 6. 批量不触发 feedback hint ===")
    service, _, _ = make_service()
    feedback = FakeFeedback()
    with TestClient(build_app(service, feedback=feedback)) as client:
        response = client.post(
            "/api/v1/classify/batch",
            json={
                "model": "fasttext",
                "items": [{"item_id": "r1", "title": "正常标题"}],
            },
        )
    check("feedback hint: batch HTTP 200", response.status_code == 200, response.text[:300])
    check("feedback hint: similar_hint calls = 0", feedback.calls == 0, str(feedback.calls))


def regression_checks() -> None:
    print("\n=== 7. 单条/compare/SSE 基本回归 ===")
    service, _, _ = make_service()
    with TestClient(build_app(service)) as client:
        single = client.post(
            "/api/v1/classify",
            json={"title": "北京二手房成交量创年内新高", "model": "fasttext"},
        )
        check("regression: /classify 200 and pred_class", single.status_code == 200 and single.json().get("pred_class") == "finance", single.text[:300])

        compare = client.post(
            "/api/v1/compare",
            json={"title": "北京二手房成交量创年内新高", "models": ["fasttext", "bert"]},
        )
        compare_body = compare.json()
        check("regression: /compare 200 and summary total=2", compare.status_code == 200 and compare_body.get("summary", {}).get("total") == 2 and len(compare_body.get("results", [])) == 2, compare.text[:300])

        stream = client.get(
            "/api/v1/compare/stream",
            params={"title": "北京二手房成交量创年内新高", "models": "fasttext,bert"},
        )
        stream_text = stream.text
        check("regression: /compare/stream 200 + model/summary/done", stream.status_code == 200 and "event: model" in stream_text and "event: summary" in stream_text and "event: done" in stream_text, stream_text[:300])


def main() -> int:
    clear_env()
    print("=== 阶段 2A 批量分类隔离验收（fake adapter / TestClient，不连真实服务） ===")
    api_checks()
    preflight_checks()
    llm_checks()
    asyncio.run(direct_checks())
    feedback_checks()
    regression_checks()
    clear_env()
    if FAILURES:
        print(f"RESULT FAIL ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
