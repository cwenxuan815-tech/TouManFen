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
5. 运行时代码/配置中不再存在固定局域网 IP。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from serving.adapters import (  # noqa: E402
    AdapterResult,
    HttpWorkerAdapter,
    LlmAdapter,
    ModelAdapter,
    PredictionResult,
)
from serving.gateway.errors import ApiError, ConfigError  # noqa: E402
from serving.gateway.settings import load_settings  # noqa: E402
from serving.gateway.stats import StatsCollector  # noqa: E402
from serving.models.registry import ModelRegistry  # noqa: E402
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
    service_checks()
    api_checks()
    static_ip_check()
    clear_env()
    if FAILURES:
        print(f"RESULT FAIL ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
