"""F15：启动时数据来源状态（只读，不触发模型推理）。

规则与 B11 完全一致：
- 每个已启用模型的 worker `/metadata.display_name` 以 ``mock-`` 开头 → mock；
- 非 mock 的有效字符串 → live；
- metadata 缺失/非法/worker 不可达 → unknown（绝不因可达/进程存活推断 live）；
- 仅当所有已启用模型都 ready 且来源一致时才返回 live/mock；否则 unknown。
不返回 cached；cached 只由前端在快照成功加载后标注。
"""

from __future__ import annotations

from collections import Counter

from fastapi import APIRouter, Request

from ...adapters.protocol import (
    DATA_SOURCE_LIVE,
    DATA_SOURCE_MOCK,
    DATA_SOURCE_UNKNOWN,
    data_source_from_display_name,
)
from .demo_schemas import DemoStateResponse, DemoWorkerSource

router = APIRouter(prefix="/api/v1", tags=["demo"])


def _overall_source(worker_sources: list[DemoWorkerSource]) -> tuple[str, str, bool]:
    if not worker_sources:
        return DATA_SOURCE_UNKNOWN, "no_enabled_models", False
    ready = [item for item in worker_sources if item.ready and item.metadata_available]
    if len(ready) != len(worker_sources):
        return DATA_SOURCE_UNKNOWN, "workers_not_ready_or_metadata_missing", False
    sources = {item.data_source for item in ready}
    if len(sources) != 1:
        return DATA_SOURCE_UNKNOWN, "worker_sources_mixed", True
    source = next(iter(sources))
    if source not in (DATA_SOURCE_LIVE, DATA_SOURCE_MOCK):
        return DATA_SOURCE_UNKNOWN, "worker_source_unknown", False
    return source, "all_workers_ready_same_source", False


@router.get("/demo/state", response_model=DemoStateResponse)
async def demo_state(request: Request) -> DemoStateResponse:
    registry = request.app.state.registry
    prober = request.app.state.prober
    request_id = getattr(request.state, "request_id", "")
    model_ids = registry.enabled_model_ids()
    try:
        probes = await prober.probe_all()
    except Exception:
        return DemoStateResponse(
            request_id=request_id,
            data_source=DATA_SOURCE_UNKNOWN,
            ready_models=0,
            total_models=len(model_ids),
            mixed=False,
            reason="probe_error",
            source_counts={DATA_SOURCE_UNKNOWN: len(model_ids)},
            worker_sources=[
                DemoWorkerSource(
                    model_id=model_id,
                    worker_id=registry.get(model_id).worker_id,
                    ready=False,
                    metadata_available=False,
                    data_source=DATA_SOURCE_UNKNOWN,
                    display_name=None,
                )
                for model_id in model_ids
                if registry.get(model_id) is not None
            ],
        )

    worker_sources: list[DemoWorkerSource] = []
    for model_id in model_ids:
        spec = registry.get(model_id)
        if spec is None:
            continue
        probe = probes.get(spec.worker_id)
        ready = bool(probe is not None and probe.status == "up")
        metadata = probe.metadata if ready and probe is not None else None
        display_name = metadata.get("display_name") if isinstance(metadata, dict) else None
        source = (
            data_source_from_display_name(display_name)
            if metadata is not None
            else DATA_SOURCE_UNKNOWN
        )
        worker_sources.append(
            DemoWorkerSource(
                model_id=model_id,
                worker_id=spec.worker_id,
                ready=ready,
                metadata_available=isinstance(metadata, dict),
                data_source=source,
                display_name=display_name if isinstance(display_name, str) else None,
            )
        )

    overall, reason, mixed = _overall_source(worker_sources)
    counts = Counter(item.data_source for item in worker_sources)
    return DemoStateResponse(
        request_id=request_id,
        data_source=overall,
        ready_models=sum(1 for item in worker_sources if item.ready),
        total_models=len(worker_sources),
        mixed=mixed,
        reason=reason,
        source_counts=dict(counts),
        worker_sources=worker_sources,
    )
