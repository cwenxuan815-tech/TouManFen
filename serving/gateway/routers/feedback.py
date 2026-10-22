"""反馈路由：POST /feedback、GET /feedback/stats（B4）。

路由只通过 ``request.app.state.feedback`` 调用业务层；业务层在 composition
root（``serving/gateway/main.py`` 的 lifespan）注入，便于 fake/TestClient 验收。
本模块不加载模型、不访问网络。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..errors import ApiError, ERROR_FEEDBACK_UNAVAILABLE
from .feedback_schemas import (
    FeedbackStatsResponse,
    FeedbackSubmitRequest,
    FeedbackSubmitResponse,
)
from ...services.feedback import FEEDBACK_ACCEPTED_MESSAGE, FeedbackService

router = APIRouter(prefix="/api/v1", tags=["feedback"])


def _service(request: Request) -> FeedbackService:
    feedback = getattr(request.app.state, "feedback", None)
    if feedback is None:
        # 组合根未装配（例如未启动 lifespan 的裸单测）时明确失败，不伪造数据。
        raise ApiError(503, ERROR_FEEDBACK_UNAVAILABLE, "反馈服务未装配")
    return feedback


@router.post("/feedback", response_model=FeedbackSubmitResponse)
async def submit_feedback(
    request: Request, payload: FeedbackSubmitRequest
) -> FeedbackSubmitResponse:
    outcome = await _service(request).submit(
        client_id=payload.client_id,
        title=payload.title,
        model_id=payload.model_id,
        pred_class=payload.pred_class,
        corrected_class=payload.corrected_class,
        score=payload.score,
        source=payload.source,
        request_id=request.state.request_id,
    )
    return FeedbackSubmitResponse(
        request_id=request.state.request_id,
        feedback_id=outcome.feedback_id,
        status=outcome.status,
        total=outcome.total,
        message=FEEDBACK_ACCEPTED_MESSAGE,
    )


@router.get("/feedback/stats", response_model=FeedbackStatsResponse)
async def feedback_stats(request: Request) -> FeedbackStatsResponse:
    outcome = await _service(request).stats(request_id=request.state.request_id)
    return FeedbackStatsResponse(
        request_id=request.state.request_id,
        total=outcome.total,
        pending=outcome.pending,
        by_class=outcome.by_class,
        by_model=outcome.by_model,
        similar_hits=outcome.similar_hits,
        retention_days=outcome.retention_days,
    )
