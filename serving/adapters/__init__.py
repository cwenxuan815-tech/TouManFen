"""统一模型适配层。

对外公开：
- :class:`ModelAdapter`：网关业务层唯一依赖的协议；
- :class:`HttpWorkerAdapter`：通过 HTTP 调用本地 Flask worker；
- :class:`LlmAdapter`：LLM worker 的适配器（同样是 HTTP，但保留独立语义边界）；
- ``DATA_SOURCE_*``：B11 演示真实性三态取值（live / mock / unknown / cached）。

具体适配器由 composition root（``serving/gateway/main.py``）实例化，
其他模块不要直接依赖 ``HttpWorkerAdapter`` / ``LlmAdapter`` 具体类。
"""

from .http_worker import HttpWorkerAdapter  # noqa: F401
from .llm import LlmAdapter  # noqa: F401
from .protocol import (  # noqa: F401
    DATA_SOURCE_CACHED,
    DATA_SOURCE_LIVE,
    DATA_SOURCE_MOCK,
    DATA_SOURCE_UNKNOWN,
    AdapterResult,
    ModelAdapter,
    PredictionResult,
    ScoreEntry,
)

__all__ = [
    "ModelAdapter",
    "PredictionResult",
    "AdapterResult",
    "ScoreEntry",
    "HttpWorkerAdapter",
    "LlmAdapter",
    "DATA_SOURCE_LIVE",
    "DATA_SOURCE_MOCK",
    "DATA_SOURCE_CACHED",
    "DATA_SOURCE_UNKNOWN",
]
