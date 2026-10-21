"""兼容层：旧的 ``WorkerHttpAdapter`` 名称。

新代码请使用 :class:`serving.adapters.http_worker.HttpWorkerAdapter`（每实例绑定
一个模型，``predict(title, request_id)``）。本类保留旧构造函数
``(classes, client)`` 与旧方法签名 ``predict(spec, title, request_id)``，
仅用于尚未迁移的调用方；网关业务层已不再依赖它。

确认无调用方后可在下一个版本移除本文件。
"""

from __future__ import annotations

from typing import Sequence

import httpx

from ..adapters.http_worker import HttpWorkerAdapter
from ..adapters.protocol import AdapterResult
from .registry import ModelSpec


class WorkerHttpAdapter:
    """Deprecated: 兼容旧接口的适配器包装。"""

    def __init__(self, classes: Sequence[str], client: httpx.AsyncClient) -> None:
        self._classes = tuple(classes)
        self._client = client

    async def predict(
        self, model: ModelSpec, title: str, request_id: str
    ) -> AdapterResult:
        adapter = HttpWorkerAdapter(model, self._classes, self._client)
        return await adapter.predict(title, request_id)


__all__ = ["AdapterResult", "WorkerHttpAdapter"]
