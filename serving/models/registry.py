"""模型注册表（阶段 3）。

职责：
- 从 settings 读取模型元数据、启用状态、worker 映射；
- 提供默认模型与默认比较集合；
- 不发起 HTTP 请求，不做类别校验。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..gateway.settings import ModelConfig, Settings, WorkerConfig


@dataclass(frozen=True)
class ModelSpec:
    model_id: str
    worker_id: str
    display_name: str
    version: str | None
    enabled: bool
    compare_by_default: bool
    worker: WorkerConfig


class ModelRegistry:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._order = list(settings.models.keys())
        self._models: dict[str, ModelSpec] = {}
        for model_id, model in settings.models.items():
            self._models[model_id] = self._build_spec(model_id, model)

    def _build_spec(self, model_id: str, model: ModelConfig) -> ModelSpec:
        worker = self._settings.workers[model.worker]
        return ModelSpec(
            model_id=model_id,
            worker_id=model.worker,
            display_name=model.display_name,
            version=model.model_version,
            enabled=model.enabled,
            compare_by_default=model.compare_by_default,
            worker=worker,
        )

    @property
    def default_model_id(self) -> str:
        return self._settings.default_model

    def get(self, model_id: str) -> ModelSpec | None:
        return self._models.get(model_id)

    def all_model_ids(self) -> list[str]:
        return list(self._order)

    def enabled_model_ids(self) -> list[str]:
        return [model_id for model_id in self._order if self._models[model_id].enabled]

    def compare_default_model_ids(self) -> list[str]:
        return [
            model_id
            for model_id in self._order
            if self._models[model_id].enabled
            and self._models[model_id].compare_by_default
        ]
