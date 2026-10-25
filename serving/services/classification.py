"""分类业务服务（阶段 3）。

职责：
- 标题规范化与长度校验；
- 模型选择与启用状态校验；
- 单模型分类与多模型对比编排；
- 类别白名单校验（由适配器返回结果后统一判断）；
- 有界并发、逐模型失败隔离、日志与统计。

依赖边界：本服务只依赖 :class:`serving.adapters.protocol.ModelAdapter` 协议，
具体适配器由应用装配处（``serving/gateway/main.py``）注入，便于用 fake adapter 测试。
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping, Sequence

from ..adapters.protocol import (
    DATA_SOURCE_LIVE,
    DATA_SOURCE_UNKNOWN,
    AdapterResult,
    ModelAdapter,
    ScoreEntry,
)
from ..gateway.errors import (
    ApiError,
    ERROR_BATCH_CANCELLED,
    ERROR_BATCH_INVALID,
    ERROR_BATCH_LLM_NOT_ALLOWED,
    ERROR_BATCH_TOTAL_TIMEOUT,
    ERROR_BATCH_TOO_LARGE,
    ERROR_INTERNAL,
    ERROR_INVALID_MODELS,
    ERROR_INVALID_MODEL_OUTPUT,
    ERROR_INVALID_TITLE,
    ERROR_LLM_QUOTA_EXHAUSTED,
    ERROR_LLM_QUOTA_IDENTITY_INVALID,
    ERROR_LLM_QUOTA_UNAVAILABLE,
    ERROR_MODEL_DISABLED,
    ERROR_MODEL_ERROR,
    ERROR_TITLE_TOO_LONG,
    ERROR_UNKNOWN_MODEL,
    ERROR_WORKER_BAD_RESPONSE,
    ERROR_WORKER_HTTP_ERROR,
    ERROR_WORKER_TIMEOUT,
    ERROR_WORKER_UNAVAILABLE,
)
from ..gateway.settings import (
    BATCH_CONCURRENCY_MAX,
    BATCH_CONCURRENCY_MIN,
    BATCH_MAX_ITEMS_LIMIT,
    BATCH_MAX_TOTAL_CHARS_LIMIT,
    BATCH_TITLE_MAX_CHARS,
    DEFAULT_BATCH_CONCURRENCY,
    DEFAULT_BATCH_MAX_ITEMS,
    DEFAULT_BATCH_MAX_TOTAL_CHARS,
    DEFAULT_BATCH_TOTAL_TIMEOUT_MS,
    DEFAULT_COMPARE_CONCURRENCY,
    DEFAULT_COMPARE_TOTAL_TIMEOUT_MS,
    RejectConfig,
)
from ..gateway.stats import StatsCollector
from ..models.registry import ModelRegistry, ModelSpec
from .llm_quota import (
    LlmQuotaIdentityError,
    LlmQuotaStore,
    LlmQuotaStoreError,
    llm_quota_context,
)

#: B6：SSE 心跳间隔（注释行；不伪装 model 事件、不延长总截止，§6.6）。
COMPARE_STREAM_HEARTBEAT_SECONDS = 15.0

#: B6：客户端断连轮询间隔（避免每个事件都调用一次 is_disconnected）。
COMPARE_DISCONNECT_POLL_SECONDS = 0.2

#: 阶段 2A：批量 item_id / batch_id 的客户端关联格式。
BATCH_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
BATCH_ITEM_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

#: 阶段 2A：批量断连轮询间隔（秒）。
BATCH_DISCONNECT_POLL_SECONDS = 0.2

#: B3 软拒识原因（schema 契约，顺序即优先级：low_max_score 优先）。
REJECT_REASON_LOW_MAX_SCORE = "low_max_score"
REJECT_REASON_HIGH_ENTROPY = "high_entropy"
REJECT_ENTROPY_BASE = "e"


@dataclass(frozen=True)
class RejectDecision:
    """一次分类的软拒识判定结果（不改变 pred_class / scores / latency）。"""

    rejected: bool
    reason: str | None = None
    #: 有分数时恒为非空：阈值来源/方法/适用范围；无分数/未启用时为 None。
    threshold_meta: dict[str, Any] | None = None


def shannon_entropy(scores: list[float]) -> float:
    """归一化后的 Shannon entropy（base=e）。

    先按 ``sum(scores)`` 归一化，避免 fastText float32 归一化误差（和可能略
    大于 1）影响量纲；``sum <= 0`` 或空输入返回 0.0。
    """
    total = sum(scores)
    if total <= 0.0:
        return 0.0
    entropy = 0.0
    for value in scores:
        normalized = value / total
        if normalized > 0.0:
            entropy -= normalized * math.log(normalized)
    return entropy


def _threshold_meta(
    *,
    config: RejectConfig,
    model_id: str,
    max_score: float,
    entropy: float,
) -> dict[str, Any]:
    """构造 ``threshold_meta``（gateway schema 原样透传；键序即文档形状）。"""
    calibration = config.calibration
    return {
        # 下列 7 个键为 F6 对接的稳定契约。
        "source": calibration.source,
        "calibrated": calibration.calibrated,
        "method": calibration.method,
        "max_score_threshold": config.max_score_threshold,
        "entropy_threshold": config.entropy_threshold,
        "scope_note": calibration.scope_note,
        "sample_size": (
            calibration.positive_sample_size + calibration.negative_sample_size
        ),
        # 以下为辅助信息（可增删，不影响 F6 主契约）。
        "dataset": calibration.dataset,
        "positive_sample_size": calibration.positive_sample_size,
        "negative_sample_size": calibration.negative_sample_size,
        "seed": calibration.seed,
        "derived_for_model": calibration.derived_for_model,
        "applied_model_id": model_id,
        "rule": "max_score < max_score_threshold OR entropy > entropy_threshold",
        "entropy_base": REJECT_ENTROPY_BASE,
        "observed_max_score": round(max_score, 6),
        "observed_entropy": round(entropy, 6),
        "artifacts": {
            "roc": calibration.roc_artifact,
            "threshold_selection": calibration.threshold_selection_artifact,
        },
    }


def evaluate_reject(
    scores: tuple[ScoreEntry, ...] | None,
    model_id: str,
    config: RejectConfig | None,
) -> RejectDecision:
    """业务层软拒识判定：**只**基于模型给出的未校准分数，不抛异常。

    - 未配置/未启用/无分数：``rejected=false`` 且 ``threshold_meta=None``；
    - 有分数：``max_score < max_score_threshold`` 或
      ``entropy > entropy_threshold`` 时拒识，但仍保留调用方的 pred_class；
    - 防御性校验失败（非有限数/越界）视为“无有效分数”，不拒识、不抛错。
    """
    if config is None or not config.enabled or scores is None:
        return RejectDecision(rejected=False)
    values = [entry.score for entry in scores]
    if not values:
        return RejectDecision(rejected=False)
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return RejectDecision(rejected=False)
        number = float(value)
        if not math.isfinite(number) or not 0.0 <= number <= 1.0:
            return RejectDecision(rejected=False)

    max_score = max(values)
    entropy = shannon_entropy(values)
    if max_score < config.max_score_threshold:
        reason: str | None = REJECT_REASON_LOW_MAX_SCORE
    elif entropy > config.entropy_threshold:
        reason = REJECT_REASON_HIGH_ENTROPY
    else:
        reason = None
    return RejectDecision(
        rejected=reason is not None,
        reason=reason,
        threshold_meta=_threshold_meta(
            config=config,
            model_id=model_id,
            max_score=max_score,
            entropy=entropy,
        ),
    )


@dataclass
class ClassifyOutcome:
    model_id: str
    model_version: str | None
    pred_class: str
    latency_ms: int
    #: 仅在 worker 分数通过适配器校验时有值；否则均为 None。
    scores: tuple[ScoreEntry, ...] | None = None
    score_kind: str | None = None
    top_k: tuple[ScoreEntry, ...] | None = None
    #: B3 软拒识：即使 rejected=True 也保留 pred_class / scores / top_k。
    rejected: bool = False
    reject_reason: str | None = None
    threshold_meta: dict[str, Any] | None = None
    #: B11 演示真实性：live / mock / unknown；None = 适配器未判定（不推断 live）。
    data_source: str | None = None


@dataclass
class CompareItem:
    model_id: str
    status: str  # ok | error
    latency_ms: int
    model_version: str | None = None
    pred_class: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    #: B11 演示真实性：live / mock / unknown；None = 适配器未判定（不推断 live）。
    data_source: str | None = None


@dataclass
class CompareOutcome:
    items: list[CompareItem]
    total: int
    ok: int
    failed: int
    status: str  # ok | partial | all_failed


@dataclass(frozen=True)
class BatchItemInput:
    """网关路由传入的批量条目；title 尚未规范化。"""

    item_id: str
    title: str


@dataclass
class BatchItemOutcome:
    """批量中的一个条目的最终结果（成功或稳定失败）。"""

    item_id: str
    index: int
    status: str  # ok | error
    outcome: ClassifyOutcome | None = None
    latency_ms: int = 0
    error_code: str | None = None
    error_message: str | None = None
    error_details: dict[str, Any] | None = None
    #: 失败项没有可信来源；成功项透传适配器判定，不推断 live。
    data_source: str | None = None


@dataclass
class BatchOutcome:
    model_id: str
    items: list[BatchItemOutcome]
    total: int
    ok: int
    failed: int
    status: str  # ok | partial | all_failed
    elapsed_ms: int
    total_timeout_ms: int


# ---------------------------------------------------------------------------
# B6：/compare/stream 渐进式事件（服务层只产出事件，SSE 编码由路由层负责）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StreamModelEvent:
    """单个模型的完成事件（每个模型最多一次，按实际完成顺序产出）。

    ``status=ok`` 时 ``model_version``/``pred_class`` 有值、``error_*`` 为 None；
    ``status=error`` 时相反。逐模型失败只产生该条 error，不影响其他模型。
    """

    model_id: str
    status: str  # ok | error
    latency_ms: int
    model_version: str | None = None
    pred_class: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    #: B11 演示真实性：live / mock / unknown；None = 适配器未判定。
    data_source: str | None = None


@dataclass(frozen=True)
class StreamHeartbeatEvent:
    """SSE 心跳：路由层渲染为注释行 ``: heartbeat``，**不是** model 事件。"""


@dataclass(frozen=True)
class StreamSummaryEvent:
    """全部完成或总截止后产出一次；路由层随后补 ``done`` 并关闭流。"""

    status: str  # ok | partial | all_failed
    total: int
    ok: int
    failed: int
    elapsed_ms: int
    total_timeout_ms: int
    partial: bool


class ClassificationService:
    def __init__(
        self,
        registry: ModelRegistry,
        adapters: Mapping[str, ModelAdapter],
        classes: tuple[str, ...],
        max_title_chars: int,
        stats: StatsCollector,
        logger: logging.Logger,
        reject: RejectConfig | None = None,
        compare_total_timeout_ms: int = DEFAULT_COMPARE_TOTAL_TIMEOUT_MS,
        compare_concurrency: int = DEFAULT_COMPARE_CONCURRENCY,
        compare_heartbeat_seconds: float = COMPARE_STREAM_HEARTBEAT_SECONDS,
        compare_disconnect_poll_seconds: float = COMPARE_DISCONNECT_POLL_SECONDS,
        batch_max_items: int = DEFAULT_BATCH_MAX_ITEMS,
        batch_max_total_chars: int = DEFAULT_BATCH_MAX_TOTAL_CHARS,
        batch_concurrency: int = DEFAULT_BATCH_CONCURRENCY,
        batch_total_timeout_ms: int = DEFAULT_BATCH_TOTAL_TIMEOUT_MS,
        batch_disconnect_poll_seconds: float = BATCH_DISCONNECT_POLL_SECONDS,
        llm_quota: LlmQuotaStore | None = None,
        llm_quota_enforced: bool = False,
    ) -> None:
        self._registry = registry
        self._llm_quota = llm_quota
        # 生产 composition root 即使 store 初始化失败也设为 True，使 LLM 请求
        # 在 fanout 前 fail-closed；直接构造 service 的既有单测默认为 False。
        self._llm_quota_enforced = bool(llm_quota_enforced)
        self._adapters = dict(adapters)
        self._classes = frozenset(classes)
        self._max_title_chars = max_title_chars
        self._stats = stats
        self._logger = logger
        #: None = 未启用软拒识（测试/未装配场景），行为与 B1 完全一致。
        self._reject = reject
        # B6：总截止只由网关编排层使用；到点取消网关侧 task，不中止 worker（§6.6）。
        if compare_total_timeout_ms <= 0:
            raise ValueError("compare_total_timeout_ms 必须为正整数")
        # B12：并发上限由 settings 注入（TMF_COMPARE_CONCURRENCY，默认 3）；
        # compare 与 compare_stream 共用同一实例配置，不另设第二套并发。
        if compare_concurrency <= 0:
            raise ValueError("compare_concurrency 必须为正整数")
        if compare_heartbeat_seconds <= 0:
            raise ValueError("compare_heartbeat_seconds 必须为正数")
        if compare_disconnect_poll_seconds <= 0:
            raise ValueError("compare_disconnect_poll_seconds 必须为正数")
        self._compare_total_timeout_ms = int(compare_total_timeout_ms)
        self._compare_concurrency = int(compare_concurrency)
        self._compare_heartbeat_seconds = float(compare_heartbeat_seconds)
        self._compare_disconnect_poll_seconds = float(compare_disconnect_poll_seconds)

        # 阶段 2A：批量硬上限只能收紧；默认值与已确认规则一致。
        if not 1 <= int(batch_max_items) <= BATCH_MAX_ITEMS_LIMIT:
            raise ValueError("batch_max_items 必须在 1~100 之间")
        if not 1 <= int(batch_max_total_chars) <= BATCH_MAX_TOTAL_CHARS_LIMIT:
            raise ValueError("batch_max_total_chars 必须在 1~20000 之间")
        if not BATCH_CONCURRENCY_MIN <= int(batch_concurrency) <= BATCH_CONCURRENCY_MAX:
            raise ValueError("batch_concurrency 必须在 1~10 之间")
        if int(batch_total_timeout_ms) <= 0:
            raise ValueError("batch_total_timeout_ms 必须为正整数")
        if float(batch_disconnect_poll_seconds) <= 0:
            raise ValueError("batch_disconnect_poll_seconds 必须为正数")
        self._batch_max_items = int(batch_max_items)
        self._batch_max_total_chars = int(batch_max_total_chars)
        self._batch_concurrency = int(batch_concurrency)
        self._batch_total_timeout_ms = int(batch_total_timeout_ms)
        self._batch_disconnect_poll_seconds = float(batch_disconnect_poll_seconds)
        # 进程级共享信号量：同一 gateway 进程内所有批量请求共用，防止
        # 10 个 IP × 每请求 2 并发把个人 PC 打爆；不能在每个请求里新建。
        self._batch_semaphore = asyncio.Semaphore(self._batch_concurrency)

    @property
    def batch_total_timeout_ms(self) -> int:
        return self._batch_total_timeout_ms

    @property
    def batch_concurrency(self) -> int:
        return self._batch_concurrency

    def _validate_title(self, title: str) -> str:
        if not isinstance(title, str):
            raise ApiError(400, ERROR_INVALID_TITLE, "title 必须是字符串")
        trimmed = title.strip()
        if not trimmed:
            raise ApiError(400, ERROR_INVALID_TITLE, "title 不能为空")
        if len(trimmed) > self._max_title_chars:
            raise ApiError(
                400,
                ERROR_TITLE_TOO_LONG,
                f"title 长度不能超过 {self._max_title_chars} 个字符",
                {"max_chars": self._max_title_chars},
            )
        return trimmed

    def _resolve_one(self, model_id: str | None) -> ModelSpec:
        selected = self._registry.default_model_id if model_id is None else model_id
        spec = self._registry.get(selected)
        if spec is None:
            raise ApiError(
                404, ERROR_UNKNOWN_MODEL, "模型不存在", {"model_id": selected}
            )
        if not spec.enabled:
            raise ApiError(
                400, ERROR_MODEL_DISABLED, "模型已禁用", {"model_id": selected}
            )
        return spec

    def _resolve_many(self, models: list[str] | None) -> list[ModelSpec]:
        if models is None:
            model_ids = self._registry.compare_default_model_ids()
            if not model_ids:
                raise ApiError(400, ERROR_INVALID_MODELS, "没有可比较的默认模型")
        else:
            if not models:
                raise ApiError(400, ERROR_INVALID_MODELS, "models 不能为空")
            if len(set(models)) != len(models):
                raise ApiError(400, ERROR_INVALID_MODELS, "models 不能重复")
            model_ids = list(models)

        specs: list[ModelSpec] = []
        for model_id in model_ids:
            spec = self._registry.get(model_id)
            if spec is None:
                raise ApiError(
                    404, ERROR_UNKNOWN_MODEL, "模型不存在", {"model_id": model_id}
                )
            if not spec.enabled:
                raise ApiError(
                    400, ERROR_MODEL_DISABLED, "模型已禁用", {"model_id": model_id}
                )
            specs.append(spec)
        return specs

    def _adapter_for(self, spec: ModelSpec) -> ModelAdapter:
        adapter = self._adapters.get(spec.model_id)
        if adapter is None:
            raise ApiError(
                502, ERROR_MODEL_ERROR, "模型适配器未装配", {"model_id": spec.model_id}
            )
        return adapter

    @staticmethod
    def _is_llm_spec(spec: ModelSpec) -> bool:
        """按 model_id 或 worker_id 识别 LLM，避免未来别名绕过。"""
        return spec.model_id == "llm" or spec.worker_id == "llm"

    def _preflight_llm_quota(
        self,
        specs: Sequence[ModelSpec],
        client_ip: str | None,
    ) -> None:
        """在 compare/stream fanout 或 classify adapter 调用前检查额度。

        只读检查，不消耗额度；真正的原子 reserve 在 QuotaedLlmAdapter 内完成。
        这样并发请求仍可能抢到最后一次额度，由 adapter 的原子 reserve 拒绝。
        """
        if not self._llm_quota_enforced:
            return
        if not any(self._is_llm_spec(spec) for spec in specs):
            return
        if not client_ip:
            raise ApiError(
                403,
                ERROR_LLM_QUOTA_IDENTITY_INVALID,
                "无法确认客户端身份，本次未调用大模型。",
            )
        if self._llm_quota is None:
            raise ApiError(
                503,
                ERROR_LLM_QUOTA_UNAVAILABLE,
                "LLM 配额存储暂时不可用，本次未调用大模型。",
            )
        try:
            usage = self._llm_quota.get_usage(client_ip)
        except LlmQuotaIdentityError as exc:
            raise ApiError(
                403,
                ERROR_LLM_QUOTA_IDENTITY_INVALID,
                "无法确认客户端身份，本次未调用大模型。",
            ) from exc
        except LlmQuotaStoreError as exc:
            raise ApiError(
                503,
                ERROR_LLM_QUOTA_UNAVAILABLE,
                "LLM 配额存储暂时不可用，本次未调用大模型。",
            ) from exc
        if usage.blocked:
            raise ApiError(
                403,
                ERROR_LLM_QUOTA_EXHAUSTED,
                "该网络出口的 3 次大模型调用额度已用完；本地模型仍可使用。",
                {"limit": usage.limit, "used": usage.used},
            )

    def preflight_llm_request(
        self,
        models: list[str] | None,
        client_ip: str | None,
    ) -> None:
        """供 SSE 路由在开始 StreamingResponse 之前调用。

        只处理 LLM 配额；模型参数非法时保持原有“流内 error 事件”语义，
        不能因为预检把旧契约改成普通 HTTP 错误。
        """
        if not self._llm_quota_enforced:
            return
        try:
            specs = self._resolve_many(models)
        except ApiError:
            return
        self._preflight_llm_quota(specs, client_ip)

    @staticmethod
    def _http_status(code: str | None) -> int:
        if code == ERROR_LLM_QUOTA_EXHAUSTED:
            return 403
        if code == ERROR_LLM_QUOTA_IDENTITY_INVALID:
            return 403
        if code == ERROR_LLM_QUOTA_UNAVAILABLE:
            return 503
        if code == ERROR_WORKER_TIMEOUT:
            return 504
        if code in {
            ERROR_WORKER_UNAVAILABLE,
            ERROR_WORKER_HTTP_ERROR,
            ERROR_WORKER_BAD_RESPONSE,
            ERROR_INVALID_MODEL_OUTPUT,
        }:
            return 502
        return 502

    def _to_api_error(self, result: AdapterResult) -> ApiError:
        code = result.error_code or ERROR_MODEL_ERROR
        message = result.error_message or "所选模型调用失败"
        return ApiError(
            self._http_status(code),
            code,
            message,
            {"model_id": result.model_id},
        )

    def _record(self, result: AdapterResult, request_id: str) -> None:
        outcome = "ok" if result.status == "ok" else "error"
        # B11：运行时统计基线只计 live 调用；mock/cached/unknown 不计入，
        # 也不伪造“真实统计”（cached 仅存在于离线快照，网关运行时不产生）。
        stats_counted = result.data_source == DATA_SOURCE_LIVE
        if stats_counted:
            self._stats.record_model_call(result.model_id, outcome)
        self._logger.info(
            "model_call",
            extra={
                "event": "model_call",
                "request_id": request_id,
                "model_id": result.model_id,
                "model_status": outcome,
                "duration_ms": result.latency_ms,
                "error_code": result.error_code,
                "data_source": result.data_source,
                "stats_counted": stats_counted,
            },
        )

    async def classify(
        self,
        title: str,
        model_id: str | None,
        request_id: str,
        client_ip: str | None = None,
    ) -> ClassifyOutcome:
        clean_title = self._validate_title(title)
        spec = self._resolve_one(model_id)
        self._preflight_llm_quota([spec], client_ip)
        adapter = self._adapter_for(spec)
        with llm_quota_context(client_ip, "/api/v1/classify"):
            result = await adapter.predict(clean_title, request_id)
        self._record(result, request_id)
        if result.status != "ok":
            raise self._to_api_error(result)
        # B3：软拒识只读取适配器已校验的分数；不改变 pred_class/延迟/分数本身。
        decision = evaluate_reject(result.scores, spec.model_id, self._reject)
        return ClassifyOutcome(
            model_id=spec.model_id,
            model_version=spec.version,
            pred_class=result.pred_class or "",
            latency_ms=result.latency_ms,
            scores=result.scores,
            score_kind=result.score_kind,
            top_k=result.top_k,
            rejected=decision.rejected,
            reject_reason=decision.reason,
            threshold_meta=decision.threshold_meta,
            data_source=result.data_source,
        )

    def _prepare_batch(
        self,
        *,
        model: str,
        batch_id: str | None,
        items: Sequence[BatchItemInput],
    ) -> tuple[ModelSpec, list[tuple[int, str, str]]]:
        """整批预检：任何不合法都在调用 worker 前失败，不做部分受理。"""
        if not isinstance(model, str) or not model.strip():
            raise ApiError(
                400,
                ERROR_BATCH_INVALID,
                "model 不能为空",
                {"field": "model"},
            )
        clean_model = model.strip()
        # v1 明确禁止 LLM：必须在 _resolve_one / adapter 调用之前拒绝。
        if clean_model == "llm":
            raise ApiError(
                400,
                ERROR_BATCH_LLM_NOT_ALLOWED,
                "批量分类暂不支持大模型，请选择本地模型",
                {"model": "llm"},
            )
        if batch_id is not None:
            if not isinstance(batch_id, str) or not BATCH_ID_PATTERN.fullmatch(batch_id):
                raise ApiError(
                    400,
                    ERROR_BATCH_INVALID,
                    "batch_id 格式非法",
                    {"field": "batch_id"},
                )
        if (
            not isinstance(items, Sequence)
            or isinstance(items, (str, bytes))
            or len(items) == 0
        ):
            raise ApiError(
                400,
                ERROR_BATCH_INVALID,
                "items 必须是非空数组",
                {"field": "items"},
            )
        if len(items) > self._batch_max_items:
            raise ApiError(
                400,
                ERROR_BATCH_TOO_LARGE,
                "批次标题条数超过上限",
                {
                    "max_items": self._batch_max_items,
                    "actual_items": len(items),
                },
            )

        # 模型不存在/禁用同样在任何 worker 调用前失败。
        spec = self._resolve_one(clean_model)
        max_title_chars = min(self._max_title_chars, BATCH_TITLE_MAX_CHARS)
        normalized: list[tuple[int, str, str]] = []
        seen_ids: set[str] = set()
        invalid: list[dict[str, Any]] = []
        total_chars = 0

        for index, item in enumerate(items):
            item_id = getattr(item, "item_id", None)
            if (
                not isinstance(item_id, str)
                or not BATCH_ITEM_ID_PATTERN.fullmatch(item_id)
            ):
                invalid.append(
                    {
                        "index": index,
                        "item_id": item_id if isinstance(item_id, str) else None,
                        "reason": "invalid_item_id",
                    }
                )
                continue
            if item_id in seen_ids:
                invalid.append(
                    {
                        "index": index,
                        "item_id": item_id,
                        "reason": "duplicate_item_id",
                    }
                )
                continue
            seen_ids.add(item_id)

            title = getattr(item, "title", None)
            if not isinstance(title, str):
                invalid.append(
                    {
                        "index": index,
                        "item_id": item_id,
                        "reason": "title_not_string",
                    }
                )
                continue
            clean_title = title.strip()
            if not clean_title:
                invalid.append(
                    {
                        "index": index,
                        "item_id": item_id,
                        "reason": "empty_title",
                    }
                )
                continue
            if len(clean_title) > max_title_chars:
                invalid.append(
                    {
                        "index": index,
                        "item_id": item_id,
                        "reason": "title_too_long",
                        "max_chars": max_title_chars,
                    }
                )
                continue
            normalized.append((index, item_id, clean_title))
            total_chars += len(clean_title)

        if invalid:
            raise ApiError(
                400,
                ERROR_BATCH_INVALID,
                "批量请求包含非法条目",
                {
                    "items": invalid[:20],
                    "invalid_count": len(invalid),
                },
            )
        if total_chars > self._batch_max_total_chars:
            raise ApiError(
                400,
                ERROR_BATCH_TOO_LARGE,
                "批次标题总字符数超过上限",
                {
                    "max_total_chars": self._batch_max_total_chars,
                    "actual_total_chars": total_chars,
                    "max_items": self._batch_max_items,
                },
            )
        return spec, normalized

    async def _watch_batch_disconnect(
        self, is_disconnected: Callable[[], Awaitable[bool]]
    ) -> bool:
        """轮询客户端断连；与 compare_stream 一致，检测异常按已断开处理。"""
        while True:
            await asyncio.sleep(self._batch_disconnect_poll_seconds)
            try:
                if await is_disconnected():
                    return True
            except asyncio.CancelledError:
                raise
            except Exception:
                return True

    async def classify_batch(
        self,
        *,
        model: str,
        batch_id: str | None,
        items: Sequence[BatchItemInput],
        request_id: str,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
    ) -> BatchOutcome:
        """多条标题 × 单模型批量分类。

        边界：
        - 预检失败整批 4xx，不调用任何 worker；
        - 批次总截止到点后保留已完成结果，未完成条目标为
          ``BATCH_TOTAL_TIMEOUT``；客户端断开标为 ``BATCH_CANCELLED``；
        - 只创建 ``min(batch_concurrency, total)`` 个 worker task，且所有
          批量请求共用进程级信号量，避免一次创建 100 个任务/并发；
        - 每条复用 ``classify``（含校验/data_source/scores/top_k/reject）；
        - 不调用 ``_human_feedback_hint``，不记录标题。
        """
        spec, normalized = self._prepare_batch(
            model=model, batch_id=batch_id, items=items
        )
        total = len(normalized)
        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + (self._batch_total_timeout_ms / 1000.0)
        results: list[BatchItemOutcome | None] = [None] * total
        queue: asyncio.Queue[int] = asyncio.Queue()
        for index, _, _ in normalized:
            queue.put_nowait(index)

        stop_event = asyncio.Event()
        stop_reason: str | None = None
        workers: list[asyncio.Task[None]] = []

        async def worker() -> None:
            while not stop_event.is_set():
                try:
                    index = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                if stop_event.is_set():
                    return
                _, item_id, title = normalized[index]
                acquired = False
                try:
                    await self._batch_semaphore.acquire()
                    acquired = True
                    if stop_event.is_set():
                        return
                    try:
                        outcome = await self.classify(
                            title, spec.model_id, request_id
                        )
                    except asyncio.CancelledError:
                        raise
                    except ApiError as exc:
                        results[index] = BatchItemOutcome(
                            item_id=item_id,
                            index=index,
                            status="error",
                            latency_ms=0,
                            error_code=exc.code,
                            error_message=exc.message,
                            error_details=(
                                dict(exc.details) if exc.details else None
                            ),
                        )
                        continue
                    except Exception:
                        self._logger.exception(
                            "batch_item_failed",
                            extra={
                                "event": "error",
                                "request_id": request_id,
                                "model_id": spec.model_id,
                                "item_id": item_id,
                            },
                        )
                        results[index] = BatchItemOutcome(
                            item_id=item_id,
                            index=index,
                            status="error",
                            latency_ms=0,
                            error_code=ERROR_INTERNAL,
                            error_message="批量条目分类异常",
                        )
                        continue
                    results[index] = BatchItemOutcome(
                        item_id=item_id,
                        index=index,
                        status="ok",
                        outcome=outcome,
                        latency_ms=outcome.latency_ms,
                        data_source=outcome.data_source,
                    )
                finally:
                    if acquired:
                        self._batch_semaphore.release()

        async def monitor() -> None:
            nonlocal stop_reason
            disconnect_task: asyncio.Task[bool] | None = None
            if is_disconnected is not None:
                disconnect_task = asyncio.create_task(
                    self._watch_batch_disconnect(is_disconnected)
                )
            try:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    stop_reason = "deadline"
                elif disconnect_task is None:
                    await asyncio.sleep(remaining)
                    stop_reason = "deadline"
                else:
                    done, _ = await asyncio.wait(
                        {disconnect_task}, timeout=remaining
                    )
                    if disconnect_task in done:
                        try:
                            disconnected = disconnect_task.result()
                        except Exception:
                            disconnected = True
                        stop_reason = (
                            "disconnect" if disconnected else "deadline"
                        )
                    else:
                        stop_reason = "deadline"
            except asyncio.CancelledError:
                raise
            finally:
                if disconnect_task is not None:
                    disconnect_task.cancel()
                    await asyncio.gather(
                        disconnect_task, return_exceptions=True
                    )
                if stop_reason is not None and not stop_event.is_set():
                    stop_event.set()
                    for task in workers:
                        task.cancel()

        worker_count = min(self._batch_concurrency, total)
        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        monitor_task = asyncio.create_task(monitor())
        try:
            await asyncio.gather(*workers, return_exceptions=True)
        finally:
            monitor_task.cancel()
            await asyncio.gather(monitor_task, return_exceptions=True)

        timeout_code = (
            ERROR_BATCH_CANCELLED
            if stop_reason == "disconnect"
            else ERROR_BATCH_TOTAL_TIMEOUT
        )
        timeout_message = (
            "批次已取消"
            if stop_reason == "disconnect"
            else "批次未在总时限内完成"
        )
        items_out: list[BatchItemOutcome] = []
        for index, item_id, _ in normalized:
            result = results[index]
            if result is None:
                result = BatchItemOutcome(
                    item_id=item_id,
                    index=index,
                    status="error",
                    latency_ms=0,
                    error_code=timeout_code,
                    error_message=timeout_message,
                )
            items_out.append(result)

        ok = sum(1 for item in items_out if item.status == "ok")
        failed = total - ok
        if ok == total:
            status = "ok"
        elif ok == 0:
            status = "all_failed"
        else:
            status = "partial"
        elapsed_ms = int(round((loop.time() - started) * 1000))
        self._logger.info(
            "batch_classify",
            extra={
                "event": "batch_classify",
                "request_id": request_id,
                "batch_id": batch_id,
                "model_id": spec.model_id,
                "batch_total": total,
                "batch_ok": ok,
                "batch_failed": failed,
                "batch_status": status,
                "duration_ms": elapsed_ms,
            },
        )
        return BatchOutcome(
            model_id=spec.model_id,
            items=items_out,
            total=total,
            ok=ok,
            failed=failed,
            status=status,
            elapsed_ms=elapsed_ms,
            total_timeout_ms=self._batch_total_timeout_ms,
        )

    async def compare(
        self,
        title: str,
        models: list[str] | None,
        request_id: str,
        client_ip: str | None = None,
    ) -> CompareOutcome:
        clean_title = self._validate_title(title)
        specs = self._resolve_many(models)
        # fanout 前 fail-closed：额度已满或 store 不可用时，不能先启动本地模型。
        self._preflight_llm_quota(specs, client_ip)
        semaphore = asyncio.Semaphore(self._compare_concurrency)
        adapters = [self._adapter_for(spec) for spec in specs]

        async def run_one(adapter: ModelAdapter) -> AdapterResult:
            async with semaphore:
                return await adapter.predict(clean_title, request_id)

        with llm_quota_context(client_ip, "/api/v1/compare"):
            raw_results = await asyncio.gather(
                *(run_one(adapter) for adapter in adapters), return_exceptions=True
            )

        items: list[CompareItem] = []
        for spec, raw in zip(specs, raw_results):
            if isinstance(raw, AdapterResult):
                result = raw
            else:
                result = AdapterResult(
                    model_id=spec.model_id,
                    status="error",
                    latency_ms=0,
                    error_code=ERROR_WORKER_UNAVAILABLE,
                    error_message="所选模型调用异常",
                    # 适配器抛异常时来源无法确认：unknown，绝不猜 live。
                    data_source=DATA_SOURCE_UNKNOWN,
                )
            self._record(result, request_id)
            if result.status == "ok":
                items.append(
                    CompareItem(
                        model_id=spec.model_id,
                        status="ok",
                        latency_ms=result.latency_ms,
                        model_version=spec.version,
                        pred_class=result.pred_class,
                        data_source=result.data_source,
                    )
                )
            else:
                items.append(
                    CompareItem(
                        model_id=spec.model_id,
                        status="error",
                        latency_ms=result.latency_ms,
                        error_code=result.error_code or ERROR_INTERNAL,
                        error_message=result.error_message or "所选模型调用失败",
                        data_source=result.data_source,
                    )
                )

        total = len(items)
        ok = sum(1 for item in items if item.status == "ok")
        failed = total - ok
        if ok == total:
            status = "ok"
        elif ok == 0:
            status = "all_failed"
        else:
            status = "partial"
        return CompareOutcome(
            items=items,
            total=total,
            ok=ok,
            failed=failed,
            status=status,
        )

    # ------------------------------------------------------------------
    # B6：/compare/stream 渐进式 SSE 编排
    # ------------------------------------------------------------------
    @property
    def compare_total_timeout_ms(self) -> int:
        """B6 总截止（毫秒）：来自 ``TMF_COMPARE_TOTAL_TIMEOUT_MS`` 的装配值。"""
        return self._compare_total_timeout_ms

    @property
    def compare_concurrency(self) -> int:
        """B12 并发上限：来自 ``TMF_COMPARE_CONCURRENCY``（默认 3）的装配值。"""
        return self._compare_concurrency

    async def _run_stream_model(
        self,
        spec: ModelSpec,
        adapter: ModelAdapter,
        title: str,
        request_id: str,
        semaphore: asyncio.Semaphore,
    ) -> AdapterResult:
        """调用单个模型并归一化异常（不向编排层抛出，逐模型失败隔离）。"""
        try:
            async with semaphore:
                return await adapter.predict(title, request_id)
        except asyncio.CancelledError:
            # 网关侧取消/总截止：直接向上传播，由编排层统一清理。
            raise
        except Exception:
            # 适配器契约要求不抛异常；这里兜底，日志不记录标题。
            self._logger.exception(
                "model_adapter_exception",
                extra={
                    "event": "error",
                    "request_id": request_id,
                    "model_id": spec.model_id,
                },
            )
            return AdapterResult(
                model_id=spec.model_id,
                status="error",
                latency_ms=0,
                error_code=ERROR_WORKER_UNAVAILABLE,
                error_message="所选模型调用异常",
                data_source=DATA_SOURCE_UNKNOWN,
            )

    @staticmethod
    async def _wait_disconnect(
        is_disconnected: Callable[[], Awaitable[bool]],
        poll_seconds: float,
    ) -> bool:
        """轮询客户端断连；检测本身异常时按“已断开”处理（停止 push 最安全）。"""
        while True:
            await asyncio.sleep(poll_seconds)
            try:
                if await is_disconnected():
                    return True
            except asyncio.CancelledError:
                raise
            except Exception:
                return True

    @staticmethod
    def _model_event(spec: ModelSpec, result: AdapterResult) -> StreamModelEvent:
        if result.status == "ok":
            return StreamModelEvent(
                model_id=spec.model_id,
                status="ok",
                latency_ms=result.latency_ms,
                model_version=spec.version,
                pred_class=result.pred_class,
                data_source=result.data_source,
            )
        return StreamModelEvent(
            model_id=spec.model_id,
            status="error",
            latency_ms=result.latency_ms,
            model_version=None,
            pred_class=None,
            error_code=result.error_code or ERROR_INTERNAL,
            error_message=result.error_message or "所选模型调用失败",
            data_source=result.data_source,
        )

    async def compare_stream(
        self,
        title: str,
        models: list[str] | None,
        request_id: str,
        *,
        is_disconnected: Callable[[], Awaitable[bool]] | None = None,
        client_ip: str | None = None,
    ) -> AsyncIterator[StreamModelEvent | StreamHeartbeatEvent | StreamSummaryEvent]:
        """流式多模型对比：逐条产出事件（B6 / §6.6）。

        契约：
        - 每个模型最多一条 ``model`` 事件，按实际完成顺序产出；
        - 每 ``compare_heartbeat_seconds``（默认 15s）产出一条心跳，不延长总截止；
        - 全部完成或总截止后产出一条 ``summary``（已完成结果保留，未完成按失败计）；
        - 客户端断连 / 生成器被关闭（``aclose``）时停止产出，并取消网关侧未完成
          task；已发出的 worker 请求不强制中止（§6.6 本期限制）。

        请求级校验错误（标题/模型参数）以 :class:`ApiError` 抛出，由路由层转成
        SSE ``error`` 事件；逐模型失败只体现在该条 ``model`` 事件的 ``status=error``。
        """
        clean_title = self._validate_title(title)
        specs = self._resolve_many(models)
        self._preflight_llm_quota(specs, client_ip)
        adapters = [self._adapter_for(spec) for spec in specs]

        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + (self._compare_total_timeout_ms / 1000.0)
        total = len(specs)
        ok = 0
        # B12：并发上限由实例配置注入（TMF_COMPARE_CONCURRENCY，默认 3）；
        # 与 POST /compare 共用同一配置，不另设第二套并发/截止。
        semaphore = asyncio.Semaphore(self._compare_concurrency)

        # ContextVar 在 create_task 时被复制给任务，确保 LLM wrapper 看到同一 IP。
        with llm_quota_context(client_ip, "/api/v1/compare/stream"):
            pending: dict[asyncio.Task, ModelSpec] = {
                asyncio.create_task(
                    self._run_stream_model(
                        spec, adapter, clean_title, request_id, semaphore
                    )
                ): spec
                for spec, adapter in zip(specs, adapters)
            }
        heartbeat_task: asyncio.Task | None = asyncio.create_task(
            asyncio.sleep(self._compare_heartbeat_seconds)
        )
        disconnect_task: asyncio.Task | None = (
            asyncio.create_task(
                self._wait_disconnect(
                    is_disconnected, self._compare_disconnect_poll_seconds
                )
            )
            if is_disconnected is not None
            else None
        )
        #: 生成器被取消（含 GeneratorExit）时不能在 finally 里 await，
        #: 否则 anyio 取消作用域会把清理再次打断。
        closed = False
        try:
            while pending:
                remaining = deadline - loop.time()
                if remaining <= 0.0:
                    break
                waiters: set[asyncio.Task] = set(pending)
                if heartbeat_task is not None:
                    waiters.add(heartbeat_task)
                if disconnect_task is not None:
                    waiters.add(disconnect_task)
                done, _ = await asyncio.wait(
                    waiters,
                    timeout=remaining,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    # 总截止到点：保留已完成结果，未完成 task 退出循环后取消。
                    break
                if disconnect_task is not None and disconnect_task in done:
                    disconnected = bool(disconnect_task.result())
                    disconnect_task = None
                    if disconnected:
                        # 客户端已断开：停止 push，由 finally 取消未完成 task。
                        return
                    disconnect_task = asyncio.create_task(
                        self._wait_disconnect(
                            is_disconnected, self._compare_disconnect_poll_seconds
                        )
                    )
                if heartbeat_task is not None and heartbeat_task in done:
                    heartbeat_task = asyncio.create_task(
                        asyncio.sleep(self._compare_heartbeat_seconds)
                    )
                    yield StreamHeartbeatEvent()
                for task in [item for item in done if item in pending]:
                    spec = pending.pop(task)
                    try:
                        result = task.result()
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        result = AdapterResult(
                            model_id=spec.model_id,
                            status="error",
                            latency_ms=0,
                            error_code=ERROR_WORKER_UNAVAILABLE,
                            error_message="所选模型调用异常",
                            data_source=DATA_SOURCE_UNKNOWN,
                        )
                    self._record(result, request_id)
                    if result.status == "ok":
                        ok += 1
                    yield self._model_event(spec, result)

            # 总截止到点（或循环结束仍有未完成）：网关侧必须取消并收尾。
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                pending.clear()

            # total 为请求的模型总数；failed 含错误与总截止时未完成的模型。
            failed = total - ok
            if ok == total:
                status = "ok"
            elif ok == 0:
                status = "all_failed"
            else:
                status = "partial"
            yield StreamSummaryEvent(
                status=status,
                total=total,
                ok=ok,
                failed=failed,
                elapsed_ms=int(round((loop.time() - started) * 1000)),
                total_timeout_ms=self._compare_total_timeout_ms,
                partial=(status == "partial"),
            )
        except (asyncio.CancelledError, GeneratorExit):
            closed = True
            raise
        finally:
            # 网关侧 task 一律取消：不等待已开始的 worker 请求（§6.6）。
            for task in pending:
                task.cancel()
            if heartbeat_task is not None:
                heartbeat_task.cancel()
            if disconnect_task is not None:
                disconnect_task.cancel()
            if pending and not closed:
                await asyncio.gather(*pending, return_exceptions=True)
            pending.clear()
