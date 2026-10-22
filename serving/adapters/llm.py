"""LLM 适配器。

为什么需要单独的 ``LlmAdapter``：
- LLM 仍是**本地 Flask worker**（:8105），不是网关直连 Provider——
  API Key 只存在于 ``04-LLM/.env`` 与 LLM worker 进程中，网关不读取密钥；
- 但 LLM 的调用策略与其他模型不同（更短的总预算、关闭重试、输出白名单、
  opt-in 语义），把这一层语义显式建模，避免出现两个职责重复的 HTTP adapter。

实现上复用 ``HttpWorkerAdapter`` 的传输/错误归一化逻辑，只增加 LLM 边界标识，
保证网关侧行为与阶段 3/4A 完全一致。

B11：``data_source`` 判定同样继承自 ``HttpWorkerAdapter``——即通过 LLM worker
``/metadata.display_name`` 的 ``mock-`` 前缀区分 mock 与 live；不读取 Provider
密钥、不发起任何上游调用。
"""

from __future__ import annotations

from .http_worker import HttpWorkerAdapter


class LlmAdapter(HttpWorkerAdapter):
    """LLM worker 适配器（仅本地 HTTP 调用，不读取 Provider 密钥）。"""

    #: 供装配/日志区分适配器类型；不参与业务逻辑
    adapter_kind = "llm"
