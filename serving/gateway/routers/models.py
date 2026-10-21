"""只读模型注册表路由：/models。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..schemas import ModelsResponse, ModelState

router = APIRouter(prefix="/api/v1", tags=["system"])


@router.get("/models", response_model=ModelsResponse)
async def list_models(request: Request) -> ModelsResponse:
    """返回固定 5 个模型的注册信息与当前可用状态。

    - 不触发分类推理；
    - 版本仅来自 worker /metadata 或静态配置；不可得时为 null；
    - 状态来自带短缓存的只读探测。
    """
    settings = request.app.state.settings
    probe_results = await request.app.state.prober.probe_all()

    models: list[ModelState] = []
    for model_id, model in settings.models.items():
        probe = probe_results.get(model.worker)
        if not model.enabled:
            status = "disabled"
        elif probe is None:
            status = "unknown"
        else:
            status = probe.status

        version: str | None = None
        if probe is not None and isinstance(probe.metadata, dict):
            metadata_version = probe.metadata.get("version")
            if isinstance(metadata_version, str) and metadata_version.strip():
                version = metadata_version
        if version is None:
            version = model.model_version

        models.append(
            ModelState(
                model_id=model_id,
                display_name=model.display_name,
                enabled=model.enabled,
                worker_id=model.worker,
                model_version=version,
                status=status,
            )
        )

    return ModelsResponse(
        request_id=request.state.request_id,
        default_model=settings.default_model,
        classes=list(settings.classes),
        models=models,
    )
