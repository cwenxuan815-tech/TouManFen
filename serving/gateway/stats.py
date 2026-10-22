"""进程内轻量统计（阶段 2/3）。

明确边界：
- 仅单进程内存数据，重启后清零；
- 统计 HTTP 请求计数/状态码分类/路由耗时；
- 阶段 3 额外按模型记录分类调用的 ok/error 计数；
- B11：``model_calls`` **只统计 data_source=live 的真实调用**；mock/unknown
  不计入（cached 仅存在于离线快照，运行时不产生），不伪造真实统计；
- 不统计预测准确率、LLM 成本或预测成功率。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Any

LATENCY_BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)


class StatsCollector:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._started_monotonic = time.monotonic()
        self._since = datetime.now(timezone.utc).isoformat()
        self._total = 0
        self._by_status: dict[str, int] = {
            "2xx": 0,
            "3xx": 0,
            "4xx": 0,
            "5xx": 0,
            "other": 0,
        }
        self._routes: dict[str, dict[str, Any]] = {}
        self._model_calls: dict[str, dict[str, int]] = {}

    @staticmethod
    def _status_class(status: int) -> str:
        if 200 <= status < 300:
            return "2xx"
        if 300 <= status < 400:
            return "3xx"
        if 400 <= status < 500:
            return "4xx"
        if 500 <= status < 600:
            return "5xx"
        return "other"

    def record(self, route: str, status: int, duration_ms: float) -> None:
        status_class = self._status_class(status)
        with self._lock:
            self._total += 1
            self._by_status[status_class] += 1
            route_stats = self._routes.setdefault(
                route,
                {
                    "count": 0,
                    "total_ms": 0.0,
                    "max_ms": 0.0,
                    "buckets": {bucket: 0 for bucket in LATENCY_BUCKETS_MS},
                },
            )
            route_stats["count"] += 1
            route_stats["total_ms"] += duration_ms
            if duration_ms > route_stats["max_ms"]:
                route_stats["max_ms"] = duration_ms
            for bucket in LATENCY_BUCKETS_MS:
                if duration_ms <= bucket:
                    route_stats["buckets"][bucket] += 1

    def record_model_call(self, model_id: str, outcome: str) -> None:
        """按模型记录分类调用结果；只计数，不记录标题。"""
        normalized = "ok" if outcome == "ok" else "error"
        with self._lock:
            counters = self._model_calls.setdefault(model_id, {"ok": 0, "error": 0})
            counters[normalized] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            routes: dict[str, Any] = {}
            for path, stats in self._routes.items():
                count = stats["count"]
                routes[path] = {
                    "count": count,
                    "avg_ms": round(stats["total_ms"] / count, 2) if count else 0.0,
                    "max_ms": round(stats["max_ms"], 2),
                    "latency_buckets": [
                        {"le_ms": bucket, "count": stats["buckets"][bucket]}
                        for bucket in LATENCY_BUCKETS_MS
                    ],
                }
            return {
                "since": self._since,
                "uptime_s": round(time.monotonic() - self._started_monotonic, 3),
                "total_requests": self._total,
                "by_status": dict(self._by_status),
                "routes": routes,
                "model_calls": {
                    model_id: dict(counters)
                    for model_id, counters in self._model_calls.items()
                },
            }
