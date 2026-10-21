"""分类路由：/classify、/compare（阶段 3）。"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..schemas import (
    ClassifyRequest,
    ClassifyResponse,
    CompareRequest,
    CompareResponse,
)

router = APIRouter(prefix="/api/v1", tags=["classification"])


@router.post("/classify", response_model=ClassifyResponse)
async def classify(request: Request, payload: ClassifyRequest) -> ClassifyResponse:
    outcome = await request.app.state.classifier.classify(
        title=payload.title,
        model_id=payload.model,
        request_id=request.state.request_id,
    )
    return ClassifyResponse(
        request_id=request.state.request_id,
        model_id=outcome.model_id,
        model_version=outcome.model_version,
        pred_class=outcome.pred_class,
        latency_ms=outcome.latency_ms,
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
