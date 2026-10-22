#!/usr/bin/env python
"""统一适配层 / 网关配置 / API 行为验收（不加载模型、不启动真实服务）。

运行环境：网关依赖（fastapi/uvicorn/httpx/pydantic/yaml），例如
    $env:PYTHONPATH='<repo>\\venv-gateway\\Lib\\site-packages'
    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/verify_refactor_gateway.py

覆盖：
1. 默认配置 + 环境变量覆盖 + 非法配置拒绝；
2. HttpWorkerAdapter / LlmAdapter：成功、不可用、超时、非 200、非法 JSON、
   缺字段、非法类别、worker error；
3. ClassificationService 只依赖 ModelAdapter 协议（fake adapter 注入）；
4. 网关 /healthz、/models、/readyz、/stats、/classify、/compare、request-id、CORS；
5. 运行时代码/配置中不再存在固定局域网 IP；
6. B10 集成契约（全部 fake adapter / 临时 TMF_DB_PATH，不连真实 worker）：
   - B1 分数字段：``scores`` / ``score_kind`` / ``top_k``；
   - B3 软拒识：``rejected`` / ``reject_reason`` / ``threshold_meta``；
   - B11 演示真实性：``/classify``、``/compare`` 每项、SSE ``model`` 事件的
     ``data_source``（live / mock / unknown / null 不伪造），且 mock 不计入统计；
   - B6 SSE 事件契约（响应头 + model/summary/done 形状）；
   - B7 竞技场 ``/arena/next|vote|leaderboard``（匿名、揭晓、Elo、warming_up）；
   - B4 反馈 ``/feedback`` + ``/feedback/stats``（pending、重复 409、hint、清理）；
   - B8 ``/bench/pk`` 只读产物契约（有产物则结构断言，无产物则 404 分支）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from serving.adapters import (  # noqa: E402
    DATA_SOURCE_LIVE,
    DATA_SOURCE_MOCK,
    DATA_SOURCE_UNKNOWN,
    AdapterResult,
    HttpWorkerAdapter,
    LlmAdapter,
    ModelAdapter,
    PredictionResult,
    ScoreEntry,
)
from serving.gateway.errors import (  # noqa: E402
    ApiError,
    ConfigError,
    ERROR_ARENA_DUPLICATE_VOTE,
    ERROR_BENCH_NOT_FOUND,
    ERROR_FEEDBACK_DUPLICATE,
)
from serving.gateway.settings import load_settings  # noqa: E402
from serving.gateway.stats import StatsCollector  # noqa: E402
from serving.models.registry import ModelRegistry  # noqa: E402
from serving.services.arena import ArenaService  # noqa: E402
from serving.services.classification import ClassificationService  # noqa: E402

FAILURES: list[str] = []
ENV_KEYS = (
    "TMF_BIND_HOST",
    "TMF_GATEWAY_HOST",
    "TMF_GATEWAY_PORT",
    "TMF_FRONTEND_HOST",
    "TMF_FRONTEND_PORT",
    "TMF_WORKER_HOST",
    "TMF_WORKER_URL_RF",
    "TMF_WORKER_URL_LLM",
    "TMF_CORS_ORIGINS",
    # B10：避免外部环境变量改变本次契约断言（DB 由 main() 强制临时路径）。
    "TMF_COMPARE_CONCURRENCY",
    "TMF_COMPARE_TOTAL_TIMEOUT_MS",
    "TMF_REJECT_ENABLED",
    "TMF_REJECT_MAX_SCORE_THRESHOLD",
    "TMF_REJECT_ENTROPY_THRESHOLD",
    "TMF_ARENA_TEST_FILE",
    "TMF_BENCH_DIR",
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


def set_env(**values: str) -> None:
    clear_env()
    for key, value in values.items():
        os.environ[key] = value


class FakeAdapter:
    """仅实现 ModelAdapter 协议的测试适配器。"""

    def __init__(self, model_id: str, result: AdapterResult) -> None:
        self.model_id = model_id
        self.model_version: str | None = None
        self._result = result

    async def predict(self, title: str, request_id: str) -> AdapterResult:
        return self._result


def ok_result(model_id: str, pred: str = "sports", latency: int = 12) -> AdapterResult:
    return AdapterResult(
        model_id=model_id, status="ok", latency_ms=latency, pred_class=pred
    )


def error_result(model_id: str, code: str = "WORKER_TIMEOUT") -> AdapterResult:
    return AdapterResult(
        model_id=model_id,
        status="error",
        latency_ms=3,
        error_code=code,
        error_message="所选模型未在配置的时间内返回",
    )


def scored_body(
    classes: list[str],
    *,
    pred: str = "sports",
    score_overrides: dict[str, object] | None = None,
    top_k: list[dict[str, object]] | None = None,
    score_kind: str = "raw_softmax",
) -> dict[str, object]:
    """构造 worker 成功响应（含有效分数、score_kind、top_k）。"""
    scores: dict[str, object] = {class_id: 0.01 for class_id in classes}
    scores[pred] = 0.91
    if score_overrides:
        scores.update(score_overrides)
    if top_k is None:
        ranked = sorted(scores.items(), key=lambda item: (-float(item[1]), item[0]))
        top_k = [
            {"class_id": class_id, "score": score}
            for class_id, score in ranked[:3]
        ]
    return {
        "title": "北京二手房成交量创年内新高",
        "pred_class": pred,
        "scores": scores,
        "score_kind": score_kind,
        "top_k": top_k,
    }


def json_body_handler(body: dict[str, object]):
    """把响应体编码为 JSON（允许 NaN/Infinity 以验证非有限数降级）。"""
    text = json.dumps(body, ensure_ascii=False, allow_nan=True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=text.encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )

    return handler


def scored_adapter_result(model_id: str, classes: list[str]) -> AdapterResult:
    """构造“已通过适配器校验”的成功结果，用于 API 层断言。"""
    scores = {class_id: 0.01 for class_id in classes}
    scores["sports"] = 0.91
    entries = tuple(
        ScoreEntry(class_id=class_id, score=score)
        for class_id, score in sorted(scores.items())
    )
    top = tuple(
        ScoreEntry(class_id=class_id, score=score)
        for class_id, score in sorted(
            scores.items(), key=lambda item: (-item[1], item[0])
        )[:3]
    )
    return AdapterResult(
        model_id=model_id,
        status="ok",
        latency_ms=12,
        pred_class="sports",
        scores=entries,
        score_kind="raw_softmax",
        top_k=top,
    )


def invalid_score_bodies(classes: list[str]) -> dict[str, dict[str, object]]:
    """四类异常分数（+score_kind/top_k 异常）的 worker 响应。"""
    cases: dict[str, dict[str, object]] = {}

    body = scored_body(classes)
    body["scores"].pop(classes[0])  # type: ignore[union-attr]
    cases["length mismatch"] = body

    body = scored_body(classes)
    body["scores"][classes[0]] = float("nan")  # type: ignore[index]
    cases["NaN"] = body

    body = scored_body(classes)
    body["scores"][classes[0]] = float("inf")  # type: ignore[index]
    cases["Infinity"] = body

    body = scored_body(classes)
    body["scores"][classes[0]] = 1.5  # type: ignore[index]
    cases["out of range"] = body

    body = scored_body(classes)
    body["scores"][classes[0]] = "0.01"  # type: ignore[index]
    cases["non numeric"] = body

    body = scored_body(classes)
    body["score_kind"] = "calibrated"
    cases["unknown score_kind"] = body

    body = scored_body(classes)
    body["top_k"] = list(reversed(body["top_k"]))  # type: ignore[arg-type]
    cases["top_k unsorted"] = body

    body = scored_body(classes)
    body["top_k"] = body["top_k"][:2]  # type: ignore[index]
    cases["top_k wrong length"] = body

    return cases


def make_service(adapters: dict[str, ModelAdapter]) -> ClassificationService:
    settings = load_settings()
    return ClassificationService(
        registry=ModelRegistry(settings),
        adapters=adapters,
        classes=settings.classes,
        max_title_chars=settings.max_title_chars,
        stats=StatsCollector(),
        logger=logging.getLogger("verify-refactor"),
    )


async def adapter_case(handler, model_id: str = "rf") -> AdapterResult:
    settings = load_settings()
    spec = ModelRegistry(settings).get(model_id)
    assert spec is not None
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = LlmAdapter(spec, settings.classes, client) if model_id == "llm" else HttpWorkerAdapter(
            spec, settings.classes, client
        )
        return await adapter.predict("北京二手房成交量创年内新高", "verify-rid")


def config_checks() -> None:
    clear_env()
    settings = load_settings()
    check(
        "default worker urls are loopback",
        all(
            httpx.URL(worker.base_url).host == "127.0.0.1"
            for worker in settings.workers.values()
        ),
        str([worker.base_url for worker in settings.workers.values()]),
    )
    check("default bind host", settings.network.bind_host == "127.0.0.1")
    check(
        "default gateway address",
        (settings.network.gateway_host, settings.network.gateway_port)
        == ("127.0.0.1", 8000),
    )
    check(
        "default cors origin",
        settings.network.cors_origins == ("http://127.0.0.1:5173",),
        str(settings.network.cors_origins),
    )

    set_env(TMF_WORKER_HOST="127.0.0.2")
    settings = load_settings()
    check(
        "TMF_WORKER_HOST overrides host only",
        all(
            httpx.URL(worker.base_url).host == "127.0.0.2"
            and httpx.URL(worker.base_url).port in {8101, 8103, 8104, 8105, 8106}
            for worker in settings.workers.values()
        ),
        str([worker.base_url for worker in settings.workers.values()]),
    )

    set_env(TMF_WORKER_URL_RF="http://127.0.0.1:18101")
    settings = load_settings()
    check(
        "TMF_WORKER_URL_<ID> overrides full url",
        settings.workers["rf"].base_url == "http://127.0.0.1:18101",
        settings.workers["rf"].base_url,
    )

    set_env(TMF_GATEWAY_PORT="18000", TMF_FRONTEND_PORT="15173")
    settings = load_settings()
    check(
        "gateway/frontend port overrides",
        settings.network.gateway_port == 18000
        and settings.network.frontend_origin == "http://127.0.0.1:15173",
        f"{settings.network.gateway_port} {settings.network.frontend_origin}",
    )

    set_env(TMF_FRONTEND_HOST="0.0.0.0")
    settings = load_settings()
    check(
        "wildcard frontend host uses loopback for CORS origin",
        settings.network.frontend_origin == "http://127.0.0.1:5173",
        settings.network.frontend_origin,
    )

    set_env(TMF_CORS_ORIGINS="http://127.0.0.1:5174,http://127.0.0.1:5175")
    settings = load_settings()
    check(
        "explicit cors origins appended",
        "http://127.0.0.1:5174" in settings.network.cors_origins
        and "http://127.0.0.1:5175" in settings.network.cors_origins,
        str(settings.network.cors_origins),
    )

    invalid_cases = [
        ("TMF_WORKER_HOST", "http://bad-host"),
        ("TMF_GATEWAY_PORT", "70000"),
        ("TMF_GATEWAY_PORT", "abc"),
        ("TMF_CORS_ORIGINS", "*"),
        ("TMF_FRONTEND_HOST", "a/b"),
        ("TMF_WORKER_URL_RF", "ftp://127.0.0.1:8101"),
    ]
    for key, value in invalid_cases:
        set_env(**{key: value})
        try:
            load_settings()
            check(f"invalid {key}={value} rejected", False)
        except ConfigError:
            check(f"invalid {key}={value} rejected", True)
        except Exception as exc:  # noqa: BLE001
            check(f"invalid {key}={value} rejected", False, type(exc).__name__)
    clear_env()


def adapter_checks() -> None:
    clear_env()

    def handler_ok(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"title": "x", "pred_class": "sports"})

    result = asyncio.run(adapter_case(handler_ok))
    check(
        "adapter success",
        result.status == "ok" and result.pred_class == "sports" and result.latency_ms >= 0,
        str(result),
    )

    def handler_500(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    result = asyncio.run(adapter_case(handler_500))
    check(
        "adapter non-200 -> WORKER_HTTP_ERROR",
        result.error_code == "WORKER_HTTP_ERROR",
        str(result),
    )

    def handler_bad_json(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not-json")

    result = asyncio.run(adapter_case(handler_bad_json))
    check(
        "adapter invalid json -> WORKER_BAD_RESPONSE",
        result.error_code == "WORKER_BAD_RESPONSE",
        str(result),
    )

    def handler_missing(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"title": "x"})

    result = asyncio.run(adapter_case(handler_missing))
    check(
        "adapter missing pred_class -> WORKER_BAD_RESPONSE",
        result.error_code == "WORKER_BAD_RESPONSE",
        str(result),
    )

    def handler_bad_class(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"title": "x", "pred_class": "not_a_class"})

    result = asyncio.run(adapter_case(handler_bad_class))
    check(
        "adapter invalid class -> INVALID_MODEL_OUTPUT",
        result.error_code == "INVALID_MODEL_OUTPUT",
        str(result),
    )

    def handler_worker_error(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"pred_class": "", "error": "mock error"})

    result = asyncio.run(adapter_case(handler_worker_error))
    check(
        "adapter worker error -> MODEL_ERROR",
        result.error_code == "MODEL_ERROR",
        str(result),
    )

    def handler_timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    result = asyncio.run(adapter_case(handler_timeout))
    check(
        "adapter timeout -> WORKER_TIMEOUT",
        result.error_code == "WORKER_TIMEOUT",
        str(result),
    )

    def handler_connect(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    result = asyncio.run(adapter_case(handler_connect))
    check(
        "adapter connect error -> WORKER_UNAVAILABLE",
        result.error_code == "WORKER_UNAVAILABLE",
        str(result),
    )

    fake = FakeAdapter("rf", ok_result("rf"))
    check("fake adapter satisfies ModelAdapter protocol", isinstance(fake, ModelAdapter))

    settings = load_settings()
    spec = ModelRegistry(settings).get("llm")
    assert spec is not None
    async def llm_adapter_check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler_ok)) as client:
            adapter = LlmAdapter(spec, settings.classes, client)
            return adapter, await adapter.predict("x", "rid")
    adapter, result = asyncio.run(llm_adapter_check())
    check(
        "LlmAdapter is a ModelAdapter",
        isinstance(adapter, ModelAdapter) and result.status == "ok",
        str(result),
    )
    check("PredictionResult has required fields", PredictionResult("sports", 1).latency_ms == 1)


def score_adapter_checks() -> None:
    """B1：适配器解析未校准分数；四类异常分数降级且保留 pred_class。"""
    clear_env()
    classes = list(load_settings().classes)

    result = asyncio.run(adapter_case(json_body_handler(scored_body(classes))))
    check(
        "adapter valid scores parsed",
        result.status == "ok"
        and result.pred_class == "sports"
        and result.score_kind == "raw_softmax"
        and result.scores is not None
        and len(result.scores) == len(classes)
        and {entry.class_id: entry.score for entry in result.scores}["sports"]
        == 0.91
        and result.top_k is not None
        and len(result.top_k) == 3
        and result.top_k[0].class_id == "sports"
        and result.top_k[0].score == 0.91,
        str(result),
    )

    for label, body in invalid_score_bodies(classes).items():
        result = asyncio.run(adapter_case(json_body_handler(body)))
        check(
            f"adapter invalid scores dropped ({label})",
            result.status == "ok"
            and result.pred_class == "sports"
            and result.error_code is None
            and result.scores is None
            and result.score_kind is None
            and result.top_k is None,
            str(result),
        )

    # 无分数模型（LLM）：完全不带分数相关字段，仍需成功返回类别。
    result = asyncio.run(
        adapter_case(
            json_body_handler({"title": "x", "pred_class": "sports"})
        )
    )
    check(
        "adapter no-score worker keeps pred_class only",
        result.status == "ok"
        and result.pred_class == "sports"
        and result.scores is None
        and result.score_kind is None
        and result.top_k is None,
        str(result),
    )

    # score_kind 需准确对应来源：raw_proba 也是合法枚举。
    result = asyncio.run(
        adapter_case(
            json_body_handler(scored_body(classes, score_kind="raw_proba"))
        )
    )
    check(
        "adapter accepts raw_proba score_kind",
        result.status == "ok" and result.score_kind == "raw_proba",
        str(result),
    )


def service_checks() -> None:
    clear_env()
    adapters: dict[str, ModelAdapter] = {
        "rf": FakeAdapter("rf", ok_result("rf")),
        "fasttext": FakeAdapter("fasttext", ok_result("fasttext")),
        "bert": FakeAdapter("bert", ok_result("bert")),
        "llm": FakeAdapter("llm", ok_result("llm")),
        "distill": FakeAdapter("distill", ok_result("distill")),
    }
    service = make_service(adapters)

    outcome = asyncio.run(service.classify("测试标题", "rf", "rid"))
    check(
        "service classify via protocol",
        outcome.pred_class == "sports" and outcome.model_id == "rf",
        str(outcome),
    )

    outcome = asyncio.run(service.compare("测试标题", ["rf", "bert"], "rid"))
    check(
        "service compare all ok",
        outcome.status == "ok" and outcome.ok == 2,
        str(outcome),
    )

    partial = dict(adapters)
    partial["bert"] = FakeAdapter("bert", error_result("bert"))
    service = make_service(partial)
    outcome = asyncio.run(service.compare("测试标题", ["rf", "bert"], "rid"))
    check(
        "service compare partial failure isolated",
        outcome.status == "partial"
        and outcome.items[0].status == "ok"
        and outcome.items[1].status == "error",
        str(outcome),
    )

    try:
        asyncio.run(service.classify("测试标题", "unknown", "rid"))
        check("service unknown model rejected", False)
    except ApiError as exc:
        check("service unknown model rejected", exc.code == "UNKNOWN_MODEL")

    try:
        asyncio.run(service.classify("", "rf", "rid"))
        check("service empty title rejected", False)
    except ApiError as exc:
        check("service empty title rejected", exc.code == "INVALID_TITLE")


def api_checks() -> None:
    clear_env()
    from serving.gateway.main import create_app

    app = create_app()
    with TestClient(app) as client:
        settings = app.state.settings
        fake: dict[str, ModelAdapter] = {
            model_id: FakeAdapter(model_id, ok_result(model_id))
            for model_id in settings.models
        }
        fake["bert"] = FakeAdapter("bert", error_result("bert", "WORKER_TIMEOUT"))
        fake["rf"] = FakeAdapter(
            "rf", scored_adapter_result("rf", list(settings.classes))
        )
        app.state.classifier = ClassificationService(
            registry=app.state.registry,
            adapters=fake,
            classes=settings.classes,
            max_title_chars=settings.max_title_chars,
            stats=app.state.stats,
            logger=logging.getLogger("verify-refactor-api"),
        )

        response = client.get("/api/v1/healthz")
        check(
            "api healthz",
            response.status_code == 200
            and response.json()["status"] == "ok"
            and response.headers.get("X-Request-ID") == response.json()["request_id"],
        )

        response = client.get("/api/v1/models")
        body = response.json()
        check(
            "api models contract",
            response.status_code == 200
            and len(body["models"]) == 5
            and body["default_model"] == "fasttext"
            and len(body["classes"]) == 10,
            str(body)[:200],
        )

        response = client.get("/api/v1/readyz")
        check(
            "api readyz aggregates workers",
            response.status_code == 200
            and response.json()["status"] in {"ok", "degraded"}
            and len(response.json()["workers"]) == 5,
        )

        response = client.get("/api/v1/stats")
        check("api stats", response.status_code == 200 and response.json()["process_local"] is True)

        response = client.post("/api/v1/classify", json={"title": "测试", "model": "rf"})
        body = response.json()
        check(
            "api classify",
            response.status_code == 200
            and body["pred_class"] == "sports"
            and body["model_id"] == "rf"
            and isinstance(body["latency_ms"], int),
            str(body),
        )

        expected_scores = {class_id: 0.01 for class_id in settings.classes}
        expected_scores["sports"] = 0.91
        expected_top = [
            {"class_id": class_id, "score": score}
            for class_id, score in sorted(
                expected_scores.items(), key=lambda item: (-item[1], item[0])
            )[:3]
        ]
        response = client.post(
            "/api/v1/classify",
            json={"title": "北京二手房成交量创年内新高", "model": "rf"},
        )
        body = response.json()
        check(
            "api classify scores/top_k contract",
            response.status_code == 200
            and body["pred_class"] == "sports"
            and body["scores"] == expected_scores
            and body["score_kind"] == "raw_softmax"
            and body["top_k"] == expected_top,
            str(body),
        )

        signature = None
        stable = True
        for _ in range(5):
            repeated = client.post(
                "/api/v1/classify",
                json={"title": "北京二手房成交量创年内新高", "model": "rf"},
            )
            if repeated.status_code != 200:
                stable = False
                break
            payload = repeated.json()
            current = (
                payload.get("score_kind"),
                payload.get("scores"),
                payload.get("top_k"),
                payload.get("pred_class"),
            )
            if signature is None:
                signature = current
            elif current != signature:
                stable = False
        check(
            "api classify score_kind stable over 5 requests",
            stable and signature is not None and signature[0] == "raw_softmax",
            str(signature),
        )

        response = client.post(
            "/api/v1/classify", json={"title": "测试", "model": "llm"}
        )
        body = response.json()
        check(
            "api classify no-score model has null score fields",
            response.status_code == 200
            and body["pred_class"] == "sports"
            and body["scores"] is None
            and body["score_kind"] is None
            and body["top_k"] is None,
            str(body),
        )

        for label, invalid_body in invalid_score_bodies(
            list(settings.classes)
        ).items():
            degraded = asyncio.run(adapter_case(json_body_handler(invalid_body)))
            adapters: dict[str, ModelAdapter] = {
                model_id: FakeAdapter(model_id, ok_result(model_id))
                for model_id in settings.models
            }
            adapters["rf"] = FakeAdapter("rf", degraded)
            app.state.classifier = ClassificationService(
                registry=app.state.registry,
                adapters=adapters,
                classes=settings.classes,
                max_title_chars=settings.max_title_chars,
                stats=app.state.stats,
                logger=logging.getLogger("verify-refactor-api-invalid-scores"),
            )
            response = client.post(
                "/api/v1/classify", json={"title": "测试", "model": "rf"}
            )
            body = response.json()
            check(
                f"api invalid scores degrade without 500 ({label})",
                response.status_code == 200
                and body["pred_class"] == "sports"
                and body["scores"] is None
                and body["score_kind"] is None
                and body["top_k"] is None
                and "error" not in body,
                f"{response.status_code} {body}",
            )

        # 恢复带“bert 失败 + rf 有分数”的分类器，供后续 compare 断言使用。
        app.state.classifier = ClassificationService(
            registry=app.state.registry,
            adapters=fake,
            classes=settings.classes,
            max_title_chars=settings.max_title_chars,
            stats=app.state.stats,
            logger=logging.getLogger("verify-refactor-api"),
        )

        response = client.post("/api/v1/compare", json={"title": "测试", "models": ["rf", "bert"]})
        body = response.json()
        check(
            "api compare partial",
            response.status_code == 200
            and body["summary"] == {"total": 2, "ok": 1, "failed": 1, "status": "partial"}
            and "pred_class" not in body["results"][1],
            str(body),
        )

        response = client.post("/api/v1/classify", json={"title": "测试", "model": "nope"})
        check(
            "api unknown model",
            response.status_code == 404
            and response.json()["error"]["code"] == "UNKNOWN_MODEL",
        )

        response = client.post("/api/v1/classify", json={"title": "x" * 201})
        check(
            "api title too long",
            response.status_code == 400
            and response.json()["error"]["code"] == "TITLE_TOO_LONG",
        )

        response = client.post("/api/v1/classify", json={"model": "rf"})
        check(
            "api missing title 422",
            response.status_code == 422
            and response.json()["error"]["code"] == "INVALID_REQUEST",
        )

        response = client.get("/api/v1/healthz", headers={"X-Request-ID": "bad id!"})
        check(
            "api invalid request id",
            response.status_code == 400
            and response.json()["error"]["code"] == "INVALID_REQUEST_ID",
        )

        response = client.options(
            "/api/v1/models",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        check(
            "api cors allows configured origin",
            response.status_code == 200
            and response.headers.get("access-control-allow-origin")
            == "http://127.0.0.1:5173",
            f"{response.status_code} {dict(response.headers)}",
        )

        response = client.get("/api/v1/models", headers={"Origin": "http://evil.example"})
        check(
            "api cors rejects unknown origin",
            response.headers.get("access-control-allow-origin") is None,
        )

    # 非法 CORS 环境变量应在应用创建阶段失败（不启动服务）
    set_env(TMF_CORS_ORIGINS="*")
    try:
        create_app()
        check("app creation rejects wildcard cors", False)
    except ConfigError:
        check("app creation rejects wildcard cors", True)
    clear_env()


# ---------------------------------------------------------------------------
# B10：新增接口契约（fake adapter + 临时 TMF_DB_PATH；不连接真实 worker/LLM）
# ---------------------------------------------------------------------------
DEMO_TITLE_B10 = "北京二手房成交量创年内新高"

SSE_MODEL_KEYS = {
    "request_id",
    "model_id",
    "status",
    "latency_ms",
    "model_version",
    "pred_class",
    "data_source",
    "error",
}
SSE_SUMMARY_KEYS = {
    "request_id",
    "status",
    "total",
    "ok",
    "failed",
    "elapsed_ms",
    "total_timeout_ms",
    "partial",
}
SSE_DONE_KEYS = {"request_id", "status", "total", "ok", "failed"}

THRESHOLD_META_REQUIRED = {
    "source",
    "calibrated",
    "method",
    "max_score_threshold",
    "entropy_threshold",
    "scope_note",
    "sample_size",
}


def parse_sse(body: str) -> tuple[list[tuple[str, dict]], list[str], list[str]]:
    """最小 SSE 解析：返回 (event 列表, 心跳注释行, 非法帧)。"""
    events: list[tuple[str, dict]] = []
    heartbeats: list[str] = []
    malformed: list[str] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        name: str | None = None
        data: dict | None = None
        bad = False
        comment = False
        for line in block.split("\n"):
            if not line:
                continue
            if line.startswith(":"):
                comment = True
                heartbeats.append(line)
                continue
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                try:
                    data = json.loads(line[len("data: ") :])
                except json.JSONDecodeError:
                    bad = True
            else:
                bad = True
        if comment and name is None:
            continue
        if name is None or bad or data is None:
            malformed.append(block)
            continue
        events.append((name, data))
    return events, heartbeats, malformed


def _scored_adapter_result(
    model_id: str,
    classes: list[str],
    values: list[float],
    pred_class: str,
    *,
    data_source: str = DATA_SOURCE_LIVE,
) -> AdapterResult:
    """构造已通过适配器校验的带分数结果（用于 B1/B3 API 契约断言）。"""
    entries = tuple(
        ScoreEntry(class_id=class_id, score=float(value))
        for class_id, value in zip(classes, values)
    )
    top = tuple(
        sorted(entries, key=lambda entry: (-entry.score, entry.class_id))[:3]
    )
    return AdapterResult(
        model_id=model_id,
        status="ok",
        latency_ms=9,
        pred_class=pred_class,
        scores=entries,
        score_kind="raw_softmax",
        top_k=top,
        data_source=data_source,
    )


def _normal_scores(classes: list[str]) -> list[float]:
    return [0.91 if class_id == "sports" else 0.01 for class_id in classes]


def _low_max_scores(classes: list[str]) -> list[float]:
    """全部类别同分且低于 max_score_threshold -> low_max_score。"""
    return [0.1 for _ in classes]


def _high_entropy_scores(classes: list[str]) -> list[float]:
    """max_score >= 阈值但熵超过阈值 -> high_entropy（两类主导 + 长尾）。"""
    values = [0.55, 0.4]
    rest = max(len(classes) - 2, 1)
    values.extend([0.05 / rest for _ in range(rest)])
    return values[: len(classes)]


def _install_classifier(
    app, adapters: dict[str, ModelAdapter], *, with_reject: bool = False
) -> ClassificationService:
    settings = app.state.settings
    service = ClassificationService(
        registry=app.state.registry,
        adapters=adapters,
        classes=settings.classes,
        max_title_chars=settings.max_title_chars,
        stats=app.state.stats,
        logger=logging.getLogger("verify-refactor-b10"),
        reject=settings.reject if with_reject else None,
    )
    app.state.classifier = service
    return service


def b3_b11_api_checks() -> None:
    """B1/B3/B11 + B6 SSE：字段契约（fake adapter，临时 TMF_DB_PATH）。"""
    print("\n=== 6. B3 软拒识 / B11 data_source / B6 SSE 契约 ===")
    clear_env()
    from serving.gateway.main import create_app

    app = create_app()
    with TestClient(app) as client:
        settings = app.state.settings
        classes = list(settings.classes)

        # -- B3：软拒识（保留 pred_class/分数，阈值元数据来自 models.yaml） --
        reject_adapters: dict[str, ModelAdapter] = {
            model_id: FakeAdapter(model_id, ok_result(model_id))
            for model_id in settings.models
        }
        reject_adapters["rf"] = FakeAdapter(
            "rf",
            _scored_adapter_result(
                "rf", classes, _low_max_scores(classes), classes[0]
            ),
        )
        reject_adapters["fasttext"] = FakeAdapter(
            "fasttext",
            _scored_adapter_result(
                "fasttext", classes, _high_entropy_scores(classes), classes[0]
            ),
        )
        reject_adapters["bert"] = FakeAdapter(
            "bert",
            _scored_adapter_result(
                "bert", classes, _normal_scores(classes), "sports"
            ),
        )
        _install_classifier(app, reject_adapters, with_reject=True)

        low = client.post("/api/v1/classify", json={"title": "测试", "model": "rf"})
        low_body = low.json()
        low_meta = low_body.get("threshold_meta") or {}
        check(
            "B3 low_max_score: rejected=true, reason kept, pred_class/scores preserved",
            low.status_code == 200
            and low_body.get("rejected") is True
            and low_body.get("reject_reason") == "low_max_score"
            and low_body.get("pred_class") == classes[0]
            and low_body.get("scores") is not None
            and len(low_body.get("top_k") or []) == 3
            and low_body.get("score_kind") == "raw_softmax",
            str(low_body)[:300],
        )
        check(
            "B3 threshold_meta contract (7 fixed keys + applied model, uncalibrated)",
            THRESHOLD_META_REQUIRED <= set(low_meta)
            and low_meta.get("applied_model_id") == "rf"
            and low_meta.get("calibrated") is False
            and low_meta.get("source") == "synthetic_negatives"
            and low_meta.get("method") == "max_score+entropy"
            and low_meta.get("max_score_threshold")
            == settings.reject.max_score_threshold
            and low_meta.get("entropy_threshold")
            == settings.reject.entropy_threshold
            and isinstance(low_meta.get("scope_note"), str)
            and bool(low_meta.get("scope_note").strip())
            and int(low_meta.get("sample_size") or 0) > 0,
            str(low_meta)[:300],
        )
        high = client.post(
            "/api/v1/classify", json={"title": "测试", "model": "fasttext"}
        ).json()
        high_meta = high.get("threshold_meta") or {}
        check(
            "B3 high_entropy branch (max>=threshold, entropy>threshold)",
            high.get("rejected") is True
            and high.get("reject_reason") == "high_entropy"
            and high.get("pred_class") == classes[0]
            and float(high_meta.get("observed_max_score") or 0)
            >= settings.reject.max_score_threshold
            and float(high_meta.get("observed_entropy") or 0)
            > settings.reject.entropy_threshold,
            str(high)[:300],
        )
        normal = client.post(
            "/api/v1/classify", json={"title": "测试", "model": "bert"}
        ).json()
        check(
            "B3 not rejected but has scores -> threshold_meta still present",
            normal.get("rejected") is False
            and normal.get("reject_reason") is None
            and normal.get("pred_class") == "sports"
            and isinstance(normal.get("threshold_meta"), dict)
            and THRESHOLD_META_REQUIRED <= set(normal.get("threshold_meta") or {}),
            str(normal)[:300],
        )
        no_score = client.post(
            "/api/v1/classify", json={"title": "测试", "model": "llm"}
        ).json()
        check(
            "B3 no-score model: rejected=false, no reject_reason/threshold_meta",
            no_score.get("rejected") is False
            and no_score.get("reject_reason") is None
            and no_score.get("threshold_meta") is None
            and no_score.get("scores") is None,
            str(no_score)[:300],
        )
        # B11 统计断言的基线：上面 B3 段用的是 live 分数，会计入 model_calls；
        # data_source 段改用 mock / None / unknown，必须不增加计数。
        baseline_calls = client.get("/api/v1/stats").json().get("model_calls") or {}

        # -- B11：data_source（live / mock / unknown / null 不伪造） --
        ds_adapters: dict[str, ModelAdapter] = {
            "rf": FakeAdapter(
                "rf",
                AdapterResult(
                    model_id="rf", status="ok", latency_ms=5,
                    pred_class="sports", data_source=DATA_SOURCE_LIVE,
                ),
            ),
            "fasttext": FakeAdapter(
                "fasttext",
                AdapterResult(
                    model_id="fasttext", status="ok", latency_ms=5,
                    pred_class="sports", data_source=DATA_SOURCE_MOCK,
                ),
            ),
            "bert": FakeAdapter(
                "bert",
                AdapterResult(
                    model_id="bert", status="ok", latency_ms=5,
                    pred_class="sports", data_source=None,
                ),
            ),
            "llm": FakeAdapter(
                "llm",
                AdapterResult(
                    model_id="llm", status="ok", latency_ms=5,
                    pred_class="sports", data_source=DATA_SOURCE_UNKNOWN,
                ),
            ),
            "distill": FakeAdapter("distill", ok_result("distill")),
        }
        _install_classifier(app, ds_adapters)

        classify_sources: dict[str, object] = {}
        for model_id, expected in (
            ("rf", DATA_SOURCE_LIVE),
            ("fasttext", DATA_SOURCE_MOCK),
            ("bert", None),
            ("llm", DATA_SOURCE_UNKNOWN),
        ):
            body = client.post(
                "/api/v1/classify", json={"title": "测试", "model": model_id}
            ).json()
            classify_sources[model_id] = body.get("data_source")
            check(
                f"B11 /classify data_source for {model_id} == {expected}",
                body.get("data_source") == expected,
                str(body.get("data_source")),
            )
        check(
            "B11 data_source never inferred as live when adapter is null",
            classify_sources.get("bert") is None,
            str(classify_sources),
        )

        compare = client.post(
            "/api/v1/compare",
            json={"title": "测试", "models": ["rf", "fasttext", "bert", "llm"]},
        )
        compare_body = compare.json()
        by_id = {item.get("model_id"): item for item in compare_body.get("results", [])}
        check(
            "B11 /compare carries data_source on every item",
            compare.status_code == 200
            and by_id["rf"].get("data_source") == DATA_SOURCE_LIVE
            and by_id["fasttext"].get("data_source") == DATA_SOURCE_MOCK
            and "data_source" in by_id["bert"]
            and by_id["bert"].get("data_source") is None
            and by_id["llm"].get("data_source") == DATA_SOURCE_UNKNOWN,
            str(compare_body)[:400],
        )

        error_adapter = FakeAdapter(
            "llm",
            AdapterResult(
                model_id="llm",
                status="error",
                latency_ms=3,
                error_code="WORKER_TIMEOUT",
                error_message="所选模型未在配置的时间内返回",
                data_source=DATA_SOURCE_MOCK,
            ),
        )
        error_adapters = dict(ds_adapters)
        error_adapters["llm"] = error_adapter
        _install_classifier(app, error_adapters)
        error_compare = client.post(
            "/api/v1/compare", json={"title": "测试", "models": ["rf", "llm"]}
        ).json()
        llm_item = {
            item.get("model_id"): item
            for item in error_compare.get("results", [])
        }.get("llm", {})
        check(
            "B11 error item also carries data_source (mock) and no pred_class",
            llm_item.get("status") == "error"
            and llm_item.get("data_source") == DATA_SOURCE_MOCK
            and llm_item.get("error", {}).get("code") == "WORKER_TIMEOUT"
            and "pred_class" not in llm_item,
            str(llm_item),
        )

        # B11：mock/unknown 不计入 /stats 的真实 model_calls（只有 live 计数）。
        current_calls = client.get("/api/v1/stats").json().get("model_calls") or {}

        def _ok(calls: dict, model_id: str) -> int:
            return int((calls or {}).get(model_id, {}).get("ok", 0))

        check(
            "B11 /stats model_calls only counts live (mock/None/unknown excluded)",
            _ok(current_calls, "rf") - _ok(baseline_calls, "rf") >= 1
            and _ok(current_calls, "fasttext") == _ok(baseline_calls, "fasttext")
            and _ok(current_calls, "bert") == _ok(baseline_calls, "bert")
            and "llm" not in current_calls,
            f"baseline={baseline_calls} current={current_calls}",
        )

        # -- B6：SSE 事件契约（model / summary / done + data_source） --
        sse_adapters = {
            "rf": FakeAdapter(
                "rf",
                AdapterResult(
                    model_id="rf", status="ok", latency_ms=5,
                    pred_class="sports", data_source=DATA_SOURCE_LIVE,
                ),
            ),
            "fasttext": FakeAdapter(
                "fasttext",
                AdapterResult(
                    model_id="fasttext", status="ok", latency_ms=6,
                    pred_class="finance", data_source=DATA_SOURCE_MOCK,
                ),
            ),
            "llm": FakeAdapter(
                "llm",
                AdapterResult(
                    model_id="llm", status="error", latency_ms=3,
                    error_code="WORKER_TIMEOUT",
                    error_message="所选模型未在配置的时间内返回",
                    data_source=DATA_SOURCE_MOCK,
                ),
            ),
        }
        _install_classifier(app, sse_adapters)
        sse = client.get(
            "/api/v1/compare/stream",
            params={"title": DEMO_TITLE_B10, "models": "rf,fasttext,llm"},
        )
        events, heartbeats, malformed = parse_sse(sse.text)
        names = [name for name, _ in events]
        model_events = [data for name, data in events if name == "model"]
        summary_events = [data for name, data in events if name == "summary"]
        done_events = [data for name, data in events if name == "done"]
        headers = {key.lower(): value for key, value in sse.headers.items()}
        check(
            "B6 SSE headers contract",
            sse.status_code == 200
            and headers.get("content-type", "").startswith("text/event-stream")
            and headers.get("cache-control") == "no-cache"
            and headers.get("x-accel-buffering") == "no",
            str(headers)[:300],
        )
        check(
            "B6 SSE event sequence: 3 model -> summary -> done, no error frames",
            names == ["model", "model", "model", "summary", "done"]
            and not malformed
            and not heartbeats,
            str(names),
        )
        check(
            "B6 SSE model events carry data_source and exact field shape",
            len(model_events) == 3
            and all(set(event) == SSE_MODEL_KEYS for event in model_events)
            and {
                event["model_id"]: event["data_source"] for event in model_events
            }
            == {
                "rf": DATA_SOURCE_LIVE,
                "fasttext": DATA_SOURCE_MOCK,
                "llm": DATA_SOURCE_MOCK,
            }
            and next(
                event for event in model_events if event["model_id"] == "llm"
            ).get("error", {}).get("code")
            == "WORKER_TIMEOUT",
            str(model_events)[:400],
        )
        check(
            "B6 SSE summary/done contract",
            len(summary_events) == 1
            and len(done_events) == 1
            and set(summary_events[0]) == SSE_SUMMARY_KEYS
            and summary_events[0].get("status") == "partial"
            and summary_events[0].get("total") == 3
            and summary_events[0].get("ok") == 2
            and summary_events[0].get("failed") == 1
            and summary_events[0].get("total_timeout_ms") == 18000
            and set(done_events[0]) == SSE_DONE_KEYS
            and done_events[0].get("status") == "partial",
            f"{summary_events} {done_events}",
        )


def arena_checks() -> None:
    """B7：/arena/next|vote|leaderboard 契约（fake adapter + 临时题库）。"""
    print("\n=== 7. B7 竞技场 next/vote/leaderboard 契约 ===")
    clear_env()
    saved_test_file = os.environ.pop("TMF_ARENA_TEST_FILE", None)
    with tempfile.TemporaryDirectory() as temp_dir:
        test_file = Path(temp_dir) / "arena_test.txt"
        test_file.write_text(
            "北京二手房成交量创年内新高\t1\n"
            "国足世预赛客场取胜\t7\n"
            "A股三大指数集体收涨\t2\n",
            encoding="utf-8",
        )
        os.environ["TMF_ARENA_TEST_FILE"] = str(test_file)
        try:
            from serving.gateway.main import create_app

            app = create_app()
            with TestClient(app) as client:
                settings = app.state.settings
                registry = app.state.registry
                model_ids = registry.all_model_ids()
                fake = {
                    model_id: FakeAdapter(
                        model_id,
                        AdapterResult(
                            model_id=model_id,
                            status="ok",
                            latency_ms=7,
                            pred_class="sports",
                            data_source=DATA_SOURCE_LIVE,
                        ),
                    )
                    for model_id in model_ids
                }
                old_arena = app.state.arena
                old_arena.close()
                app.state.arena = ArenaService(
                    registry=registry,
                    adapters=fake,
                    classes=settings.classes,
                    logger=logging.getLogger("verify-refactor-arena"),
                    test_file=test_file,
                    rng=random.Random(20261022),
                )

                next_resp = client.get("/api/v1/arena/next")
                next_body = next_resp.json()
                candidates = next_body.get("candidates") or []
                check(
                    "B7 arena/next contract (item + 5 anonymous candidates)",
                    next_resp.status_code == 200
                    and isinstance(next_body.get("item_id"), str)
                    and next_body.get("title")
                    in {
                        "北京二手房成交量创年内新高",
                        "国足世预赛客场取胜",
                        "A股三大指数集体收涨",
                    }
                    and isinstance(next_body.get("request_id"), str)
                    and len(candidates) == 5
                    and next_body.get("partial") is False
                    and all(
                        set(candidate) == {"anon_id", "pred_class", "latency_ms"}
                        for candidate in candidates
                    )
                    and len({candidate["anon_id"] for candidate in candidates}) == 5
                    and all(candidate["pred_class"] in settings.classes for candidate in candidates),
                    str(next_body)[:400],
                )
                check(
                    "B7 arena/next anonymity: no model_id/display_name/version in payload",
                    all(model_id not in next_resp.text for model_id in model_ids)
                    and all(
                        registry.get(model_id).display_name not in next_resp.text
                        for model_id in model_ids
                    ),
                    next_resp.text[:300],
                )

                client_id = "verify-b10-arena-client"
                vote_payload = {
                    "client_id": client_id,
                    "item_id": next_body["item_id"],
                    "anon_id": candidates[0]["anon_id"],
                }
                vote_resp = client.post("/api/v1/arena/vote", json=vote_payload)
                vote_body = vote_resp.json()
                selected = vote_body.get("selected") or {}
                check(
                    "B7 arena/vote reveals only selected model + Elo update",
                    vote_resp.status_code == 200
                    and set(selected) == {"model_id", "display_name", "pred_class"}
                    and selected.get("model_id") in model_ids
                    and selected.get("pred_class") in settings.classes
                    and selected.get("display_name")
                    == registry.get(selected["model_id"]).display_name
                    and isinstance(vote_body.get("elo_after"), (int, float))
                    and float(vote_body["elo_after"]) > 1200.0
                    and vote_body.get("games_applied") == 4
                    and "anon_id" not in vote_body,
                    str(vote_body)[:300],
                )
                duplicate = client.post("/api/v1/arena/vote", json=vote_payload)
                check(
                    "B7 arena duplicate vote -> 409",
                    duplicate.status_code == 409
                    and duplicate.json()["error"]["code"]
                    == ERROR_ARENA_DUPLICATE_VOTE,
                    f"{duplicate.status_code} {duplicate.text[:200]}",
                )

                leaderboard = client.get("/api/v1/arena/leaderboard")
                board = leaderboard.json()
                entries = {entry.get("model_id"): entry for entry in board.get("entries", [])}
                winner_id = selected["model_id"]
                check(
                    "B7 arena/leaderboard contract (warming_up, vote data_kind, 5 entries)",
                    leaderboard.status_code == 200
                    and board.get("status") == "warming_up"
                    and board.get("data_kind") == "vote"
                    and isinstance(board.get("note"), str)
                    and bool(board.get("note"))
                    and board.get("min_votes_for_ranking") == 50
                    and len(entries) == 5
                    and all(
                        set(entry)
                        == {
                            "model_id",
                            "display_name",
                            "elo",
                            "votes",
                            "wins",
                            "losses",
                            "win_rate",
                            "wilson_lower",
                            "wilson_upper",
                            "warming_up",
                            "rank",
                        }
                        for entry in entries.values()
                    ),
                    str(board)[:400],
                )
                check(
                    "B7 arena/leaderboard after one vote: winner 1/1, others 1/0, rank null",
                    entries.get(winner_id, {}).get("votes") == 1
                    and entries.get(winner_id, {}).get("wins") == 1
                    and entries.get(winner_id, {}).get("losses") == 0
                    and entries.get(winner_id, {}).get("win_rate") == 1.0
                    and all(
                        entry.get("votes") == 1
                        and entry.get("warming_up") is True
                        and entry.get("rank") is None
                        and 0.0 <= float(entry.get("wilson_lower", -1)) <= 1.0
                        and 0.0 <= float(entry.get("wilson_upper", -1)) <= 1.0
                        and float(entry.get("wilson_lower", 1))
                        <= float(entry.get("wilson_upper", 0))
                        for entry in entries.values()
                    )
                    and all(
                        entry.get("losses") == 1
                        for model_id, entry in entries.items()
                        if model_id != winner_id
                    ),
                    str(entries)[:400],
                )
        finally:
            os.environ.pop("TMF_ARENA_TEST_FILE", None)
            if saved_test_file is not None:
                os.environ["TMF_ARENA_TEST_FILE"] = saved_test_file


def feedback_checks() -> None:
    """B4：/feedback 与 /feedback/stats 契约（临时 TMF_DB_PATH，不保留原文）。"""
    print("\n=== 8. B4 反馈 submit/stats/hint/重复/清理契约 ===")
    clear_env()
    from serving.gateway.main import create_app

    app = create_app()
    with TestClient(app) as client:
        settings = app.state.settings
        feedback = getattr(app.state, "feedback", None)
        check("B4 feedback service assembled", feedback is not None)
        _install_classifier(
            app,
            {
                model_id: FakeAdapter(model_id, ok_result(model_id))
                for model_id in settings.models
            },
        )
        if feedback is None:
            return

        db_path = Path(feedback.db_path).resolve()
        expected_db = Path(os.environ["TMF_DB_PATH"]).resolve()
        check(
            "B4 feedback uses temp TMF_DB_PATH (default DB untouched)",
            db_path == expected_db and db_path.is_file(),
            f"{db_path} expected={expected_db}",
        )

        payload = {
            "client_id": "verify-b10-client",
            "title": DEMO_TITLE_B10,
            "model_id": "rf",
            "pred_class": "sports",
            "corrected_class": "finance",
            "score": 0.9,
            "source": "verify",
        }
        submit = client.post("/api/v1/feedback", json=payload)
        submit_body = submit.json()
        message = submit_body.get("message") or ""
        check(
            "B4 feedback submit: pending only, no 'learned/retrained' wording",
            submit.status_code == 200
            and isinstance(submit_body.get("feedback_id"), int)
            and submit_body["feedback_id"] > 0
            and submit_body.get("status") == "pending"
            and submit_body.get("total", 0) >= 1
            and message == "反馈已提交，待审核"
            and not any(
                forbidden in message
                for forbidden in ("已学会", "已重新训练", "已更新", "已训练")
            ),
            str(submit_body)[:300],
        )
        duplicate = client.post("/api/v1/feedback", json=payload)
        check(
            "B4 duplicate feedback -> 409",
            duplicate.status_code == 409
            and duplicate.json()["error"]["code"] == ERROR_FEEDBACK_DUPLICATE,
            f"{duplicate.status_code} {duplicate.text[:200]}",
        )

        stats = client.get("/api/v1/feedback/stats").json()
        check(
            "B4 feedback stats aggregates (no raw export fields)",
            stats.get("total", 0) >= 1
            and stats.get("pending", 0) >= 1
            and int(stats.get("by_class", {}).get("finance", 0)) >= 1
            and int(stats.get("by_model", {}).get("rf", 0)) >= 1
            and stats.get("retention_days") == 30
            and isinstance(stats.get("similar_hits"), int)
            and not ({"titles", "rows", "items", "raw"} & set(stats)),
            str(stats)[:300],
        )

        hint_body = client.post(
            "/api/v1/classify", json={"title": DEMO_TITLE_B10, "model": "rf"}
        ).json()
        check(
            "B4 classify hint: similar pending feedback shown, pred_class unchanged",
            hint_body.get("human_feedback_hint")
            == {"corrected_class": "finance", "status": "pending"}
            and hint_body.get("pred_class") == "sports",
            str(hint_body)[:300],
        )
        unrelated = client.post(
            "/api/v1/classify",
            json={"title": "秋季流感疫苗开始接种", "model": "rf"},
        ).json()
        check(
            "B4 no similar feedback -> hint is null",
            unrelated.get("human_feedback_hint") is None,
            str(unrelated.get("human_feedback_hint")),
        )

        removed = asyncio.run(
            feedback.cleanup_expired(now_epoch=time.time() + 31 * 86400)
        )
        after = client.get("/api/v1/feedback/stats").json()
        check(
            "B4 retention cleanup removes expired feedback (30 days)",
            removed >= 1 and after.get("total") == 0 and after.get("pending") == 0,
            f"removed={removed} stats={after}",
        )


def bench_checks() -> None:
    """B8：/bench/pk 只读产物契约（有产物断言结构，无产物断言 404）。"""
    print("\n=== 9. B8 /bench/pk 只读产物契约 ===")
    clear_env()
    saved_bench_dir = os.environ.pop("TMF_BENCH_DIR", None)
    try:
        from serving.gateway.main import create_app

        app = create_app()
        with TestClient(app) as client:
            response = client.get("/api/v1/bench/pk")
            if response.status_code == 200:
                body = response.json()
                artifact = body.get("artifact") or {}
                meta = artifact.get("meta") or {}
                check(
                    "B8 bench/pk returns latest artifact (relative path, no absolute path)",
                    isinstance(body.get("request_id"), str)
                    and str(body.get("artifact_path", "")).startswith("artifacts/bench/pk_")
                    and str(body.get("artifact_path", "")).endswith(".json")
                    and ":" not in str(body.get("artifact_path", "")),
                    str(body.get("artifact_path")),
                )
                check(
                    "B8 bench artifact structure (meta/models/derived/missing_models)",
                    {"meta", "dataset", "models", "derived", "missing_models"}
                    <= set(artifact)
                    and {
                        "dataset",
                        "split",
                        "sample_size",
                        "generated_at",
                        "code_version",
                        "model_versions",
                        "metric_def",
                        "data_kind",
                    }
                    <= set(meta)
                    and isinstance(meta.get("sample_size"), int)
                    and meta["sample_size"] > 0
                    and isinstance(artifact.get("models"), list)
                    and bool(artifact.get("models"))
                    and isinstance(artifact.get("missing_models"), list),
                    str(meta)[:300],
                )
            else:
                check(
                    "B8 bench/pk missing artifact -> 404 BENCH_ARTIFACT_NOT_FOUND",
                    response.status_code == 404
                    and response.json()["error"]["code"] == ERROR_BENCH_NOT_FOUND,
                    f"{response.status_code} {response.text[:200]}",
                )
    finally:
        os.environ.pop("TMF_BENCH_DIR", None)
        if saved_bench_dir is not None:
            os.environ["TMF_BENCH_DIR"] = saved_bench_dir


def static_ip_check() -> None:
    suffixes = {".py", ".ps1", ".bat", ".yaml", ".yml", ".ts", ".json", ".example"}
    # 生成物（日志/运行状态/dist/依赖）和本地未入库覆盖文件不属于"源码固定 IP"检查范围。
    excluded_parts = {"node_modules", "__pycache__", "dist", "logs", "run", ".git"}
    excluded_names = {".env.local"}
    hits: list[str] = []
    for path in PROJECT_ROOT.rglob("*"):
        if not path.is_file() or path.suffix not in suffixes:
            continue
        if path.name in excluded_names:
            continue
        if any(part in path.parts for part in excluded_parts):
            continue
        if path.name == Path(__file__).name:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if "192.168.12.54" in text:
            hits.append(str(path.relative_to(PROJECT_ROOT)))
    check("no hardcoded LAN IP in runtime files", not hits, ", ".join(hits))


def main() -> int:
    clear_env()
    config_checks()
    adapter_checks()
    score_adapter_checks()
    service_checks()

    # B10：所有 API/DB 断言统一使用临时 TMF_DB_PATH，绝不写 serving/run/tmf.db。
    # 显式保存外部值以便恢复；脚本运行结束后临时库随目录删除。
    saved_db_path = os.environ.pop("TMF_DB_PATH", None)
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_db = Path(temp_dir) / "verify_refactor_gateway.db"
        os.environ["TMF_DB_PATH"] = str(temp_db)
        try:
            api_checks()
            b3_b11_api_checks()
            arena_checks()
            feedback_checks()
            bench_checks()
        finally:
            os.environ.pop("TMF_DB_PATH", None)
    check(
        "B10 temp TMF_DB_PATH removed after run (no residual db)",
        not temp_db.exists(),
        str(temp_db),
    )
    if saved_db_path is not None:
        os.environ["TMF_DB_PATH"] = saved_db_path

    static_ip_check()
    clear_env()
    if FAILURES:
        print(f"RESULT FAIL ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
