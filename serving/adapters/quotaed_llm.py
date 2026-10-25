"""阶段 4B：所有网关 LLM adapter 调用的统一配额守卫。

包装真实 ``LlmAdapter``；每次 ``predict`` 先从请求上下文取可信客户端 IP，
在发出 worker HTTP 请求前原子 reserve 一次额度：

- reserve 成功后才调用 inner adapter；
- 额度耗尽返回 ``LLM_QUOTA_EXHAUSTED``（后续调用层映射 HTTP 403）；
- store 缺失/不可用返回 ``LLM_QUOTA_UNAVAILABLE``（映射 503）；
- 身份缺失/非法返回 ``LLM_QUOTA_IDENTITY_INVALID``（映射 403）；
- 不读取标题/Provider 密钥，不写日志，不做重试或退款。

新的网关 LLM 路径只要从 composition root 取得这个包装器，就会自动受配额约束；
直接调用 ``LlmAdapter`` 或在 worker 端口外调用属于既有边界，不在此包装器的保护内。
"""

from __future__ import annotations

from .protocol import AdapterResult, DATA_SOURCE_UNKNOWN, ModelAdapter
from ..gateway.errors import (
    ERROR_LLM_QUOTA_EXHAUSTED,
    ERROR_LLM_QUOTA_IDENTITY_INVALID,
    ERROR_LLM_QUOTA_UNAVAILABLE,
)
from ..services.llm_quota import (
    LlmQuotaError,
    LlmQuotaIdentityError,
    LlmQuotaStore,
    LlmQuotaStoreError,
    current_llm_quota_context,
)


class QuotaedLlmAdapter:
    """LLM adapter 的统一配额包装器。"""

    adapter_kind = "llm_quota"

    def __init__(
        self,
        inner: ModelAdapter,
        quota_store: LlmQuotaStore | None,
    ) -> None:
        self._inner = inner
        self._quota_store = quota_store
        self.model_id = inner.model_id
        self.model_version = inner.model_version

    def _failure(self, code: str, message: str) -> AdapterResult:
        return AdapterResult(
            model_id=self.model_id,
            status="error",
            latency_ms=0,
            error_code=code,
            error_message=message,
            data_source=DATA_SOURCE_UNKNOWN,
        )

    async def predict(self, title: str, request_id: str) -> AdapterResult:
        context = current_llm_quota_context()
        if context is None or not context.client_ip:
            return self._failure(
                ERROR_LLM_QUOTA_IDENTITY_INVALID,
                "无法确认客户端身份，本次未调用大模型。",
            )
        if self._quota_store is None:
            return self._failure(
                ERROR_LLM_QUOTA_UNAVAILABLE,
                "LLM 配额存储暂时不可用，本次未调用大模型。",
            )
        try:
            reservation = self._quota_store.reserve(
                context.client_ip,
                request_id=request_id,
                route=context.route,
            )
        except LlmQuotaIdentityError:
            return self._failure(
                ERROR_LLM_QUOTA_IDENTITY_INVALID,
                "无法确认客户端身份，本次未调用大模型。",
            )
        except (LlmQuotaStoreError, LlmQuotaError):
            return self._failure(
                ERROR_LLM_QUOTA_UNAVAILABLE,
                "LLM 配额存储暂时不可用，本次未调用大模型。",
            )
        if not reservation.allowed:
            return self._failure(
                ERROR_LLM_QUOTA_EXHAUSTED,
                "该网络出口的 3 次大模型调用额度已用完；本地模型仍可使用。",
            )
        return await self._inner.predict(title, request_id)
