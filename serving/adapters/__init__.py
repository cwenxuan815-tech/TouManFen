"""统一模型适配层。

对外公开：
- :class:`ModelAdapter`：网关业务层唯一依赖的协议；
- :class:`HttpWorkerAdapter`：通过 HTTP 调用本地 Flask worker；
- :class:`LlmAdapter`：LLM worker 的适配器（同样是 HTTP，但保留独立语义边界）。

具体适配器由 composition root（``serving/gateway/main.py``）实例化，
其他模块不要直接依赖 ``HttpWorkerAdapter`` / ``LlmAdapter`` 具体类。
"""

from .http_worker import HttpWorkerAdapter  # noqa: F401
from .llm import LlmAdapter  # noqa: F401
from .protocol import AdapterResult, ModelAdapter, PredictionResult  # noqa: F401

__all__ = [
    "ModelAdapter",
    "PredictionResult",
    "AdapterResult",
    "HttpWorkerAdapter",
    "LlmAdapter",
]
