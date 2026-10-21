"""Worker 只读探测（阶段 2）。

约束：
- 只调用 worker 的 /readyz 与 /metadata；
- 不调用 /predict_api，不触发模型推理，不访问 LLM 上游；
- 单次探测有短超时，无重试；
- 结果短时缓存，避免高频重复探测；
- 错误只返回安全摘要，不返回堆栈或 worker 内部路径。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .settings import Settings, WorkerConfig


@dataclass
class ProbeResult:
    worker_id: str
    status: str  # up | down | disabled
    latency_ms: int | None = None
    error: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None


class WorkerProber:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._timeout_s = settings.probe_timeout_ms / 1000.0
        self._cache_s = settings.status_cache_ms / 1000.0
        self._cache: dict[str, tuple[float, ProbeResult]] = {}
        # 只探测配置的内网 worker；显式忽略系统代理，避免代理拦截内网直连。
        self._client = httpx.AsyncClient(trust_env=False)

    async def close(self) -> None:
        await self._client.aclose()

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self._timeout_s, connect=min(0.5, self._timeout_s))

    @staticmethod
    def _url(worker: WorkerConfig, path: str) -> str:
        return worker.base_url.rstrip("/") + path

    async def _fetch_metadata(self, worker: WorkerConfig) -> dict[str, Any] | None:
        try:
            response = await self._client.get(
                self._url(worker, worker.metadata_path), timeout=self._timeout()
            )
            if response.status_code != 200:
                return None
            data = response.json()
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    async def _probe_one(self, worker_id: str, worker: WorkerConfig) -> ProbeResult:
        now = time.monotonic()
        cached = self._cache.get(worker_id)
        if cached is not None and (now - cached[0]) < self._cache_s:
            return cached[1]

        started = time.perf_counter()
        try:
            response = await self._client.get(
                self._url(worker, worker.ready_path), timeout=self._timeout()
            )
            latency_ms = int((time.perf_counter() - started) * 1000)
            if response.status_code != 200:
                result = ProbeResult(
                    worker_id=worker_id,
                    status="down",
                    latency_ms=latency_ms,
                    error={
                        "code": "PROBE_HTTP_STATUS",
                        "message": f"worker 就绪探测返回 HTTP {response.status_code}",
                    },
                )
            else:
                try:
                    data = response.json()
                except Exception:
                    data = None
                if not isinstance(data, dict) or data.get("status") != "ready":
                    result = ProbeResult(
                        worker_id=worker_id,
                        status="down",
                        latency_ms=latency_ms,
                        error={
                            "code": "PROBE_NOT_READY",
                            "message": "worker 未返回 ready 状态",
                        },
                    )
                else:
                    metadata = await self._fetch_metadata(worker)
                    result = ProbeResult(
                        worker_id=worker_id,
                        status="up",
                        latency_ms=latency_ms,
                        metadata=metadata,
                    )
        except httpx.TimeoutException:
            result = ProbeResult(
                worker_id=worker_id,
                status="down",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error={"code": "PROBE_TIMEOUT", "message": "worker 就绪探测超时"},
            )
        except httpx.HTTPError:
            result = ProbeResult(
                worker_id=worker_id,
                status="down",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error={"code": "PROBE_CONNECT_ERROR", "message": "worker 不可达"},
            )
        except Exception:
            result = ProbeResult(
                worker_id=worker_id,
                status="down",
                latency_ms=int((time.perf_counter() - started) * 1000),
                error={"code": "PROBE_INTERNAL_ERROR", "message": "worker 就绪探测异常"},
            )

        if self._cache_s > 0:
            self._cache[worker_id] = (time.monotonic(), result)
        return result

    async def probe_all(self) -> dict[str, ProbeResult]:
        enabled = [
            (worker_id, worker)
            for worker_id, worker in self._settings.workers.items()
            if worker.enabled
        ]
        raw_results = await asyncio.gather(
            *[self._probe_one(worker_id, worker) for worker_id, worker in enabled],
            return_exceptions=True,
        )
        results: dict[str, ProbeResult] = {}
        for (worker_id, _worker), raw in zip(enabled, raw_results):
            if isinstance(raw, ProbeResult):
                results[worker_id] = raw
            else:
                results[worker_id] = ProbeResult(
                    worker_id=worker_id,
                    status="down",
                    error={
                        "code": "PROBE_INTERNAL_ERROR",
                        "message": "worker 就绪探测任务异常",
                    },
                )
        for worker_id, worker in self._settings.workers.items():
            if not worker.enabled:
                results[worker_id] = ProbeResult(worker_id=worker_id, status="disabled")
        return {name: results[name] for name in self._settings.workers if name in results}
