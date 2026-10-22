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

#: B11 演示真实性三态（+ unknown 兜底）。判定依据只有 worker 的
#: ``/metadata.display_name`` 约定：以 ``mock-`` 开头 → ``mock``，否则 ``live``；
#: 取不到 metadata / 不合法 → ``unknown``（**不得推断为 live**）。
#: ``cached`` 只出现在离线快照产物里，运行中的网关不会返回它。
DATA_SOURCE_LIVE = "live"
DATA_SOURCE_MOCK = "mock"
DATA_SOURCE_CACHED = "cached"
DATA_SOURCE_UNKNOWN = "unknown"

#: 运行中适配器可能返回的 data_source 取值（cached 不在此列）。
RUNTIME_DATA_SOURCES = frozenset(
    {DATA_SOURCE_LIVE, DATA_SOURCE_MOCK, DATA_SOURCE_UNKNOWN}
)


def data_source_from_display_name(value: object) -> str:
    """B11 唯一判定规则：``mock-`` 前缀 → mock，其它有效字符串 → live。

    缺失/空/非字符串 → unknown（绝不从“worker 可达”推断 live）。
    """
    if not isinstance(value, str) or not value.strip():
        return DATA_SOURCE_UNKNOWN
    if value.strip().lower().startswith("mock-"):
        return DATA_SOURCE_MOCK
    return DATA_SOURCE_LIVE


@dataclass(frozen=True)
class PredictionResult:
    """成功预测的最小结果：类别 + 网关侧观测到的耗时（毫秒）。"""

    pred_class: str
    latency_ms: int


@dataclass(frozen=True)
class ScoreEntry:
    """一个类别及其未校准分数（``scores`` 与 ``top_k`` 的条目）。

    以 ``class_id`` 显式配对，避免依赖 worker 与网关的类别顺序一致。
    """

    class_id: str
    score: float


@dataclass(frozen=True)
class AdapterResult:
    """适配器统一返回：成功时带 ``pred_class``，失败时带错误码/脱敏消息。

    ``scores`` / ``score_kind`` / ``top_k`` 只在 worker 返回的分数通过全部
    校验时才有值；任何分数异常都降级为 ``None``，不影响 ``status=ok`` 与
    ``pred_class``。

    ``data_source``（B11）：
    - ``live`` / ``mock`` / ``unknown``：由 HTTP 适配器根据 worker
      ``/metadata.display_name`` 约定判定（``mock-`` 前缀 → mock）；
    - ``None``：适配器未实现该判定（例如测试 fake adapter），**不得当作 live**。
    """

    model_id: str
    status: Literal["ok", "error"]
    latency_ms: int
    pred_class: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    scores: tuple[ScoreEntry, ...] | None = None
    score_kind: str | None = None
    top_k: tuple[ScoreEntry, ...] | None = None
    data_source: str | None = None


@runtime_checkable
class ModelAdapter(Protocol):
    """单模型适配器契约：一个实例绑定一个已注册模型。"""

    model_id: str
    model_version: str | None

    async def predict(self, title: str, request_id: str) -> AdapterResult:
        """调用模型并返回归一化结果；不得向调用方抛出未归一化的异常。"""
        ...
