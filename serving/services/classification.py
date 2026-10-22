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
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Mapping

from ..adapters.protocol import (
    DATA_SOURCE_LIVE,
    DATA_SOURCE_UNKNOWN,
    AdapterResult,
    ModelAdapter,
    ScoreEntry,
)
from ..gateway.errors import (
    ApiError,
    ERROR_INTERNAL,
    ERROR_INVALID_MODELS,
    ERROR_INVALID_MODEL_OUTPUT,
    ERROR_INVALID_TITLE,
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
    DEFAULT_COMPARE_CONCURRENCY,
    DEFAULT_COMPARE_TOTAL_TIMEOUT_MS,
    RejectConfig,
)
from ..gateway.stats import StatsCollector
from ..models.registry import ModelRegistry, ModelSpec

#: B6：SSE 心跳间隔（注释行；不伪装 model 事件、不延长总截止，§6.6）。
COMPARE_STREAM_HEARTBEAT_SECONDS = 15.0

#: B6：客户端断连轮询间隔（避免每个事件都调用一次 is_disconnected）。
COMPARE_DISCONNECT_POLL_SECONDS = 0.2

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
    ) -> None:
        self._registry = registry
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
    def _http_status(code: str | None) -> int:
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
        self, title: str, model_id: str | None, request_id: str
    ) -> ClassifyOutcome:
        clean_title = self._validate_title(title)
        spec = self._resolve_one(model_id)
        adapter = self._adapter_for(spec)
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

    async def compare(
        self, title: str, models: list[str] | None, request_id: str
    ) -> CompareOutcome:
        clean_title = self._validate_title(title)
        specs = self._resolve_many(models)
        semaphore = asyncio.Semaphore(self._compare_concurrency)
        adapters = [self._adapter_for(spec) for spec in specs]

        async def run_one(adapter: ModelAdapter) -> AdapterResult:
            async with semaphore:
                return await adapter.predict(clean_title, request_id)

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
        adapters = [self._adapter_for(spec) for spec in specs]

        loop = asyncio.get_running_loop()
        started = loop.time()
        deadline = started + (self._compare_total_timeout_ms / 1000.0)
        total = len(specs)
        ok = 0
        # B12：并发上限由实例配置注入（TMF_COMPARE_CONCURRENCY，默认 3）；
        # 与 POST /compare 共用同一配置，不另设第二套并发/截止。
        semaphore = asyncio.Semaphore(self._compare_concurrency)

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
