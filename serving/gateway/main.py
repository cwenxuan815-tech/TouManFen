"""网关 FastAPI 应用（阶段 3）。

范围：
- 阶段 2 只读：/healthz、/readyz、/models、/stats；
- 阶段 3 分类：/classify、/compare；
- 网关只通过 HTTP 调用 Flask worker，不导入模型/torch/transformers/训练代码；
- request ID / 结构化日志 / 基础统计。
"""

from __future__ import annotations

import os
import re
import time
import uuid
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .errors import (
    ApiError,
    ConfigError,
    ERROR_INTERNAL,
    ERROR_INVALID_REQUEST,
    ERROR_INVALID_REQUEST_ID,
    ERROR_METHOD_NOT_ALLOWED,
    ERROR_NOT_FOUND,
    error_payload,
)
from .logging_utils import log_startup_error, setup_logging
from ..models.http_adapter import WorkerHttpAdapter
from ..models.registry import ModelRegistry
from .probes import WorkerProber
from .routers import classify as classify_router
from .routers import health as health_router
from .routers import models as models_router
from .routers import stats as stats_router
from ..services.classification import ClassificationService
from .settings import SERVING_DIR, load_settings
from .stats import StatsCollector

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
GATEWAY_VERSION = "stage3"
# 仅局域网前端开发来源（Vite dev server）；可通过 TMF_CORS_ORIGINS 追加（逗号分隔，不允许 *）。
DEFAULT_CORS_ORIGINS = ("http://192.168.12.54:5173",)


def _cors_origins() -> list[str]:
    extras: list[str] = []
    raw = os.environ.get("TMF_CORS_ORIGINS", "")
    for item in raw.split(","):
        origin = item.strip()
        if not origin:
            continue
        if origin == "*" or not origin.startswith(("http://", "https://")):
            raise ConfigError(
                "TMF_CORS_ORIGINS 必须是明确的 http(s) 来源，且不允许使用 *"
            )
        extras.append(origin)
    return list(dict.fromkeys([*DEFAULT_CORS_ORIGINS, *extras]))


def create_app() -> FastAPI:
    logger = setup_logging(SERVING_DIR / "logs" / "gateway.jsonl")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            settings = load_settings()
        except ConfigError as exc:
            log_startup_error(f"CONFIG_ERROR: {exc}")
            raise
        app.state.settings = settings
        app.state.stats = StatsCollector()
        app.state.prober = WorkerProber(settings)
        # 只调用配置的内网 worker；显式忽略系统代理，避免内网直连被代理拦截。
        app.state.http_client = httpx.AsyncClient(trust_env=False)
        registry = ModelRegistry(settings)
        adapter = WorkerHttpAdapter(settings.classes, app.state.http_client)
        app.state.registry = registry
        app.state.adapter = adapter
        app.state.classifier = ClassificationService(
            registry=registry,
            adapter=adapter,
            classes=settings.classes,
            max_title_chars=settings.max_title_chars,
            stats=app.state.stats,
            logger=logger,
        )
        logger.info("gateway_started", extra={"event": "startup"})
        try:
            yield
        finally:
            await app.state.prober.close()
            await app.state.http_client.aclose()
            logger.info("gateway_stopped", extra={"event": "shutdown"})

    app = FastAPI(
        title="TouManFen Gateway",
        version=GATEWAY_VERSION,
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        stats: StatsCollector = request.app.state.stats
        incoming_request_id = request.headers.get("X-Request-ID")

        if incoming_request_id is not None and not REQUEST_ID_PATTERN.match(
            incoming_request_id
        ):
            request_id = str(uuid.uuid4())
            payload = error_payload(
                request_id,
                ERROR_INVALID_REQUEST_ID,
                "X-Request-ID 必须匹配 ^[A-Za-z0-9._-]{1,64}$",
            )
            stats.record("unmatched", 400, 0.0)
            logger.info(
                "request_rejected",
                extra={
                    "event": "request",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "route": "unmatched",
                    "status": 400,
                    "duration_ms": 0.0,
                },
            )
            return JSONResponse(
                status_code=400,
                content=payload,
                headers={"X-Request-ID": request_id},
            )

        request_id = incoming_request_id or str(uuid.uuid4())
        request.state.request_id = request_id
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            logger.exception(
                "unhandled_exception",
                extra={
                    "event": "error",
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                },
            )
            stats.record("unmatched", 500, duration_ms)
            return JSONResponse(
                status_code=500,
                content=error_payload(request_id, ERROR_INTERNAL, "网关内部错误"),
                headers={"X-Request-ID": request_id},
            )

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        route = getattr(request.scope.get("route"), "path", None) or "unmatched"
        stats.record(route, response.status_code, duration_ms)
        logger.info(
            "request",
            extra={
                "event": "request",
                "request_id": request_id,
                "method": request.method,
                "path": request.url.path,
                "route": route,
                "status": response.status_code,
                "duration_ms": duration_ms,
            },
        )
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        if exc.status_code == 404:
            code = ERROR_NOT_FOUND
        elif exc.status_code == 405:
            code = ERROR_METHOD_NOT_ALLOWED
        else:
            code = "HTTP_ERROR"
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(request_id, code, str(exc.detail)),
            headers={"X-Request-ID": request_id},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ):
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        errors = [
            {
                "loc": ".".join(str(item) for item in error.get("loc", ())),
                "msg": error.get("msg", ""),
            }
            for error in exc.errors()
        ]
        return JSONResponse(
            status_code=422,
            content=error_payload(
                request_id,
                ERROR_INVALID_REQUEST,
                "请求参数校验失败",
                {"errors": errors},
            ),
            headers={"X-Request-ID": request_id},
        )

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(
                request_id,
                exc.code,
                exc.message,
                exc.details,
            ),
            headers={"X-Request-ID": request_id},
        )

    app.include_router(health_router.router)
    app.include_router(models_router.router)
    app.include_router(stats_router.router)
    app.include_router(classify_router.router)

    # 开发环境 CORS：只允许本地前端来源，只开放所需方法与请求头。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins(),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID"],
        allow_credentials=False,
        max_age=600,
    )
    return app


app = create_app()
