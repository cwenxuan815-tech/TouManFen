"""B6：``GET /api/v1/compare/stream`` 渐进式 SSE 路由。

契约（与 F9 前端一致，见工作清单 §6.6 / B6）：
- ``Content-Type: text/event-stream; charset=utf-8``、``Cache-Control: no-cache``、
  ``X-Accel-Buffering: no``、``Connection: keep-alive``；
- ``event: model`` 每个模型最多一次，按实际完成顺序；逐模型失败用 ``status=error``，
  不发流级 ``error``；
- ``event: summary`` + ``event: done`` 在全部完成或总截止后各一次，随后关闭流；
- ``event: error`` 仅用于请求/流级致命错误（标题非法、模型参数非法、内部错误）；
- 心跳为注释行 ``: heartbeat``（每 15s），不伪装 model 事件、不延长总截止；
- 客户端断连后停止 push，并取消网关侧未完成 task（不强制中止 worker，§6.6）。

旧 ``POST /api/v1/compare`` 行为完全不变（见 ``routers/classify.py``）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, AsyncIterator, Mapping

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from ..errors import ApiError, ERROR_INTERNAL, ERROR_INVALID_MODELS, ERROR_INVALID_TITLE
from ...services.classification import (
    StreamHeartbeatEvent,
    StreamModelEvent,
    StreamSummaryEvent,
)

router = APIRouter(prefix="/api/v1", tags=["classification"])

_logger = logging.getLogger("tmf.gateway")

#: SSE 固定响应头（Starlette 会为 text/* 自动补 charset=utf-8）。
SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}

#: 心跳注释行：不是事件，EventSource 解析时忽略（不会触发 onmessage）。
HEARTBEAT_LINE = ": heartbeat\n\n"


def _format_sse(event: str, data: Mapping[str, Any]) -> str:
    """渲染 ``event: <name>\\ndata: <json>\\n\\n``（JSON 不含裸换行，单帧安全）。"""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


def _parse_models(raw: str | None) -> list[str] | None:
    """解析 ``models=a,b,c``；省略返回 None（沿用 compare 默认集合）。"""
    if raw is None:
        return None
    if not raw.strip():
        raise ApiError(400, ERROR_INVALID_MODELS, "models 不能为空")
    model_ids = [part.strip() for part in raw.split(",")]
    if any(not model_id for model_id in model_ids):
        raise ApiError(400, ERROR_INVALID_MODELS, "models 参数格式非法")
    return model_ids


@router.get("/compare/stream")
async def compare_stream(
    request: Request,
    title: str | None = None,
    models: str | None = None,
) -> StreamingResponse:
    """SSE 渐进式多模型对比；参数走 query（EventSource 不能带 body）。"""
    request_id = getattr(request.state, "request_id", None) or str(uuid.uuid4())

    async def event_source() -> AsyncIterator[str]:
        stream = None
        finished = False
        try:
            if title is None:
                raise ApiError(400, ERROR_INVALID_TITLE, "缺少 title 参数")
            model_ids = _parse_models(models)
            classifier = request.app.state.classifier
            # 校验错误（标题/模型）在首次迭代时由服务层抛 ApiError，统一转 error 事件；
            # 逐模型失败不会走到这里，而是 model 事件 status=error。
            stream = classifier.compare_stream(
                title=title,
                models=model_ids,
                request_id=request_id,
                is_disconnected=request.is_disconnected,
            )
            async for event in stream:
                if isinstance(event, StreamHeartbeatEvent):
                    yield HEARTBEAT_LINE
                elif isinstance(event, StreamModelEvent):
                    yield _format_sse(
                        "model",
                        {
                            "request_id": request_id,
                            "model_id": event.model_id,
                            "status": event.status,
                            "latency_ms": event.latency_ms,
                            "model_version": event.model_version,
                            "pred_class": event.pred_class,
                            # B11：新增字段（向后兼容）；live / mock / unknown。
                            "data_source": event.data_source,
                            "error": (
                                None
                                if event.status == "ok"
                                else {
                                    "code": event.error_code,
                                    "message": event.error_message,
                                }
                            ),
                        },
                    )
                elif isinstance(event, StreamSummaryEvent):
                    yield _format_sse(
                        "summary",
                        {
                            "request_id": request_id,
                            "status": event.status,
                            "total": event.total,
                            "ok": event.ok,
                            "failed": event.failed,
                            "elapsed_ms": event.elapsed_ms,
                            "total_timeout_ms": event.total_timeout_ms,
                            "partial": event.partial,
                        },
                    )
                    yield _format_sse(
                        "done",
                        {
                            "request_id": request_id,
                            "status": event.status,
                            "total": event.total,
                            "ok": event.ok,
                            "failed": event.failed,
                        },
                    )
                    finished = True
                    return
        except asyncio.CancelledError:
            # 客户端断连由 Starlette 取消流任务，交给 finally 清理。
            raise
        except ApiError as exc:
            # 请求级致命错误：流已开始也用 error 事件返回（不记录标题）。
            yield _format_sse(
                "error",
                {
                    "request_id": request_id,
                    "code": exc.code,
                    "message": exc.message,
                },
            )
        except Exception:
            _logger.exception(
                "compare_stream_failed",
                extra={"event": "error", "request_id": request_id},
            )
            if not finished:
                yield _format_sse(
                    "error",
                    {
                        "request_id": request_id,
                        "code": ERROR_INTERNAL,
                        "message": "网关内部错误",
                    },
                )
        finally:
            # 关闭服务层生成器：取消/收尾未完成 task，避免 task 泄漏。
            if stream is not None:
                try:
                    await stream.aclose()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception(
                        "compare_stream_close_failed",
                        extra={"event": "error", "request_id": request_id},
                    )

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers=SSE_HEADERS,
    )
