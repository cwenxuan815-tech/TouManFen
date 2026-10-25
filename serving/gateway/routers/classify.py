"""分类路由：/classify、/compare（阶段 3）。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..batch_schemas import (
    BatchClassifyRequest,
    BatchClassifyResponse,
    BatchItemResult,
    BatchSummary,
)
from ..errors import ERROR_INTERNAL
from ..schemas import (
    ClassifyRequest,
    ClassifyResponse,
    CompareRequest,
    CompareResponse,
    ErrorInfo,
    HumanFeedbackHint,
    TopKEntry,
)
from ...services.classification import (
    BatchItemInput,
    BatchItemOutcome,
    BatchOutcome,
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
        client_ip=getattr(request.state, "client_ip", None),
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


def _batch_item_result(item: BatchItemOutcome) -> BatchItemResult:
    """把服务层结果映射为公开响应；失败项不携带任何伪造类别。"""
    if item.status == "ok" and item.outcome is not None:
        outcome = item.outcome
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
        return BatchItemResult(
            item_id=item.item_id,
            index=item.index,
            status="ok",
            pred_class=outcome.pred_class,
            latency_ms=outcome.latency_ms,
            model_version=outcome.model_version,
            data_source=outcome.data_source,
            scores=scores,
            score_kind=outcome.score_kind,
            top_k=top_k,
            rejected=outcome.rejected,
            reject_reason=outcome.reject_reason,
            threshold_meta=outcome.threshold_meta,
        )
    return BatchItemResult(
        item_id=item.item_id,
        index=item.index,
        status="error",
        latency_ms=item.latency_ms,
        error=ErrorInfo(
            code=item.error_code or ERROR_INTERNAL,
            message=item.error_message or "批量条目分类失败",
            details=item.error_details,
        ),
    )


def _batch_response(
    request_id: str, outcome: BatchOutcome, batch_id: str | None
) -> BatchClassifyResponse:
    return BatchClassifyResponse(
        request_id=request_id,
        batch_id=batch_id,
        model_id=outcome.model_id,
        summary=BatchSummary(
            total=outcome.total,
            ok=outcome.ok,
            failed=outcome.failed,
            status=outcome.status,
            elapsed_ms=outcome.elapsed_ms,
            total_timeout_ms=outcome.total_timeout_ms,
        ),
        results=[_batch_item_result(item) for item in outcome.items],
    )


@router.post(
    "/classify/batch",
    response_model=BatchClassifyResponse,
    response_model_exclude_none=True,
)
async def classify_batch(
    request: Request, payload: BatchClassifyRequest
) -> BatchClassifyResponse:
    """多条标题 × 单模型批量分类（阶段 2A）。

    - 模型、条数、总字符、每项 item_id/title 全部在服务层预检；
    - ``llm`` 在任何 worker 调用前拒绝；
    - 不调用反馈相似度扫描，不返回 ``human_feedback_hint``；
    - 批次总时限/进程级并发由 ``ClassificationService`` 负责。
    """
    outcome = await request.app.state.classifier.classify_batch(
        model=payload.model,
        batch_id=payload.batch_id,
        items=[
            BatchItemInput(item_id=item.item_id, title=item.title)
            for item in payload.items
        ],
        request_id=request.state.request_id,
        is_disconnected=request.is_disconnected,
    )
    return _batch_response(request.state.request_id, outcome, payload.batch_id)


@router.post("/compare", response_model=CompareResponse)
async def compare(request: Request, payload: CompareRequest):
    outcome = await request.app.state.classifier.compare(
        title=payload.title,
        models=payload.models,
        request_id=request.state.request_id,
        client_ip=getattr(request.state, "client_ip", None),
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
