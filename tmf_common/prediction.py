"""worker 侧统一预测服务契约。

五个模型（RF / fastText / BERT / LLM / 蒸馏）各自处理加载、预处理与推理，
但对外暴露统一的模板流程：

    normalize_title -> infer -> validate_prediction -> PredictionResult

Flask 入口只负责把 JSON 交给 ``handle_payload`` 并把返回字典序列化，
不重复实现标题校验、白名单校验或模型调用。

约束：本模块只依赖标准库，不得导入 torch/transformers/fasttext/openai 等重库。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Sequence

DEFAULT_TITLE_ERROR = "传递的参数中没有title字段"


@dataclass(frozen=True)
class PredictionResult:
    """单次预测结果：类别 + 推理耗时（毫秒）。"""

    pred_class: str
    latency_ms: float


class PredictionError(Exception):
    """可序列化的预测错误（由 Flask 入口映射为兼容的错误 JSON）。"""

    code = "PREDICTION_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.message = message
        if code:
            self.code = code
        super().__init__(message)


class TitleValidationError(PredictionError):
    code = "INVALID_TITLE"


class ModelOutputError(PredictionError):
    code = "INVALID_MODEL_OUTPUT"


class ModelNotReadyError(PredictionError):
    code = "MODEL_NOT_READY"


class InferenceError(PredictionError):
    code = "MODEL_ERROR"


class PredictService(ABC):
    """单一模型的预测服务基类（模板方法）。

    子类必须提供 ``model_id``、``display_name`` 与 ``infer``；
    需要区分错误格式的模型（如 LLM）可覆盖 ``empty_title_message`` /
    ``format_error`` / ``error_sets_empty_class`` / ``ready_payload``。
    """

    model_id: str = ""
    display_name: str = ""
    version: str | None = None

    #: 缺少/空白 title 时的错误消息（保持与各 worker 历史行为一致）
    empty_title_message: str = DEFAULT_TITLE_ERROR
    #: 非 title 错误时是否把 pred_class 置空（LLM 历史行为）
    error_sets_empty_class: bool = False

    def __init__(self, classes: Sequence[str]) -> None:
        if not self.model_id:
            raise ValueError("PredictService 子类必须定义 model_id")
        if not self.display_name:
            raise ValueError("PredictService 子类必须定义 display_name")
        self.class_list: tuple[str, ...] = tuple(str(item) for item in classes)
        self._classes = frozenset(self.class_list)

    # ------------------------------------------------------------------
    # 模板流程
    # ------------------------------------------------------------------
    def predict(self, title: str) -> PredictionResult:
        """统一入口：校验标题 -> 推理 -> 白名单校验 -> 返回结果。"""
        clean_title = self.normalize_title(title)
        if not self.is_ready():
            raise ModelNotReadyError("模型或客户端尚未就绪")
        started = time.perf_counter()
        pred_class = self.infer(clean_title)
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        self.validate_prediction(pred_class)
        return PredictionResult(pred_class=pred_class, latency_ms=latency_ms)

    def normalize_title(self, title: Any) -> str:
        """标题基础校验；子类可覆盖（例如 LLM 的历史错误文案）。"""
        if not isinstance(title, str) or not title:
            raise TitleValidationError(self.empty_title_message)
        return title

    @abstractmethod
    def infer(self, title: str) -> str:
        """执行模型推理并返回类别名；实现方自行完成预处理。"""

    def validate_prediction(self, pred_class: Any) -> None:
        if not isinstance(pred_class, str) or pred_class not in self._classes:
            raise ModelOutputError("模型输出不在类别白名单内")

    # ------------------------------------------------------------------
    # readiness / metadata / 兼容 JSON
    # ------------------------------------------------------------------
    def is_ready(self) -> bool:
        """模型/客户端是否可服务；默认在构造完成后即视为就绪。"""
        return True

    def ready_payload(self) -> dict[str, Any]:
        return {"status": "ready", "model_id": self.model_id}

    def metadata(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "display_name": self.display_name,
            "version": self.version,
        }

    def format_error(self, exc: PredictionError) -> str:
        """错误 JSON 中的 error 字段格式；LLM 覆盖为 ``CODE: message``。"""
        return exc.message

    def format_title_error(self, exc: TitleValidationError) -> str:
        """缺 title 的错误文案（历史行为：不带错误码前缀）。"""
        return exc.message

    def handle_payload(self, payload: Any) -> dict[str, Any]:
        """把请求 JSON 字典转换为历史兼容的响应字典。

        - 成功：原字段 + ``pred_class``；
        - 缺 title：原字段 + ``error``（不改变更多字段）；
        - 其他预测错误：原字段 + ``error``，LLM 额外把 ``pred_class`` 置空。
        """
        data: dict[str, Any] = dict(payload) if isinstance(payload, dict) else {}
        try:
            result = self.predict(data.get("title"))
        except TitleValidationError as exc:
            data["error"] = self.format_title_error(exc)
            return data
        except PredictionError as exc:
            data["error"] = self.format_error(exc)
            if self.error_sets_empty_class:
                data["pred_class"] = ""
            return data
        data["pred_class"] = result.pred_class
        return data
