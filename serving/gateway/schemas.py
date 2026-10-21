"""阶段 3 网关请求/响应模型。

阶段 2 只读接口保持不变；新增 /classify、/compare 请求响应模型。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorInfo(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    request_id: str
    error: ErrorInfo


class HealthResponse(BaseModel):
    request_id: str
    status: str
    service: str
    version: str


class WorkerState(BaseModel):
    worker_id: str
    status: str
    latency_ms: int | None = None
    error: ErrorInfo | None = None


class ReadyResponse(BaseModel):
    request_id: str
    status: str
    workers: list[WorkerState]


class ModelState(BaseModel):
    model_id: str
    display_name: str
    enabled: bool
    worker_id: str
    model_version: str | None = None
    status: str


class ModelsResponse(BaseModel):
    request_id: str
    default_model: str
    classes: list[str]
    models: list[ModelState]


class RouteStats(BaseModel):
    count: int
    avg_ms: float
    max_ms: float
    latency_buckets: list[dict[str, Any]]


class StatsResponse(BaseModel):
    request_id: str
    process_local: bool
    note: str
    since: str
    uptime_s: float
    total_requests: int
    by_status: dict[str, int]
    routes: dict[str, RouteStats]
    model_calls: dict[str, dict[str, int]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 阶段 3：分类请求/响应
# ---------------------------------------------------------------------------
class ClassifyRequest(BaseModel):
    title: str
    model: str | None = None


class ClassifyResponse(BaseModel):
    request_id: str
    model_id: str
    model_version: str | None = None
    pred_class: str
    latency_ms: int


class CompareRequest(BaseModel):
    title: str
    models: list[str] | None = None


class CompareSummary(BaseModel):
    total: int
    ok: int
    failed: int
    status: str


class CompareResult(BaseModel):
    model_id: str
    status: str
    model_version: str | None = None
    pred_class: str | None = None
    latency_ms: int
    error: ErrorInfo | None = None


class CompareResponse(BaseModel):
    request_id: str
    summary: CompareSummary
    results: list[CompareResult]
