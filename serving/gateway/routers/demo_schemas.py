"""F15 启动数据来源状态响应模型。"""

from __future__ import annotations

from pydantic import BaseModel


class DemoWorkerSource(BaseModel):
    model_id: str
    worker_id: str
    ready: bool
    metadata_available: bool
    data_source: str
    display_name: str | None = None


class DemoStateResponse(BaseModel):
    request_id: str
    data_source: str
    ready_models: int
    total_models: int
    mixed: bool
    reason: str
    source_counts: dict[str, int]
    worker_sources: list[DemoWorkerSource]
