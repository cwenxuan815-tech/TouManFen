"""Worker HTTP 适配器（阶段 3）。

约束：
- 只向配置的 worker `predict_path` 发送 `{"title": ...}`；
- 可透传 `X-Request-ID`，但不要求 worker 支持；
- 使用配置超时，无重试；
- 不把 worker 的原始错误文本返回给调用方；
- 校验 HTTP 状态、JSON、`pred_class` 与 10 类白名单。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from ..gateway.errors import (
    ERROR_INVALID_MODEL_OUTPUT,
    ERROR_MODEL_ERROR,
    ERROR_WORKER_BAD_RESPONSE,
    ERROR_WORKER_HTTP_ERROR,
    ERROR_WORKER_TIMEOUT,
    ERROR_WORKER_UNAVAILABLE,
)
from .registry import ModelSpec


@dataclass
class AdapterResult:
    model_id: str
    status: str  # ok | error
    latency_ms: int
    pred_class: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class WorkerHttpAdapter:
    def __init__(
        self,
        classes: tuple[str, ...],
        client: httpx.AsyncClient,
    ) -> None:
        self._classes = frozenset(classes)
        self._client = client

    @staticmethod
    def _url(model: ModelSpec) -> str:
        return model.worker.base_url.rstrip("/") + model.worker.predict_path

    @staticmethod
    def _timeout(model: ModelSpec) -> httpx.Timeout:
        seconds = model.worker.timeout_ms / 1000.0
        return httpx.Timeout(seconds, connect=min(0.5, seconds))

    async def predict(
        self, model: ModelSpec, title: str, request_id: str
    ) -> AdapterResult:
        started = time.perf_counter()
        try:
            response = await self._client.post(
                self._url(model),
                json={"title": title},
                headers={"X-Request-ID": request_id},
                timeout=self._timeout(model),
            )
        except httpx.ConnectTimeout:
            # 连接阶段超时 = 不可达，而不是“模型处理超时”
            return AdapterResult(
                model_id=model.model_id,
                status="error",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error_code=ERROR_WORKER_UNAVAILABLE,
                error_message="所选模型服务不可达",
            )
        except httpx.TimeoutException:
            return AdapterResult(
                model_id=model.model_id,
                status="error",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error_code=ERROR_WORKER_TIMEOUT,
                error_message="所选模型未在配置的时间内返回",
            )
        except httpx.HTTPError:
            return AdapterResult(
                model_id=model.model_id,
                status="error",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error_code=ERROR_WORKER_UNAVAILABLE,
                error_message="所选模型服务不可达",
            )
        except Exception:
            return AdapterResult(
                model_id=model.model_id,
                status="error",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error_code=ERROR_WORKER_UNAVAILABLE,
                error_message="所选模型调用异常",
            )

        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            return AdapterResult(
                model_id=model.model_id,
                status="error",
                latency_ms=latency_ms,
                error_code=ERROR_WORKER_HTTP_ERROR,
                error_message="所选模型服务返回非 200 状态",
            )

        try:
            data = response.json()
        except Exception:
            return AdapterResult(
                model_id=model.model_id,
                status="error",
                latency_ms=latency_ms,
                error_code=ERROR_WORKER_BAD_RESPONSE,
                error_message="所选模型返回格式不符合契约",
            )

        if not isinstance(data, dict):
            return AdapterResult(
                model_id=model.model_id,
                status="error",
                latency_ms=latency_ms,
                error_code=ERROR_WORKER_BAD_RESPONSE,
                error_message="所选模型返回格式不符合契约",
            )

        if "error" in data:
            return AdapterResult(
                model_id=model.model_id,
                status="error",
                latency_ms=latency_ms,
                error_code=ERROR_MODEL_ERROR,
                error_message="所选模型返回错误",
            )

        pred_class = data.get("pred_class")
        if not isinstance(pred_class, str) or not pred_class:
            return AdapterResult(
                model_id=model.model_id,
                status="error",
                latency_ms=latency_ms,
                error_code=ERROR_WORKER_BAD_RESPONSE,
                error_message="所选模型未返回有效 pred_class",
            )

        if pred_class not in self._classes:
            return AdapterResult(
                model_id=model.model_id,
                status="error",
                latency_ms=latency_ms,
                error_code=ERROR_INVALID_MODEL_OUTPUT,
                error_message="所选模型返回的类别不在白名单内",
            )

        return AdapterResult(
            model_id=model.model_id,
            status="ok",
            latency_ms=latency_ms,
            pred_class=pred_class,
        )
