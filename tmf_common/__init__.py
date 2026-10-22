"""TMF 共享契约与配置（零重依赖）。

本包被网关与 5 个模型 worker 共享：
- :mod:`tmf_common.config`：监听地址、端口、URL、CORS 的环境变量解析与校验；
- :mod:`tmf_common.prediction`：worker 侧统一的预测服务契约；
- :mod:`tmf_common.flask_shell`：worker 侧薄 Flask 入口（仅 worker 环境导入 Flask）。

约束：本包不得导入 torch/transformers/fasttext/openai/fastapi/httpx 等重依赖，
以保证每个 worker 可以在自己独立的依赖环境中导入。
"""

from .config import (  # noqa: F401
    DEFAULT_FRONTEND_PORT,
    DEFAULT_GATEWAY_PORT,
    DEFAULT_HOST,
    NetworkConfigError,
    NetworkSettings,
    load_network_settings,
    resolve_bind_host,
    resolve_cors_origins,
    resolve_frontend_host,
    resolve_frontend_port,
    resolve_gateway_host,
    resolve_gateway_port,
    validate_host,
    validate_http_url,
    validate_origin,
    validate_port,
)
from .prediction import (  # noqa: F401
    InferenceError,
    ModelNotReadyError,
    ModelOutput,
    ModelOutputError,
    PredictionError,
    PredictionResult,
    PredictService,
    TitleValidationError,
)

__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_GATEWAY_PORT",
    "DEFAULT_FRONTEND_PORT",
    "NetworkConfigError",
    "NetworkSettings",
    "load_network_settings",
    "resolve_bind_host",
    "resolve_gateway_host",
    "resolve_gateway_port",
    "resolve_frontend_host",
    "resolve_frontend_port",
    "resolve_cors_origins",
    "validate_host",
    "validate_port",
    "validate_http_url",
    "validate_origin",
    "PredictionResult",
    "ModelOutput",
    "PredictionError",
    "TitleValidationError",
    "ModelOutputError",
    "ModelNotReadyError",
    "InferenceError",
    "PredictService",
]
