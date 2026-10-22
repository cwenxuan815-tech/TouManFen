"""worker 侧统一预测服务契约。

五个模型（RF / fastText / BERT / LLM / 蒸馏）各自处理加载、预处理与推理，
但对外暴露统一的模板流程：

    normalize_title -> infer -> validate_prediction -> PredictionResult

Flask 入口只负责把 JSON 交给 ``handle_payload`` 并把返回字典序列化，
不重复实现标题校验、白名单校验或模型调用。

分数契约（B1）：
- 本地模型可在 ``ModelOutput`` 中返回 ``scores``（与 ``class_list`` 索引一一对应）
  与 ``score_kind``；LLM 无本地概率输出，两者均为 ``None``；
- ``handle_payload`` 仅在分数“存在且有效”时附加 ``scores`` / ``score_kind`` /
  ``top_k``，无效或缺失时保持旧响应字段不变（不报错、不影响 ``pred_class``）；
- ``score_kind`` 只描述“未校准”的原始分数来源，不是校准概率，也不得跨模型比较。

约束：本模块只依赖标准库，不得导入 torch/transformers/fasttext/openai 等重库。
"""

from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Literal, Sequence, get_args

DEFAULT_TITLE_ERROR = "传递的参数中没有title字段"

#: top-k 默认取前 3 类；k 必须是 >=1 的整数，超过类别数时返回全部类别。
DEFAULT_TOP_K = 3

#: 未校准分数来源标识（不是校准概率，也不得跨模型比较绝对值）。
ScoreKind = Literal["raw_proba", "raw_fasttext", "raw_softmax"]
SCORE_KIND_RAW_PROBA: ScoreKind = "raw_proba"
SCORE_KIND_RAW_FASTTEXT: ScoreKind = "raw_fasttext"
SCORE_KIND_RAW_SOFTMAX: ScoreKind = "raw_softmax"
SCORE_KINDS = frozenset(get_args(ScoreKind))

#: 原始概率/softmax 的合法取值范围（L1 未校准分数）。
SCORE_MIN = 0.0
SCORE_MAX = 1.0


@dataclass(frozen=True)
class ModelOutput:
    """模型推理的原始输出：类别 + 可选的未校准分数。

    ``scores`` 必须与 ``PredictService.class_list`` 索引一一对应
    （第 i 个值对应第 i 个类别）；无法一一对应时应返回 ``None``，
    不得用 argmax 重算或编造替代分数。
    """

    pred_class: str
    scores: tuple[float, ...] | None = None
    score_kind: str | None = None


@dataclass(frozen=True)
class PredictionResult:
    """单次预测结果：类别 + 推理耗时（毫秒）+ 可选的未校准分数。"""

    pred_class: str
    latency_ms: float
    scores: tuple[float, ...] | None = None
    score_kind: str | None = None

    def top_k(
        self, classes: Sequence[str], k: int = DEFAULT_TOP_K
    ) -> list[dict[str, Any]]:
        """生成 top-k：分数降序；并列分数按类别名升序（稳定、可测试）。

        - ``k`` 必须是 >=1 的整数（布尔/字符串/浮点一律抛 ``ValueError``）；
        - ``k`` 大于类别数时返回全部类别（不足即全量，不报错）；
        - 无分数时返回空列表。
        """
        if isinstance(k, bool) or not isinstance(k, int) or k < 1:
            raise ValueError("k 必须是 >= 1 的整数")
        scores = self.scores
        if scores is None:
            return []
        if len(scores) != len(classes):
            raise ValueError("scores 数量必须等于类别数")
        ranked = sorted(
            range(len(classes)),
            key=lambda index: (-scores[index], classes[index]),
        )
        return [
            {"class_id": classes[index], "score": scores[index]}
            for index in ranked[:k]
        ]


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
        output = self.infer(clean_title)
        latency_ms = round((time.perf_counter() - started) * 1000, 2)
        self.validate_prediction(output.pred_class)
        return PredictionResult(
            pred_class=output.pred_class,
            latency_ms=latency_ms,
            scores=output.scores,
            score_kind=output.score_kind,
        )

    def normalize_title(self, title: Any) -> str:
        """标题基础校验；子类可覆盖（例如 LLM 的历史错误文案）。"""
        if not isinstance(title, str) or not title:
            raise TitleValidationError(self.empty_title_message)
        return title

    @abstractmethod
    def infer(self, title: str) -> ModelOutput:
        """执行模型推理并返回类别与可选分数；实现方自行完成预处理。"""

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

    def _score_fields(self, result: PredictionResult) -> dict[str, Any]:
        """把有效分数转换为兼容 JSON 字段；缺失/无效时返回空字典。

        校验项：分数数量等于类别数、每个值是有限数值、范围在 [0, 1]、
        ``score_kind`` 属于契约枚举、``pred_class`` 是最高分类别之一。
        任何一项不满足都只丢弃分数信息，不影响 ``pred_class`` 与成功响应。
        """
        scores = result.scores
        kind = result.score_kind
        if scores is None or kind not in SCORE_KINDS:
            return {}
        if len(scores) != len(self.class_list):
            return {}
        clean: list[float] = []
        for raw in scores:
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                return {}
            value = float(raw)
            if not math.isfinite(value) or not (SCORE_MIN <= value <= SCORE_MAX):
                return {}
            clean.append(value)
        if result.pred_class not in self._classes:
            return {}
        pred_index = self.class_list.index(result.pred_class)
        if clean[pred_index] != max(clean):
            return {}
        return {
            "scores": {
                class_id: clean[index]
                for index, class_id in enumerate(self.class_list)
            },
            "score_kind": kind,
            "top_k": result.top_k(self.class_list, DEFAULT_TOP_K),
        }

    def handle_payload(self, payload: Any) -> dict[str, Any]:
        """把请求 JSON 字典转换为历史兼容的响应字典。

        - 成功：原字段 + ``pred_class``；分数有效时再附加
          ``scores`` / ``score_kind`` / ``top_k``；
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
        data.update(self._score_fields(result))
        return data
