"""分类路由：/classify、/compare（阶段 3）。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..schemas import (
    ClassifyRequest,
    ClassifyResponse,
    CompareRequest,
    CompareResponse,
    HumanFeedbackHint,
    TopKEntry,
)

router = APIRouter(prefix="/api/v1", tags=["classification"])


async def _human_feedback_hint(
    request: Request, title: str
) -> HumanFeedbackHint | None:
    """B4：查询相似 pending 反馈；未装配/查询失败时安全跳过（不影响分类）。"""
    feedback = getattr(request.app.state, "feedback", None)
    if feedback is None:
        return None
    try:
        hint = await feedback.similar_hint(title)
    except Exception:
        # 保留期清理/hint 只是增强信息，失败时绝不能改变分类结果或报错。
        return None
    if hint is None:
        return None
    return HumanFeedbackHint(
        corrected_class=hint.corrected_class,
        status=hint.status,
    )


@router.post("/classify", response_model=ClassifyResponse)
async def classify(request: Request, payload: ClassifyRequest) -> ClassifyResponse:
    outcome = await request.app.state.classifier.classify(
        title=payload.title,
        model_id=payload.model,
        request_id=request.state.request_id,
    )
    scores = (
        {entry.class_id: entry.score for entry in outcome.scores}
        if outcome.scores is not None
        else None
    )
    top_k = (
        [TopKEntry(class_id=entry.class_id, score=entry.score) for entry in outcome.top_k]
        if outcome.top_k is not None
        else None
    )
    # hint 只追加参考信息，pred_class / latency_ms / scores / top_k 保持模型原值。
    hint = await _human_feedback_hint(request, payload.title)
    return ClassifyResponse(
        request_id=request.state.request_id,
        model_id=outcome.model_id,
        model_version=outcome.model_version,
        pred_class=outcome.pred_class,
        latency_ms=outcome.latency_ms,
        scores=scores,
        score_kind=outcome.score_kind,
        top_k=top_k,
        # B3：业务层已判定，路由只做字段映射（软拒识保留 pred_class/分数）。
        rejected=outcome.rejected,
        reject_reason=outcome.reject_reason,
        threshold_meta=outcome.threshold_meta,
        human_feedback_hint=hint,
        # B11：来源由适配器按 worker /metadata.display_name 判定（mock-/live/unknown）。
        data_source=outcome.data_source,
    )


@router.post("/compare", response_model=CompareResponse)
async def compare(request: Request, payload: CompareRequest):
    outcome = await request.app.state.classifier.compare(
        title=payload.title,
        models=payload.models,
        request_id=request.state.request_id,
    )

    results = []
    for item in outcome.items:
        entry: dict[str, object] = {
            "model_id": item.model_id,
            "status": item.status,
            "latency_ms": item.latency_ms,
            # B11：逐项来源（error 项也可用于区分 mock 离线等），不推断 live。
            "data_source": item.data_source,
        }
        if item.status == "ok":
            entry["model_version"] = item.model_version
            entry["pred_class"] = item.pred_class
        else:
            entry["error"] = {
                "code": item.error_code,
                "message": item.error_message,
            }
        results.append(entry)

    body = {
        "request_id": request.state.request_id,
        "summary": {
            "total": outcome.total,
            "ok": outcome.ok,
            "failed": outcome.failed,
            "status": outcome.status,
        },
        "results": results,
    }
    # 使用 JSONResponse 以便失败项精确省略 pred_class / model_version。
    return JSONResponse(status_code=200, content=body)
