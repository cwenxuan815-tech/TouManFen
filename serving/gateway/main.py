"""网关 FastAPI 应用（阶段 3）。

范围：
- 阶段 2 只读：/healthz、/readyz、/models、/stats；
- 阶段 3 分类：/classify、/compare；
- 网关只通过 HTTP 调用 Flask worker，不导入模型/torch/transformers/训练代码；
- request ID / 结构化日志 / 基础统计。
"""

from __future__ import annotations

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

from tmf_common.config import NetworkConfigError, load_network_settings

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
from ..adapters.http_worker import HttpWorkerAdapter
from ..adapters.llm import LlmAdapter
from ..adapters.protocol import ModelAdapter
from ..models.registry import ModelRegistry
from .probes import WorkerProber
from .routers import arena as arena_router
from .routers import bench as bench_router
from .routers import demo as demo_router
from .routers import classify as classify_router
from .routers import feedback as feedback_router
from .routers import health as health_router
from .routers import models as models_router
from .routers import stats as stats_router
from .routers import stream as stream_router
from ..services.arena import ArenaService
from ..services.classification import ClassificationService
from ..services.feedback import FeedbackService
from .settings import SERVING_DIR, load_settings
from .stats import StatsCollector

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
GATEWAY_VERSION = "stage3"


def _cors_origins() -> list[str]:
    """默认只放行配置的前端来源；TMF_CORS_ORIGINS 可追加明确来源（禁止 *）。"""
    try:
        return list(load_network_settings().cors_origins)
    except NetworkConfigError as exc:
        raise ConfigError(str(exc)) from exc


def _build_adapters(
    settings, registry: ModelRegistry, client: httpx.AsyncClient
) -> dict[str, ModelAdapter]:
    """composition root：在这里选择具体适配器，业务层只依赖 ModelAdapter 协议。"""
    adapters: dict[str, ModelAdapter] = {}
    for model_id in settings.models:
        spec = registry.get(model_id)
        if spec is None:
            raise ConfigError(f"模型注册缺失: {model_id}")
        if model_id == "llm":
            # LLM 仍是本地 worker；Provider/密钥只存在于 04-LLM 进程。
            adapters[model_id] = LlmAdapter(spec, settings.classes, client)
        else:
            adapters[model_id] = HttpWorkerAdapter(spec, settings.classes, client)
    return adapters


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
        # 只调用配置的 worker；显式忽略系统代理，避免内网直连被代理拦截。
        app.state.http_client = httpx.AsyncClient(trust_env=False)
        registry = ModelRegistry(settings)
        adapters = _build_adapters(settings, registry, app.state.http_client)
        app.state.registry = registry
        app.state.adapters = adapters
        app.state.classifier = ClassificationService(
            registry=registry,
            adapters=adapters,
            classes=settings.classes,
            max_title_chars=settings.max_title_chars,
            stats=app.state.stats,
            logger=logger,
            # B3 软拒识：阈值/来源元数据来自 models.yaml 的 reject 段（严格校验）。
            reject=settings.reject,
            # B6：SSE 渐进式对比总截止（TMF_COMPARE_TOTAL_TIMEOUT_MS，默认 18000）。
            compare_total_timeout_ms=settings.compare_total_timeout_ms,
            # B12：/compare 与 /compare/stream 并发上限（TMF_COMPARE_CONCURRENCY，默认 3）。
            compare_concurrency=settings.compare_concurrency,
        )
        # B7 竞技场：复用同一注册表与适配器，不重复加载模型；DB 默认
        # serving/run/tmf.db，可由 TMF_DB_PATH 覆盖（表用 CREATE TABLE IF NOT
        # EXISTS，与后续 B4 feedback 共用同一文件）。
        app.state.arena = ArenaService(
            registry=registry,
            adapters=adapters,
            classes=settings.classes,
            logger=logger,
        )
        # B4 反馈：与 B7 共用同一 SQLite 文件（TMF_DB_PATH 可覆盖），只追加
        # feedback 表，不 DROP/重建 arena 表；装配失败时降级为未装配（503），
        # 不影响分类与竞技场主流程。
        try:
            app.state.feedback = FeedbackService(
                registry=registry,
                classes=settings.classes,
                max_title_chars=settings.max_title_chars,
                logger=logger,
            )
        except Exception:
            app.state.feedback = None
            logger.exception(
                "feedback_service_unavailable", extra={"event": "error"}
            )
        if app.state.feedback is not None:
            try:
                # 启动时惰性清理 30 天前的反馈（只删 feedback，不触碰 vote/arena）。
                removed = await app.state.feedback.cleanup_expired()
                if removed:
                    logger.info(
                        "feedback_cleanup",
                        extra={"event": "feedback_cleanup", "removed": removed},
                    )
            except Exception:
                logger.exception(
                    "feedback_cleanup_failed", extra={"event": "error"}
                )
        logger.info("gateway_started", extra={"event": "startup"})
        try:
            yield
        finally:
            feedback_service = getattr(app.state, "feedback", None)
            if feedback_service is not None:
                feedback_service.close()
            app.state.arena.close()
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
    # B8：离线 PK 基准最新产物（只读，不推理）/api/v1/bench/pk。
    app.include_router(bench_router.router)
    # F15：启动数据来源状态（只读 metadata，不推理）。
    app.include_router(demo_router.router)
    # B6：SSE 渐进式对比 /api/v1/compare/stream（旧 POST /compare 保持不变）。
    app.include_router(stream_router.router)
    # B7 竞技场路由：/api/v1/arena/next|vote|leaderboard。
    app.include_router(arena_router.router)
    # B4 反馈路由：/api/v1/feedback、/api/v1/feedback/stats。
    app.include_router(feedback_router.router)

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
