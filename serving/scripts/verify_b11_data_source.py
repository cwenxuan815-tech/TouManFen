#!/usr/bin/env python
"""B11 验收：data_source 判定 / 统计排除 / 演示快照产物。

覆盖：
1. ``HttpWorkerAdapter`` / ``LlmAdapter`` 的 data_source 判定（**fake transport 为
   主证据**，明确不是真实 worker）：
   - metadata.display_name 以 ``mock-`` 开头 → ``mock``；
   - 非 mock 前缀 → ``live``；
   - metadata 404/500/非法 JSON/缺 display_name/连接异常 → ``unknown``（不猜 live）；
   - 成功结果缓存 metadata（不重复请求）、失败按内部重试窗口恢复；
   - error 结果同样携带 data_source。
2. 隔离 ``mock_worker.py``（临时端口，用完关闭）：``--display-name mock-...`` →
   mock；``--display-name 隔离fake-...``（**fake metadata，非用户真实 worker**）→ live。
3. API 链路：``/classify``、``/compare``（每项）、``/compare/stream``（model 事件）
   均带 data_source；``/stats`` 的 model_calls **只统计 live**（mock/unknown 不计）。
4. 演示快照 ``artifacts/demo/snapshot_<date>.json``：
   - 通过 ``tools/validate_meta.py``（data_kind=demo）；
   - 复用 B8 产物且数值逐字段一致（B8 文件 sha256 不变）；
   - 全量 ``data_source=cached``、LLM 保持 missing/未授权（无预测/分数）；
   - live/mock/cached/offline_benchmark 分块标注；不含运行时统计。

不启动/不连接用户 stage2 服务与真实 LLM/provider；不写默认 DB（临时 TMF_DB_PATH）；
不修改 B8 产物（只读 + sha256 前后对比）。

运行：
    $env:PYTHONPATH='<repo>\\venv-gateway\\Lib\\site-packages'
    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/verify_b11_data_source.py
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
# 复用 B9 验收脚本里的隔离 mock worker 工具（同一目录，按路径运行脚本时可用）。
sys.path.insert(0, str(Path(__file__).resolve().parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from serving.adapters import AdapterResult, HttpWorkerAdapter, LlmAdapter  # noqa: E402
from serving.adapters.http_worker import DATA_SOURCE_RETRY_S  # noqa: E402
from serving.adapters.protocol import (  # noqa: E402
    DATA_SOURCE_CACHED,
    DATA_SOURCE_LIVE,
    DATA_SOURCE_MOCK,
    DATA_SOURCE_UNKNOWN,
)
from serving.gateway.settings import load_settings  # noqa: E402
from serving.gateway.stats import StatsCollector  # noqa: E402
from serving.models.registry import ModelRegistry  # noqa: E402
from serving.services.classification import ClassificationService  # noqa: E402
from tools import make_demo_snapshot as snapshot_tool  # noqa: E402
from verify_b9_llm_optin import (  # noqa: E402
    MockWorker,
    free_tcp_port,
    port_is_closed,
)

DEMO_TITLE = "泰达荷银基金：管理层维护市场稳定信心坚决"
BENCH_DIR = PROJECT_ROOT / "artifacts" / "bench"
SNAPSHOT_DIR = PROJECT_ROOT / "artifacts" / "demo"
SNAPSHOT_PATTERN = re.compile(r"^snapshot_\d{4}-\d{2}-\d{2}\.json$")

FAILURES: list[str] = []
ENV_KEYS = (
    "TMF_COMPARE_TOTAL_TIMEOUT_MS",
    "TMF_COMPARE_CONCURRENCY",
    "TMF_DB_PATH",
    "TMF_MAX_TITLE_CHARS",
    "TMF_WORKER_HOST",
    "TMF_WORKER_URL_RF",
    "TMF_WORKER_URL_LLM",
    "TMF_CORS_ORIGINS",
    "TMF_GATEWAY_HOST",
    "TMF_GATEWAY_PORT",
    "TMF_FRONTEND_HOST",
    "TMF_FRONTEND_PORT",
)


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


def clear_env() -> None:
    for key in ENV_KEYS:
        os.environ.pop(key, None)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# 1. 适配器 data_source（fake transport；明确不是真实 worker）
# ---------------------------------------------------------------------------
def _handler_for(metadata_behavior, predict_response: httpx.Response):
    """构造 MockTransport handler：/metadata 由用例控制，其余视为 /predict_api。"""
    counters = {"metadata": 0, "predict": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metadata":
            counters["metadata"] += 1
            return metadata_behavior(request)
        counters["predict"] += 1
        return predict_response

    return handler, counters


def _run_adapter(
    metadata_behavior,
    *,
    model_id: str = "rf",
    predict_response: httpx.Response | None = None,
) -> tuple[Any, AdapterResult, dict[str, int]]:
    settings = load_settings()
    spec = ModelRegistry(settings).get(model_id)
    assert spec is not None
    handler, counters = _handler_for(
        metadata_behavior,
        predict_response
        if predict_response is not None
        else httpx.Response(200, json={"pred_class": "sports"}),
    )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = (
                LlmAdapter(spec, settings.classes, client)
                if model_id == "llm"
                else HttpWorkerAdapter(spec, settings.classes, client)
            )
            result = await adapter.predict(DEMO_TITLE, "rid-b11")
            return adapter, result

    adapter, result = asyncio.run(run())
    return adapter, result, counters


def adapter_checks() -> None:
    print("\n=== 1. 适配器 data_source 判定（fake transport，不是真实 worker） ===")

    def meta(display_name: str | None = None, status: int = 200, text: str | None = None):
        def behavior(request: httpx.Request) -> httpx.Response:
            if text is not None:
                return httpx.Response(status, text=text)
            payload = {"model_id": "rf"}
            if display_name is not None:
                payload["display_name"] = display_name
            return httpx.Response(status, json=payload)

        return behavior

    def raising(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("fake metadata refused", request=request)

    adapter, result, _ = _run_adapter(meta("随机森林（fake transport）"))
    check(
        "metadata non-mock display_name -> live",
        result.status == "ok"
        and result.data_source == DATA_SOURCE_LIVE
        and adapter.data_source == DATA_SOURCE_LIVE,
        f"{result.data_source}",
    )

    adapter, result, _ = _run_adapter(meta("mock-rf"))
    check(
        "metadata mock- prefix -> mock",
        result.status == "ok"
        and result.data_source == DATA_SOURCE_MOCK
        and adapter.data_source == DATA_SOURCE_MOCK,
        f"{result.data_source}",
    )

    unknown_cases = {
        "metadata 404": meta(status=404),
        "metadata 500": meta(status=500),
        "metadata invalid json": meta(text="not-json"),
        "metadata missing display_name": meta(),
        "metadata connection error": raising,
    }
    for label, behavior in unknown_cases.items():
        _adapter, result, _ = _run_adapter(behavior)
        check(
            f"{label} -> unknown (never guessed live)",
            result.status == "ok" and result.data_source == DATA_SOURCE_UNKNOWN,
            f"{result.status} {result.data_source}",
        )

    # 错误结果也要带 data_source（metadata 正常 + predict 500）。
    _adapter, result, _ = _run_adapter(
        meta("随机森林（fake transport）"),
        predict_response=httpx.Response(500, json={"error": "boom"}),
    )
    check(
        "error result keeps data_source (live) and error code",
        result.status == "error"
        and result.error_code == "WORKER_HTTP_ERROR"
        and result.data_source == DATA_SOURCE_LIVE,
        f"{result.status} {result.error_code} {result.data_source}",
    )

    # LlmAdapter 继承同一判定：mock-llm → mock。
    adapter, result, _ = _run_adapter(meta("mock-llm"), model_id="llm")
    check(
        "LlmAdapter inherits data_source judgement (mock-llm -> mock)",
        result.status == "ok"
        and result.data_source == DATA_SOURCE_MOCK
        and adapter.adapter_kind == "llm",
        f"{result.status} {result.data_source}",
    )

    # metadata 成功缓存：两次 predict 只请求一次 /metadata。
    settings = load_settings()
    spec = ModelRegistry(settings).get("rf")
    assert spec is not None
    handler, counters = _handler_for(
        meta("随机森林（fake transport）"),
        httpx.Response(200, json={"pred_class": "sports"}),
    )

    async def run_twice():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = HttpWorkerAdapter(spec, settings.classes, client)
            first = await adapter.predict(DEMO_TITLE, "rid-1")
            second = await adapter.predict(DEMO_TITLE, "rid-2")
            return first, second

    first, second = asyncio.run(run_twice())
    check(
        "metadata fetched once, data_source cached for subsequent calls",
        counters["metadata"] == 1
        and first.data_source == DATA_SOURCE_LIVE
        and second.data_source == DATA_SOURCE_LIVE,
        str(counters),
    )

    # 失败结果按内部重试窗口恢复（模拟窗口过期；不改产品配置）。
    settings = load_settings()
    spec = ModelRegistry(settings).get("rf")
    assert spec is not None
    state = {"mode": "fail"}

    def flaky_metadata(request: httpx.Request) -> httpx.Response:
        if state["mode"] == "fail":
            raise httpx.ConnectError("fake metadata refused", request=request)
        return httpx.Response(200, json={"display_name": "随机森林（fake transport）"})

    handler, _ = _handler_for(
        flaky_metadata, httpx.Response(200, json={"pred_class": "sports"})
    )

    async def run_retry():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = HttpWorkerAdapter(spec, settings.classes, client)
            before = await adapter.predict(DEMO_TITLE, "rid-1")
            state["mode"] = "live"
            within_window = await adapter.predict(DEMO_TITLE, "rid-2")
            # 模拟 DATA_SOURCE_RETRY_S 过后重试（只动内存里的尝试时间戳）。
            adapter._data_source_attempt_at = time.monotonic() - (DATA_SOURCE_RETRY_S + 1)
            after_window = await adapter.predict(DEMO_TITLE, "rid-3")
            return before, within_window, after_window

    before, within_window, after_window = asyncio.run(run_retry())
    check(
        "unknown cached within retry window, then recovers to live",
        before.data_source == DATA_SOURCE_UNKNOWN
        and within_window.data_source == DATA_SOURCE_UNKNOWN
        and after_window.data_source == DATA_SOURCE_LIVE,
        f"{before.data_source} {within_window.data_source} {after_window.data_source}",
    )


# ---------------------------------------------------------------------------
# 2. 隔离 mock_worker：mock / live 判定（真实 HTTP；live 为 fake metadata）
# ---------------------------------------------------------------------------
def _make_adapter_for_url(model_id: str, base_url: str):
    settings = load_settings()
    registry = ModelRegistry(settings)
    base = registry.get(model_id)
    assert base is not None
    worker = base.worker.model_copy(update={"base_url": base_url})
    spec = dataclasses.replace(base, worker=worker)
    adapter_cls = LlmAdapter if model_id == "llm" else HttpWorkerAdapter
    return adapter_cls(spec, settings.classes, httpx.AsyncClient(trust_env=False))


def mock_worker_checks() -> None:
    print("\n=== 2. 隔离 mock_worker（临时端口）：mock / live 判定 ===")
    mock_worker = MockWorker("mock-llm")
    live_fake = MockWorker("隔离fake-真实模型（fake metadata）")
    try:
        mock_worker.start()
        live_fake.start()

        async def run_predict(base_url: str):
            adapter = _make_adapter_for_url("llm", base_url)
            try:
                return await adapter.predict(DEMO_TITLE, "rid-mock")
            finally:
                await adapter._client.aclose()

        mock_result = asyncio.run(run_predict(mock_worker.base_url))
        live_result = asyncio.run(run_predict(live_fake.base_url))
        check(
            "mock_worker display_name mock-llm -> data_source=mock",
            mock_result.status == "ok"
            and mock_result.pred_class == "sports"
            and mock_result.data_source == DATA_SOURCE_MOCK,
            f"{mock_result.status} {mock_result.data_source}",
        )
        check(
            "mock_worker non-mock display_name -> data_source=live (fake metadata)",
            live_result.status == "ok"
            and live_result.data_source == DATA_SOURCE_LIVE,
            f"{live_result.status} {live_result.data_source}",
        )
        check(
            "isolated mock worker is local (no provider/key involved)",
            True,
            "mock_worker.py 仅 stdlib 本地 HTTP，不读取 API Key",
        )
    finally:
        mock_worker.stop()
        live_fake.stop()
        check(
            "isolated mock worker ports released",
            port_is_closed(mock_worker.port) and port_is_closed(live_fake.port),
            f"{mock_worker.port} {live_fake.port}",
        )


# ---------------------------------------------------------------------------
# 3. API 链路 + 统计排除
# ---------------------------------------------------------------------------
class SourcedFakeAdapter:
    def __init__(
        self,
        model_id: str,
        *,
        data_source: str | None,
        status: str = "ok",
        pred_class: str = "sports",
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> None:
        self.model_id = model_id
        self.model_version: str | None = None
        self._data_source = data_source
        self._status = status
        self._pred_class = pred_class
        self._error_code = error_code
        self._error_message = error_message
        self.calls = 0

    async def predict(self, title: str, request_id: str) -> AdapterResult:
        self.calls += 1
        return AdapterResult(
            model_id=self.model_id,
            status=self._status,  # type: ignore[arg-type]
            latency_ms=7,
            pred_class=self._pred_class if self._status == "ok" else None,
            error_code=self._error_code,
            error_message=self._error_message,
            data_source=self._data_source,
        )


def parse_sse(body: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        event = None
        data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                event = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
            elif line.startswith(":"):
                event = None  # 心跳注释行不是事件
        if event is not None and data is not None:
            events.append((event, data))
    return events


def install_api_classifier(app) -> None:
    settings = app.state.settings
    adapters = {
        "rf": SourcedFakeAdapter("rf", data_source=DATA_SOURCE_LIVE),
        "fasttext": SourcedFakeAdapter("fasttext", data_source=DATA_SOURCE_MOCK),
        "bert": SourcedFakeAdapter("bert", data_source=None),
        "llm": SourcedFakeAdapter(
            "llm",
            data_source=DATA_SOURCE_MOCK,
            status="error",
            error_code="WORKER_TIMEOUT",
            error_message="所选模型未在配置的时间内返回",
        ),
        "distill": SourcedFakeAdapter("distill", data_source=DATA_SOURCE_LIVE),
    }
    app.state.classifier = ClassificationService(
        registry=app.state.registry,
        adapters=adapters,
        classes=settings.classes,
        max_title_chars=settings.max_title_chars,
        stats=app.state.stats,
        logger=logging.getLogger("verify-b11"),
    )


def api_checks() -> None:
    print("\n=== 3. /classify /compare /compare-stream 的 data_source + 统计排除 ===")
    from serving.gateway.main import create_app

    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["TMF_DB_PATH"] = str(Path(temp_dir) / "b11_api.db")
        try:
            app = create_app()
            with TestClient(app) as client:
                install_api_classifier(app)
                settings = app.state.settings

                response = client.post(
                    "/api/v1/classify", json={"title": DEMO_TITLE, "model": "rf"}
                )
                check(
                    "single /classify carries data_source=live",
                    response.status_code == 200
                    and response.json().get("data_source") == DATA_SOURCE_LIVE,
                    str(response.json())[:300],
                )

                response = client.post(
                    "/api/v1/classify",
                    json={"title": DEMO_TITLE, "model": "fasttext"},
                )
                check(
                    "single /classify carries data_source=mock",
                    response.status_code == 200
                    and response.json().get("data_source") == DATA_SOURCE_MOCK,
                    str(response.json())[:300],
                )

                response = client.post(
                    "/api/v1/classify", json={"title": DEMO_TITLE, "model": "bert"}
                )
                body = response.json()
                check(
                    "single /classify keeps explicit null when adapter cannot judge",
                    response.status_code == 200
                    and "data_source" in body
                    and body.get("data_source") is None,
                    str(body)[:300],
                )

                response = client.post(
                    "/api/v1/compare",
                    json={"title": DEMO_TITLE, "models": ["rf", "fasttext", "bert"]},
                )
                body = response.json()
                by_id = {
                    item.get("model_id"): item for item in body.get("results", [])
                }
                check(
                    "/compare carries data_source on every item",
                    response.status_code == 200
                    and by_id.get("rf", {}).get("data_source") == DATA_SOURCE_LIVE
                    and by_id.get("fasttext", {}).get("data_source") == DATA_SOURCE_MOCK
                    and "data_source" in by_id.get("bert", {})
                    and by_id["bert"].get("data_source") is None,
                    str(body)[:400],
                )

                response = client.post(
                    "/api/v1/compare",
                    json={
                        "title": DEMO_TITLE,
                        "models": ["rf", "fasttext", "bert", "llm"],
                    },
                )
                body = response.json()
                llm_item = next(
                    (
                        item
                        for item in body.get("results", [])
                        if item.get("model_id") == "llm"
                    ),
                    {},
                )
                check(
                    "/compare error item also carries data_source (mock)",
                    response.status_code == 200
                    and body.get("summary", {}).get("status") == "partial"
                    and llm_item.get("status") == "error"
                    and llm_item.get("data_source") == DATA_SOURCE_MOCK,
                    str(llm_item)[:300],
                )

                stream_response = client.get(
                    "/api/v1/compare/stream",
                    params={
                        "title": DEMO_TITLE,
                        "models": "rf,fasttext,bert",
                    },
                )
                events = parse_sse(stream_response.text)
                model_events = [
                    data for name, data in events if name == "model"
                ]
                check(
                    "SSE model event carries data_source (backward-compatible field)",
                    stream_response.status_code == 200
                    and {data["model_id"]: data.get("data_source") for data in model_events}
                    == {
                        "rf": DATA_SOURCE_LIVE,
                        "fasttext": DATA_SOURCE_MOCK,
                        "bert": None,
                    }
                    and [name for name, _ in events][-2:] == ["summary", "done"],
                    str(events)[:400],
                )

                response = client.get("/api/v1/stats")
                body = response.json()
                model_calls = body.get("model_calls", {})
                check(
                    "/stats model_calls only counts live calls (mock/unknown excluded)",
                    model_calls.get("rf") == {"ok": 4, "error": 0}
                    and "fasttext" not in model_calls
                    and "bert" not in model_calls
                    and "llm" not in model_calls,
                    str(model_calls),
                )
                check(
                    "/stats note documents mock/cached exclusion",
                    "mock" in body.get("note", ""),
                    str(body.get("note")),
                )
        finally:
            os.environ.pop("TMF_DB_PATH", None)


# ---------------------------------------------------------------------------
# 4. 演示快照
# ---------------------------------------------------------------------------
def _latest_snapshot() -> Path:
    candidates = [
        path
        for path in SNAPSHOT_DIR.glob("snapshot_*.json")
        if path.is_file() and SNAPSHOT_PATTERN.match(path.name)
    ]
    if not candidates:
        raise FileNotFoundError(
            f"演示快照不存在：{SNAPSHOT_DIR}/snapshot_<date>.json（先运行 tools/make_demo_snapshot.py）"
        )
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _strip_generated_at(document: dict[str, Any]) -> dict[str, Any]:
    cleaned = copy.deepcopy(document)
    cleaned.pop("generated_at", None)
    if isinstance(cleaned.get("meta"), dict):
        cleaned["meta"].pop("generated_at", None)
    return cleaned


def snapshot_checks() -> None:
    print("\n=== 4. 演示快照（cached / B8 引用 / LLM missing / meta 校验） ===")
    snapshot_path = _latest_snapshot()
    check(
        "snapshot file follows artifacts/demo/snapshot_<date>.json naming",
        SNAPSHOT_PATTERN.match(snapshot_path.name) is not None,
        snapshot_path.name,
    )
    document = json.loads(snapshot_path.read_text(encoding="utf-8"))

    # B13 meta 契约：模块函数 + CLI 双校验。
    validate_meta = snapshot_tool._load_validate_meta()
    try:
        meta = validate_meta.validate_document(document)
        meta_ok = True
        meta_detail = meta.data_kind
    except Exception as exc:  # noqa: BLE001
        meta_ok = False
        meta_detail = str(exc)
    check(
        "snapshot passes tools/validate_meta.py contract (data_kind=demo)",
        meta_ok and meta_detail == "demo",
        meta_detail,
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(PROJECT_ROOT / "tools" / "validate_meta.py"),
            str(snapshot_path),
        ],
        cwd=str(PROJECT_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    check(
        "snapshot validation CLI exits 0 (META_OK)",
        completed.returncode == 0 and "META_OK" in completed.stdout,
        f"rc={completed.returncode} {completed.stdout[-200:]} {completed.stderr[-200:]}",
    )

    # cached 标注与来源分块。
    demo_cases = document.get("demo", {}).get("titles", [])
    all_results = [result for case in demo_cases for result in case.get("results", [])]
    check(
        "snapshot top-level and every demo result is data_source=cached",
        document.get("data_source") == DATA_SOURCE_CACHED
        and bool(all_results)
        and all(result.get("data_source") == DATA_SOURCE_CACHED for result in all_results),
        f"results={len(all_results)}",
    )
    sources = document.get("sources", {})
    check(
        "live/mock/cached/offline_benchmark are labelled separately",
        sources.get("live", {}).get("used") is False
        and sources.get("mock", {}).get("used") is False
        and sources.get("cached", {}).get("used") is True
        and sources.get("offline_benchmark", {}).get("used") is True
        and sources.get("offline_benchmark", {}).get("data_kind") == "offline",
        json.dumps(sources, ensure_ascii=False)[:300],
    )

    # LLM 必须 missing / 未授权，且不生成任何预测/分数。
    llm = document.get("llm", {})
    check(
        "LLM stays missing/unauthorized with no predictions or scores",
        llm.get("status") == "missing"
        and "external_provider_not_authorized" in str(llm.get("reason"))
        and llm.get("predictions") is None
        and llm.get("scores") is None
        and llm.get("data_source") is None,
        json.dumps(llm, ensure_ascii=False)[:300],
    )
    check(
        "no demo result is attributed to llm and each case marks llm missing",
        all(
            all(result.get("model_id") != "llm" for result in case.get("results", []))
            and case.get("missing_models") == ["llm"]
            for case in demo_cases
        ),
        str([case.get("case_id") for case in demo_cases]),
    )
    check(
        "cached responses contain no fabricated scores/top_k/confidence",
        all(
            "scores" not in result
            and "top_k" not in result
            and "confidence" not in result
            for result in all_results
        ),
        str(all_results[:1])[:300],
    )
    coverage = document.get("model_coverage", {})
    check(
        "model coverage lists 4 local models and llm missing",
        coverage.get("available") == ["rf", "fasttext", "bert", "distill"]
        and coverage.get("missing") == ["llm"],
        json.dumps(coverage, ensure_ascii=False),
    )

    # 不混入运行时统计。
    check(
        "snapshot declares runtime stats excluded (not mixed in)",
        document.get("runtime_stats", {}).get("included") is False
        and "model_calls" not in document,
        str(document.get("runtime_stats")),
    )

    # 复用 B8 产物：逐字段一致 + sha256 引用 + B8 文件未被修改。
    benchmark = document.get("benchmark", {})
    bench_path = PROJECT_ROOT / benchmark.get("artifact_path", "")
    check(
        "snapshot references B8 artifact and its sha256",
        bench_path.is_file()
        and benchmark.get("artifact_sha256") == sha256_file(bench_path)
        and benchmark.get("values_copied_verbatim") is True,
        f"{bench_path} {benchmark.get('artifact_sha256')}",
    )
    bench_document = json.loads(bench_path.read_text(encoding="utf-8"))
    copied_models = {
        entry.get("model_id"): entry for entry in benchmark.get("models", [])
    }
    byte_identical = all(
        copied_models.get(entry.get("model_id"), {}).get("metrics") == entry.get("metrics")
        and copied_models[entry["model_id"]].get("weight") == entry.get("weight")
        for entry in bench_document.get("models", [])
    )
    copied_derived = {
        entry.get("kind"): entry.get("metrics") for entry in benchmark.get("derived", [])
    }
    derived_identical = all(
        copied_derived.get(entry.get("kind")) == entry.get("metrics")
        for entry in bench_document.get("derived", [])
    )
    check(
        "B8 metric values copied verbatim (models + derived, no recompute)",
        byte_identical and derived_identical,
        f"models={byte_identical} derived={derived_identical}",
    )
    check(
        "B8 missing_models still only llm",
        benchmark.get("missing_models") == ["llm"],
        str(benchmark.get("missing_models")),
    )

    # 重新生成到临时文件：B8 文件 sha256 不变 + 内容确定性（忽略 generated_at）。
    sha_before = sha256_file(bench_path)
    with tempfile.TemporaryDirectory() as temp_dir:
        out_path = Path(temp_dir) / "snapshot_regen.json"
        regen = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "tools" / "make_demo_snapshot.py"),
                "--bench-artifact",
                str(bench_path),
                "--out",
                str(out_path),
            ],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        check(
            "make_demo_snapshot.py CLI exits 0 and writes snapshot",
            regen.returncode == 0 and out_path.is_file(),
            f"rc={regen.returncode} {regen.stdout[-200:]} {regen.stderr[-200:]}",
        )
        regen_document = json.loads(out_path.read_text(encoding="utf-8"))
        check(
            "regenerated snapshot is deterministic (same content except generated_at)",
            _strip_generated_at(regen_document) == _strip_generated_at(document),
            "diff detected",
        )
    check(
        "B8 artifact sha256 unchanged by snapshot generation (read-only reuse)",
        sha256_file(bench_path) == sha_before,
        sha_before,
    )


def main() -> int:
    clear_env()
    print("=== B11 data_source / 统计排除 / 演示快照验收（不连接真实 worker/LLM） ===")
    try:
        adapter_checks()
        mock_worker_checks()
        api_checks()
        snapshot_checks()
    except Exception as exc:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print(f"RESULT FAIL (exception: {type(exc).__name__}: {exc})")
        return 1
    finally:
        clear_env()

    if FAILURES:
        print(f"RESULT FAIL ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print(f"snapshot={_latest_snapshot().relative_to(PROJECT_ROOT).as_posix()}")
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
