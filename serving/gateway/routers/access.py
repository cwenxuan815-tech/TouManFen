"""阶段 5B：访问管理 HTTP API。

路径：
    POST /api/v1/access/enter
    GET  /api/v1/access/status
    POST /api/v1/access/claim
    POST /api/v1/access/leave
    POST /api/v1/access/cancel

ticket 只从 ``X-Access-Ticket`` 请求头读取；不进入 URL/query。
本模块不含业务 API gate；gate 在 main middleware 中执行。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..access_gate import (
    ACCESS_TICKET_HEADER,
    AccessDenied,
    AccessGate,
    AccessGateUnavailable,
    AccessPollThrottled,
)
from ..access_schemas import AccessStateResponse
from ..errors import (
    ApiError,
    ERROR_ACCESS_COOLING,
    ERROR_ACCESS_DISABLED,
    ERROR_ACCESS_IDENTITY_INVALID,
    ERROR_ACCESS_POLL_THROTTLED,
    ERROR_ACCESS_QUEUE_FULL,
    ERROR_ACCESS_STATE_INVALID,
    ERROR_ACCESS_TICKET_INVALID,
    ERROR_ACCESS_UNAVAILABLE,
)
from ...services.access import (
    AccessError,
    AccessResult,
    AccessStateError,
    AccessTicketError,
)

router = APIRouter(prefix="/api/v1", tags=["access"])


def _gate(request: Request) -> AccessGate:
    if not getattr(request.app.state, "access_enabled", False):
        raise ApiError(
            404,
            ERROR_ACCESS_DISABLED,
            "访问控制未启用。",
        )
    gate = getattr(request.app.state, "access_gate", None)
    if not isinstance(gate, AccessGate):
        # 启用但 manager 缺失/装配失败：不得 fail-open。
        raise ApiError(
            503,
            ERROR_ACCESS_UNAVAILABLE,
            "访问管理暂时不可用，请稍后重试。",
        )
    return gate


def _client_ip(request: Request) -> str:
    client_ip = getattr(request.state, "client_ip", None)
    if not client_ip:
        raise ApiError(
            403,
            ERROR_ACCESS_IDENTITY_INVALID,
            "无法确认客户端身份，拒绝访问。",
        )
    return str(client_ip)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "") or ""


def _ticket_from_header(request: Request) -> str | None:
    raw = request.headers.get(ACCESS_TICKET_HEADER)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def _require_ticket(request: Request) -> str:
    ticket = _ticket_from_header(request)
    if ticket is None:
        raise ApiError(
            403,
            ERROR_ACCESS_TICKET_INVALID,
            "缺少访问票据。",
        )
    return ticket


def _response(request: Request, result: AccessResult) -> AccessStateResponse:
    return AccessStateResponse(
        request_id=_request_id(request),
        state=result.state,
        ticket=result.ticket,
        position=result.position,
        ahead=result.ahead,
        remaining_seconds=result.remaining_seconds,
    )


def _map_manager_error(exc: Exception) -> ApiError:
    if isinstance(exc, AccessTicketError):
        return ApiError(
            403,
            ERROR_ACCESS_TICKET_INVALID,
            "访问票据无效或已过期。",
        )
    if isinstance(exc, AccessStateError):
        return ApiError(
            409,
            ERROR_ACCESS_STATE_INVALID,
            "当前访问状态不允许该操作。",
        )
    if isinstance(exc, AccessDenied):
        return ApiError(exc.status_code, exc.code, exc.message, exc.details)
    return ApiError(
        503,
        ERROR_ACCESS_UNAVAILABLE,
        "访问管理暂时不可用，请稍后重试。",
    )


@router.post("/access/enter", response_model=AccessStateResponse)
async def enter(request: Request) -> AccessStateResponse:
    gate = _gate(request)
    client_ip = _client_ip(request)
    ticket = _ticket_from_header(request)
    try:
        result = gate.enter(client_ip, ticket)
    except (AccessError, AccessGateUnavailable, AccessDenied) as exc:
        raise _map_manager_error(exc) from exc
    if result.state == "queue_full":
        raise ApiError(
            429,
            ERROR_ACCESS_QUEUE_FULL,
            "排队人数已满，请稍后重试。",
            {"state": "queue_full"},
        )
    return _response(request, result)


@router.get("/access/status", response_model=AccessStateResponse)
async def status(request: Request) -> AccessStateResponse:
    gate = _gate(request)
    client_ip = _client_ip(request)
    ticket = _require_ticket(request)
    try:
        result = gate.status(client_ip, ticket)
    except AccessPollThrottled as exc:
        raise ApiError(
            429,
            ERROR_ACCESS_POLL_THROTTLED,
            "状态轮询过于频繁，请稍后重试。",
            {"retry_after_seconds": exc.retry_after_seconds},
        ) from exc
    except (AccessError, AccessGateUnavailable, AccessDenied) as exc:
        raise _map_manager_error(exc) from exc
    if result.state == "expired":
        raise ApiError(
            403,
            ERROR_ACCESS_TICKET_INVALID,
            "访问票据无效或已过期。",
            {"state": "expired"},
        )
    return _response(request, result)


@router.post("/access/claim", response_model=AccessStateResponse)
async def claim(request: Request) -> AccessStateResponse:
    gate = _gate(request)
    client_ip = _client_ip(request)
    ticket = _require_ticket(request)
    try:
        result = gate.claim(client_ip, ticket)
    except (AccessError, AccessGateUnavailable, AccessDenied) as exc:
        raise _map_manager_error(exc) from exc
    return _response(request, result)


@router.post("/access/leave", response_model=AccessStateResponse)
async def leave(request: Request) -> AccessStateResponse:
    gate = _gate(request)
    client_ip = _client_ip(request)
    ticket = _require_ticket(request)
    try:
        result = gate.leave(client_ip, ticket)
    except (AccessError, AccessGateUnavailable, AccessDenied) as exc:
        raise _map_manager_error(exc) from exc
    return _response(request, result)


@router.post("/access/cancel", response_model=AccessStateResponse)
async def cancel(request: Request) -> AccessStateResponse:
    gate = _gate(request)
    client_ip = _client_ip(request)
    ticket = _require_ticket(request)
    try:
        result = gate.cancel(client_ip, ticket)
    except (AccessError, AccessGateUnavailable, AccessDenied) as exc:
        raise _map_manager_error(exc) from exc
    return _response(request, result)
