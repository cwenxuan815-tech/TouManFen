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
    ERROR_ACCESS_UNAVAILABLE,
    ERROR_INVALID_REQUEST,
    ERROR_INVALID_REQUEST_ID,
    ERROR_METHOD_NOT_ALLOWED,
    ERROR_NOT_FOUND,
    error_payload,
)
from .client_ip import ClientIpError, resolve_client_ip
from .access_config import AccessConfigError, load_access_settings
from .access_gate import (
    ACCESS_TICKET_HEADER,
    AccessDenied,
    AccessGate,
    AccessGateUnavailable,
    is_access_exempt,
)
from .logging_utils import log_startup_error, setup_logging
from .quota_config import (
    LlmQuotaConfigError,
    load_llm_quota_settings,
)
from ..services.access import AccessManager
from ..adapters.http_worker import HttpWorkerAdapter
from ..adapters.llm import LlmAdapter
from ..adapters.protocol import ModelAdapter
from ..adapters.quotaed_llm import QuotaedLlmAdapter
from ..services.llm_quota import LlmQuotaStore, LlmQuotaStoreError
from ..models.registry import ModelRegistry
from .probes import WorkerProber
from .routers import access as access_router
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
    settings,
    registry: ModelRegistry,
    client: httpx.AsyncClient,
    llm_quota: LlmQuotaStore | None,
) -> dict[str, ModelAdapter]:
    """composition root：在这里选择具体适配器，业务层只依赖 ModelAdapter 协议。

    LLM 唯一入口统一包一层 QuotaedLlmAdapter；缺少配额 store 时包装器也会
    fail-closed，而不是退回未受保护的 LlmAdapter。
    """
    adapters: dict[str, ModelAdapter] = {}
    for model_id in settings.models:
        spec = registry.get(model_id)
        if spec is None:
            raise ConfigError(f"模型注册缺失: {model_id}")
        if spec.worker_id == "llm":
            # LLM 仍是本地 worker；Provider/密钥只存在于 04-LLM 进程。
            adapters[model_id] = QuotaedLlmAdapter(
                LlmAdapter(spec, settings.classes, client),
                llm_quota,
            )
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
        app.state.trusted_proxies = ()
        app.state.access_enabled = False
        app.state.access_gate = None
        try:
            access_settings = load_access_settings()
        except AccessConfigError:
            logger.error("access_control_config_invalid", extra={"event": "error"})
            raise ConfigError(
                "访问控制配置无效；检查 TMF_ACCESS_CONTROL_ENABLED / "
                "TMF_LLM_QUOTA_HMAC_KEY"
            ) from None
        if access_settings.enabled:
            try:
                app.state.access_gate = AccessGate(
                    AccessManager(),
                    access_settings.hmac_key,
                    status_min_interval_seconds=access_settings.status_min_interval_seconds,
                )
            except Exception:
                logger.error("access_control_init_failed", extra={"event": "error"})
                raise ConfigError("访问控制初始化失败") from None
            app.state.access_enabled = True
        app.state.llm_quota = None
        try:
            quota_settings = load_llm_quota_settings()
            app.state.trusted_proxies = quota_settings.trusted_proxies
            app.state.llm_quota = LlmQuotaStore(
                quota_settings.db_path,
                quota_settings.hmac_key,
            )
        except (LlmQuotaConfigError, LlmQuotaStoreError):
            # 不输出 key/SQL 细节；LLM 路径由包装器 fail-closed，其他模型仍可用。
            logger.error("llm_quota_unavailable", extra={"event": "error"})
            app.state.llm_quota = None
            app.state.trusted_proxies = ()
        # 只调用配置的 worker；显式忽略系统代理，避免内网直连被代理拦截。
        app.state.http_client = httpx.AsyncClient(trust_env=False)
        registry = ModelRegistry(settings)
        adapters = _build_adapters(
            settings,
            registry,
            app.state.http_client,
            app.state.llm_quota,
        )
        app.state.registry = registry
        app.state.adapters = adapters
        app.state.classifier = ClassificationService(
            registry=registry,
            adapters=adapters,
            classes=settings.classes,
            max_title_chars=settings.max_title_chars,
            stats=app.state.stats,
            logger=logger,
            # 阶段 4B：所有 LLM adapter 已由 QuotaedLlmAdapter 包装；
            # 即使 store 初始化失败也启用强制预检，使 LLM 请求 fail-closed。
            llm_quota=app.state.llm_quota,
            llm_quota_enforced=True,
            # B3 软拒识：阈值/来源元数据来自 models.yaml 的 reject 段（严格校验）。
            reject=settings.reject,
            # B6：SSE 渐进式对比总截止（TMF_COMPARE_TOTAL_TIMEOUT_MS，默认 18000）。
            compare_total_timeout_ms=settings.compare_total_timeout_ms,
            # B12：/compare 与 /compare/stream 并发上限（TMF_COMPARE_CONCURRENCY，默认 3）。
            compare_concurrency=settings.compare_concurrency,
            # 阶段 2A：批量分类硬上限、进程级并发与 30s 总时限。
            batch_max_items=settings.batch_max_items,
            batch_max_total_chars=settings.batch_max_total_chars,
            batch_concurrency=settings.batch_concurrency,
            batch_total_timeout_ms=settings.batch_total_timeout_ms,
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
            if getattr(app.state, "llm_quota", None) is not None:
                app.state.llm_quota.close()
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
        # 只解析一次可信客户端 IP；不可信来源的转发头不会改变它。
        # 解析失败时保留 None，让唯一需要身份的 LLM 路径 fail-closed，
        # 不阻塞本地模型/健康检查等非 LLM 请求。
        try:
            request.state.client_ip = resolve_client_ip(
                request.client.host if request.client is not None else None,
                request.headers,
                getattr(request.app.state, "trusted_proxies", ()),
            )
        except ClientIpError:
            request.state.client_ip = None
        if getattr(request.app.state, "access_enabled", False) and not is_access_exempt(
            request.url.path, request.method
        ):
            gate = getattr(request.app.state, "access_gate", None)
            if not isinstance(gate, AccessGate):
                logger.error(
                    "access_unavailable",
                    extra={"event": "error", "request_id": request_id},
                )
                return JSONResponse(
                    status_code=503,
                    content=error_payload(
                        request_id,
                        ERROR_ACCESS_UNAVAILABLE,
                        "访问管理暂时不可用，请稍后重试。",
                    ),
                    headers={"X-Request-ID": request_id},
                )
            try:
                access_result = gate.authorize(
                    request.state.client_ip,
                    request.headers.get(ACCESS_TICKET_HEADER),
                )
            except AccessDenied as exc:
                logger.info(
                    "access_denied",
                    extra={
                        "event": "access",
                        "request_id": request_id,
                        "path": request.url.path,
                        "status": exc.status_code,
                        "error_code": exc.code,
                    },
                )
                headers = {"X-Request-ID": request_id}
                if exc.retry_after_seconds is not None:
                    headers["Retry-After"] = str(
                        max(1, int(round(exc.retry_after_seconds)))
                    )
                return JSONResponse(
                    status_code=exc.status_code,
                    content=error_payload(
                        request_id,
                        exc.code,
                        exc.message,
                        exc.details,
                    ),
                    headers=headers,
                )
            except AccessGateUnavailable:
                logger.error(
                    "access_unavailable",
                    extra={"event": "error", "request_id": request_id},
                )
                return JSONResponse(
                    status_code=503,
                    content=error_payload(
                        request_id,
                        ERROR_ACCESS_UNAVAILABLE,
                        "访问管理暂时不可用，请稍后重试。",
                    ),
                    headers={"X-Request-ID": request_id},
                )
            request.state.access_remaining_seconds = access_result.remaining_seconds
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
        # 排队前端会按约 2s 轮询 status；不把每条轮询写满日志。
        if request.url.path != "/api/v1/access/status":
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
    # 阶段 5B：access API 必须在业务 gate 之前可用；业务 gate 在 middleware 中。
    app.include_router(access_router.router)
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
        allow_headers=["Content-Type", "X-Request-ID", ACCESS_TICKET_HEADER],
        expose_headers=["X-Request-ID", "Retry-After"],
        allow_credentials=False,
        max_age=600,
    )
    return app


app = create_app()
