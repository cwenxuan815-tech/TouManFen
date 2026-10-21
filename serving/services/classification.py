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
from dataclasses import dataclass
from typing import Mapping

from ..adapters.protocol import AdapterResult, ModelAdapter
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
from ..gateway.stats import StatsCollector
from ..models.registry import ModelRegistry, ModelSpec

MAX_COMPARE_CONCURRENCY = 5


@dataclass
class ClassifyOutcome:
    model_id: str
    model_version: str | None
    pred_class: str
    latency_ms: int


@dataclass
class CompareItem:
    model_id: str
    status: str  # ok | error
    latency_ms: int
    model_version: str | None = None
    pred_class: str | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass
class CompareOutcome:
    items: list[CompareItem]
    total: int
    ok: int
    failed: int
    status: str  # ok | partial | all_failed


class ClassificationService:
    def __init__(
        self,
        registry: ModelRegistry,
        adapters: Mapping[str, ModelAdapter],
        classes: tuple[str, ...],
        max_title_chars: int,
        stats: StatsCollector,
        logger: logging.Logger,
    ) -> None:
        self._registry = registry
        self._adapters = dict(adapters)
        self._classes = frozenset(classes)
        self._max_title_chars = max_title_chars
        self._stats = stats
        self._logger = logger

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
        return ClassifyOutcome(
            model_id=spec.model_id,
            model_version=spec.version,
            pred_class=result.pred_class or "",
            latency_ms=result.latency_ms,
        )

    async def compare(
        self, title: str, models: list[str] | None, request_id: str
    ) -> CompareOutcome:
        clean_title = self._validate_title(title)
        specs = self._resolve_many(models)
        semaphore = asyncio.Semaphore(MAX_COMPARE_CONCURRENCY)
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
