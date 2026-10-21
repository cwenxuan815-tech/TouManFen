"""网关侧模型适配层协议。

层次关系：

    FastAPI 路由 -> ClassificationService（业务编排）
                              |
                              v
                      ModelAdapter（协议）
                        /            \\
            HttpWorkerAdapter      LlmAdapter
                        |                |
                        +---- HTTP ------+--> 本地 Flask worker（:8101~:8106）

约束：
- 适配器只做"调用 + 结果/错误归一化"，**不加载模型**、不读密钥；
- 具体适配器只在 composition root（``serving/gateway/main.py`` 的 lifespan）实例化；
- ``ClassificationService`` 只依赖本协议，便于用 fake adapter 做测试。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable


@dataclass(frozen=True)
class PredictionResult:
    """成功预测的最小结果：类别 + 网关侧观测到的耗时（毫秒）。"""

    pred_class: str
    latency_ms: int


@dataclass(frozen=True)
class AdapterResult:
    """适配器统一返回：成功时带 ``pred_class``，失败时带错误码/脱敏消息。"""

    model_id: str
    status: Literal["ok", "error"]
    latency_ms: int
    pred_class: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@runtime_checkable
class ModelAdapter(Protocol):
    """单模型适配器契约：一个实例绑定一个已注册模型。"""

    model_id: str
    model_version: str | None

    async def predict(self, title: str, request_id: str) -> AdapterResult:
        """调用模型并返回归一化结果；不得向调用方抛出未归一化的异常。"""
        ...
