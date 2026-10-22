"""HTTP worker 适配器。

职责：
- 通过 HTTP 调用某个本地 Flask worker 的 ``predict_path``；
- 复用应用级 ``httpx.AsyncClient``（不按请求创建，避免连接泄漏/代理问题）；
- 把连接失败、超时、非 200、非法 JSON、缺字段与非法类别归一化为
  ``AdapterResult``，保持阶段 3 的错误码与脱敏消息不变；
- 解析并校验 worker 的未校准分数（``scores`` / ``score_kind`` / ``top_k``）；
  分数异常只丢弃分数信息，保留有效 ``pred_class``；
- B11：通过 worker ``/metadata.display_name`` 判定 ``data_source``
  （``mock-`` 前缀 → mock，否则 live；取不到 → unknown，绝不猜测 live）；
- **不加载 torch/transformers/fasttext/模型文件，也不读取密钥**。
"""

from __future__ import annotations

import asyncio
import math
import time
from typing import Sequence

import httpx

from tmf_common.prediction import (
    DEFAULT_TOP_K,
    SCORE_KINDS,
    SCORE_MAX,
    SCORE_MIN,
)

from ..gateway.errors import (
    ERROR_INVALID_MODEL_OUTPUT,
    ERROR_MODEL_ERROR,
    ERROR_WORKER_BAD_RESPONSE,
    ERROR_WORKER_HTTP_ERROR,
    ERROR_WORKER_TIMEOUT,
    ERROR_WORKER_UNAVAILABLE,
)
from ..models.registry import ModelSpec
from .protocol import (
    DATA_SOURCE_LIVE,
    DATA_SOURCE_MOCK,
    DATA_SOURCE_UNKNOWN,
    AdapterResult,
    ScoreEntry,
    data_source_from_display_name,
)

#: B11：拉取 ``/metadata`` 判定 data_source 的超时上限（秒）；正常远小于此。
DATA_SOURCE_METADATA_TIMEOUT_S = 2.0

#: B11：metadata 失败后的重试间隔（秒）——worker 离线转上线后仍可恢复 live 判定。
DATA_SOURCE_RETRY_S = 30.0


class HttpWorkerAdapter:
    """单个模型的 HTTP worker 适配器。"""

    def __init__(
        self,
        spec: ModelSpec,
        classes: Sequence[str],
        client: httpx.AsyncClient,
    ) -> None:
        self.model_id = spec.model_id
        self.model_version = spec.version
        self._spec = spec
        self._classes = frozenset(classes)
        self._client = client
        # B11：data_source 由 /metadata.display_name 约定判定；成功结果永久缓存，
        # 失败结果缓存 DATA_SOURCE_RETRY_S 后重试（不把“暂时离线”固化成 unknown）。
        self._data_source: str | None = None
        self._data_source_attempt_at: float | None = None
        self._data_source_lock = asyncio.Lock()

    @property
    def data_source(self) -> str | None:
        """最近一次判定的 data_source（未判定过为 None；不是 live 推断）。"""
        return self._data_source

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------
    def _url(self) -> str:
        worker = self._spec.worker
        return worker.base_url.rstrip("/") + worker.predict_path

    def _timeout(self) -> httpx.Timeout:
        seconds = self._spec.worker.timeout_ms / 1000.0
        return httpx.Timeout(seconds, connect=min(0.5, seconds))

    # ------------------------------------------------------------------
    # B11：data_source 判定（worker /metadata.display_name 约定）
    # ------------------------------------------------------------------
    def _metadata_url(self) -> str:
        worker = self._spec.worker
        return worker.base_url.rstrip("/") + worker.metadata_path

    def _metadata_timeout(self) -> httpx.Timeout:
        seconds = min(
            self._spec.worker.timeout_ms / 1000.0,
            DATA_SOURCE_METADATA_TIMEOUT_S,
        )
        return httpx.Timeout(seconds, connect=min(0.3, seconds))

    async def _fetch_data_source(self) -> str:
        """只调用 ``/metadata`` 判定来源；任何失败/缺字段 → unknown（不猜 live）。"""
        try:
            response = await self._client.get(
                self._metadata_url(),
                timeout=self._metadata_timeout(),
            )
            if response.status_code != 200:
                return DATA_SOURCE_UNKNOWN
            data = response.json()
        except Exception:
            return DATA_SOURCE_UNKNOWN
        if not isinstance(data, dict):
            return DATA_SOURCE_UNKNOWN
        return data_source_from_display_name(data.get("display_name"))

    async def _resolve_data_source(self) -> str:
        """返回 live/mock/unknown；成功结果缓存，失败按 DATA_SOURCE_RETRY_S 重试。"""
        if self._data_source in (DATA_SOURCE_LIVE, DATA_SOURCE_MOCK):
            return self._data_source
        async with self._data_source_lock:
            if self._data_source in (DATA_SOURCE_LIVE, DATA_SOURCE_MOCK):
                return self._data_source
            now = time.monotonic()
            if (
                self._data_source == DATA_SOURCE_UNKNOWN
                and self._data_source_attempt_at is not None
                and now - self._data_source_attempt_at < DATA_SOURCE_RETRY_S
            ):
                return DATA_SOURCE_UNKNOWN
            self._data_source_attempt_at = now
            self._data_source = await self._fetch_data_source()
            return self._data_source

    # ------------------------------------------------------------------
    # 协议实现
    # ------------------------------------------------------------------
    async def predict(self, title: str, request_id: str) -> AdapterResult:
        # 先判定来源（正常的 /metadata 很快；失败会在 DATA_SOURCE_RETRY_S 内复用）。
        await self._resolve_data_source()
        started = time.perf_counter()
        try:
            response = await self._client.post(
                self._url(),
                json={"title": title},
                headers={"X-Request-ID": request_id},
                timeout=self._timeout(),
            )
        except httpx.ConnectTimeout:
            # 连接阶段超时 = 不可达，而不是“模型处理超时”
            return self._error(ERROR_WORKER_UNAVAILABLE, "所选模型服务不可达", started)
        except httpx.TimeoutException:
            return self._error(
                ERROR_WORKER_TIMEOUT, "所选模型未在配置的时间内返回", started
            )
        except httpx.HTTPError:
            return self._error(ERROR_WORKER_UNAVAILABLE, "所选模型服务不可达", started)
        except Exception:
            return self._error(ERROR_WORKER_UNAVAILABLE, "所选模型调用异常", started)

        latency_ms = int((time.perf_counter() - started) * 1000)
        if response.status_code != 200:
            return AdapterResult(
                model_id=self.model_id,
                status="error",
                latency_ms=latency_ms,
                error_code=ERROR_WORKER_HTTP_ERROR,
                error_message="所选模型服务返回非 200 状态",
                data_source=self._result_data_source(),
            )

        try:
            data = response.json()
        except Exception:
            return self._bad_response(latency_ms, "所选模型返回格式不符合契约")

        if not isinstance(data, dict):
            return self._bad_response(latency_ms, "所选模型返回格式不符合契约")

        if "error" in data:
            return AdapterResult(
                model_id=self.model_id,
                status="error",
                latency_ms=latency_ms,
                error_code=ERROR_MODEL_ERROR,
                error_message="所选模型返回错误",
                data_source=self._result_data_source(),
            )

        pred_class = data.get("pred_class")
        if not isinstance(pred_class, str) or not pred_class:
            return self._bad_response(latency_ms, "所选模型未返回有效 pred_class")

        if pred_class not in self._classes:
            return AdapterResult(
                model_id=self.model_id,
                status="error",
                latency_ms=latency_ms,
                error_code=ERROR_INVALID_MODEL_OUTPUT,
                error_message="所选模型返回的类别不在白名单内",
                data_source=self._result_data_source(),
            )

        scores, score_kind, top_k = self._parse_scores(data, pred_class)
        return AdapterResult(
            model_id=self.model_id,
            status="ok",
            latency_ms=latency_ms,
            pred_class=pred_class,
            scores=scores,
            score_kind=score_kind,
            top_k=top_k,
            data_source=self._result_data_source(),
        )

    def _result_data_source(self) -> str:
        """结果里携带的 data_source；未完成判定时为 unknown（不猜 live）。"""
        return self._data_source or DATA_SOURCE_UNKNOWN

    # ------------------------------------------------------------------
    # 分数解析与安全校验（B1）
    # ------------------------------------------------------------------
    def _parse_scores(
        self, data: dict, pred_class: str
    ) -> tuple[
        tuple[ScoreEntry, ...] | None,
        str | None,
        tuple[ScoreEntry, ...] | None,
    ]:
        """解析 worker 的 ``scores`` / ``score_kind`` / ``top_k`` 并做安全校验。

        校验：分数数量等于类别数、类别属于白名单、数值有限且位于 [0, 1]、
        ``score_kind`` 属于契约枚举、``top_k`` 与按分数排序的期望结果完全一致、
        ``pred_class`` 是最高分类别之一。任何一项失败都返回全 ``None``
        （只丢弃分数信息，保留有效 ``pred_class``），不抛异常、不变更状态。
        """
        raw_scores = data.get("scores")
        raw_kind = data.get("score_kind")
        raw_top_k = data.get("top_k")
        if raw_scores is None or raw_kind is None or raw_top_k is None:
            return None, None, None
        if (
            not isinstance(raw_scores, dict)
            or not isinstance(raw_kind, str)
            or not isinstance(raw_top_k, list)
        ):
            return None, None, None
        if raw_kind not in SCORE_KINDS:
            return None, None, None
        if set(raw_scores) != self._classes:
            return None, None, None

        scores: dict[str, float] = {}
        for class_id, raw_value in raw_scores.items():
            if not isinstance(class_id, str) or class_id not in self._classes:
                return None, None, None
            if isinstance(raw_value, bool) or not isinstance(
                raw_value, (int, float)
            ):
                return None, None, None
            value = float(raw_value)
            if not math.isfinite(value) or not (SCORE_MIN <= value <= SCORE_MAX):
                return None, None, None
            scores[class_id] = value

        # 分数必须与 pred_class 自洽（并列最高允许；不要求唯一 argmax）。
        if pred_class not in scores or scores[pred_class] != max(scores.values()):
            return None, None, None

        expected_top = sorted(
            scores.items(), key=lambda item: (-item[1], item[0])
        )[:DEFAULT_TOP_K]
        if len(raw_top_k) != len(expected_top):
            return None, None, None

        top_k: list[ScoreEntry] = []
        for raw_entry in raw_top_k:
            if not isinstance(raw_entry, dict):
                return None, None, None
            class_id = raw_entry.get("class_id")
            raw_value = raw_entry.get("score")
            if not isinstance(class_id, str) or class_id not in scores:
                return None, None, None
            if isinstance(raw_value, bool) or not isinstance(
                raw_value, (int, float)
            ):
                return None, None, None
            value = float(raw_value)
            if not math.isfinite(value) or value != scores[class_id]:
                return None, None, None
            top_k.append(ScoreEntry(class_id=class_id, score=scores[class_id]))

        if [(entry.class_id, entry.score) for entry in top_k] != expected_top:
            return None, None, None

        scores_entries = tuple(
            ScoreEntry(class_id=class_id, score=scores[class_id])
            for class_id in sorted(scores)
        )
        return scores_entries, raw_kind, tuple(top_k)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    def _error(self, code: str, message: str, started: float) -> AdapterResult:
        return AdapterResult(
            model_id=self.model_id,
            status="error",
            latency_ms=int((time.perf_counter() - started) * 1000),
            error_code=code,
            error_message=message,
            data_source=self._result_data_source(),
        )

    def _bad_response(self, latency_ms: int, message: str) -> AdapterResult:
        return AdapterResult(
            model_id=self.model_id,
            status="error",
            latency_ms=latency_ms,
            error_code=ERROR_WORKER_BAD_RESPONSE,
            error_message=message,
            data_source=self._result_data_source(),
        )
