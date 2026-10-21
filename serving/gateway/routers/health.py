"""只读系统状态路由：/healthz、/readyz。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..schemas import ErrorInfo, HealthResponse, ReadyResponse, WorkerState

router = APIRouter(prefix="/api/v1", tags=["system"])


@router.get("/healthz", response_model=HealthResponse)
async def healthz(request: Request) -> HealthResponse:
    """只表示网关进程存活；不探测 worker、不运行模型。"""
    return HealthResponse(
        request_id=request.state.request_id,
        status="ok",
        service="tmf-gateway",
        version="stage3",
    )


@router.get("/readyz", response_model=ReadyResponse)
async def readyz(request: Request) -> ReadyResponse:
    """汇总启用 worker 的本地就绪状态；不触发预测、不访问 LLM 上游。"""
    results = await request.app.state.prober.probe_all()
    workers: list[WorkerState] = []
    enabled_statuses: list[str] = []
    for worker_id, result in results.items():
        workers.append(
            WorkerState(
                worker_id=worker_id,
                status=result.status,
                latency_ms=result.latency_ms,
                error=ErrorInfo(**result.error) if result.error else None,
            )
        )
        if result.status != "disabled":
            enabled_statuses.append(result.status)
    overall = (
        "ok"
        if enabled_statuses and all(status == "up" for status in enabled_statuses)
        else "degraded"
    )
    return ReadyResponse(
        request_id=request.state.request_id,
        status=overall,
        workers=workers,
    )
