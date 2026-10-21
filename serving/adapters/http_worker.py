"""HTTP worker 适配器。

职责：
- 通过 HTTP 调用某个本地 Flask worker 的 ``predict_path``；
- 复用应用级 ``httpx.AsyncClient``（不按请求创建，避免连接泄漏/代理问题）；
- 把连接失败、超时、非 200、非法 JSON、缺字段与非法类别归一化为
  ``AdapterResult``，保持阶段 3 的错误码与脱敏消息不变；
- **不加载 torch/transformers/fasttext/模型文件，也不读取密钥**。
"""

from __future__ import annotations

import time
from typing import Sequence

import httpx

from ..gateway.errors import (
    ERROR_INVALID_MODEL_OUTPUT,
    ERROR_MODEL_ERROR,
    ERROR_WORKER_BAD_RESPONSE,
    ERROR_WORKER_HTTP_ERROR,
    ERROR_WORKER_TIMEOUT,
    ERROR_WORKER_UNAVAILABLE,
)
from ..models.registry import ModelSpec
from .protocol import AdapterResult


class HttpWorkerAdapter:
    """单个模型的 HTTP worker 适配器。"""

    def __init__(
        self,
        spec: ModelSpec,
        classes: Sequence[str],
        client: httpx.AsyncClient,
    ) -> None:
        self.model_id = spec.model_id
        self.model_version = spec.version
        self._spec = spec
        self._classes = frozenset(classes)
        self._client = client

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------
    def _url(self) -> str:
        worker = self._spec.worker
        return worker.base_url.rstrip("/") + worker.predict_path

    def _timeout(self) -> httpx.Timeout:
        seconds = self._spec.worker.timeout_ms / 1000.0
        return httpx.Timeout(seconds, connect=min(0.5, seconds))

    # ------------------------------------------------------------------
    # 协议实现
    # ------------------------------------------------------------------
    async def predict(self, title: str, request_id: str) -> AdapterResult:
        started = time.perf_counter()
        try:
            response = await self._client.post(
                self._url(),
                json={"title": title},
                headers={"X-Request-ID": request_id},
                timeout=self._timeout(),
            )
        except httpx.ConnectTimeout:
            # 连接阶段超时 = 不可达，而不是“模型处理超时”
            return self._error(ERROR_WORKER_UNAVAILABLE, "所选模型服务不可达", started)
        except httpx.TimeoutException:
            return self._error(
                ERROR_WORKER_TIMEOUT, "所选模型未在配置的时间内返回", started
            )
        except httpx.HTTPError:
            return self._error(ERROR_WORKER_UNAVAILABLE, "所选模型服务不可达", started)
        except Exception:
            return self._error(ERROR_WORKER_UNAVAILABLE, "所选模型调用异常", started)

        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            return AdapterResult(
                model_id=self.model_id,
                status="error",
                latency_ms=latency_ms,
                error_code=ERROR_WORKER_HTTP_ERROR,
                error_message="所选模型服务返回非 200 状态",
            )

        try:
            data = response.json()
        except Exception:
            return self._bad_response(latency_ms, "所选模型返回格式不符合契约")

        if not isinstance(data, dict):
            return self._bad_response(latency_ms, "所选模型返回格式不符合契约")

        if "error" in data:
            return AdapterResult(
                model_id=self.model_id,
                status="error",
                latency_ms=latency_ms,
                error_code=ERROR_MODEL_ERROR,
                error_message="所选模型返回错误",
            )

        pred_class = data.get("pred_class")
        if not isinstance(pred_class, str) or not pred_class:
            return self._bad_response(latency_ms, "所选模型未返回有效 pred_class")

        if pred_class not in self._classes:
            return AdapterResult(
                model_id=self.model_id,
                status="error",
                latency_ms=latency_ms,
                error_code=ERROR_INVALID_MODEL_OUTPUT,
                error_message="所选模型返回的类别不在白名单内",
            )

        return AdapterResult(
            model_id=self.model_id,
            status="ok",
            latency_ms=latency_ms,
            pred_class=pred_class,
        )

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _error(self, code: str, message: str, started: float) -> AdapterResult:
        return AdapterResult(
            model_id=self.model_id,
            status="error",
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_code=code,
            error_message=message,
        )

    def _bad_response(self, latency_ms: int, message: str) -> AdapterResult:
        return AdapterResult(
            model_id=self.model_id,
            status="error",
            latency_ms=latency_ms,
            error_code=ERROR_WORKER_BAD_RESPONSE,
            error_message=message,
        )
