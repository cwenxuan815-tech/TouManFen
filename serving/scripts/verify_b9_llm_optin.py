#!/usr/bin/env python
"""B9 验收：LLM opt-in（默认 compare 不含 LLM）+ mock 在线/离线四组合。

隔离策略（不启动/不连接用户 stage2 服务与真实 LLM/provider）：
- 只启动**临时端口**的 ``serving/scripts/mock_worker.py``（stdlib，本地 mock）；
- 其余 4 个模型用 fake adapter（data_source=live）替代，避免连接真实 worker；
- LLM 用**真实** ``LlmAdapter`` 通过 HTTP 打临时 mock（在线）或指向已关闭端口
  （离线），因此验证的是真实适配器/网关链路，而非纯函数 mock；
- ``/models`` 用 fake prober 替代 worker 探测，避免任何真实 worker 连接；
- 临时 ``TMF_DB_PATH``，不写默认 DB。

四组合（表驱动）：
1. 默认请求（models 省略） × mock 在线 → 4 个本地模型 ok、结果不含 llm、
   mock worker 的 ``/predict_api`` 调用计数为 0（默认请求不触发 LLM）；
2. 默认请求 × mock 离线 → 同上（默认集合与 LLM 可用性无关）；
3. 显式 llm（models 含 llm） × mock 在线 → 5 项全 ok，llm 的
   ``data_source=mock``（worker display_name=mock-llm，B11 判定）；
4. 显式 llm × mock 离线 → summary=partial，4 个本地模型结果保留，
   llm 为 error 且 data_source=unknown（不猜 live），不返回 pred_class。

运行：
    $env:PYTHONPATH='<repo>\\venv-gateway\\Lib\\site-packages'
    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/verify_b9_llm_optin.py
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from serving.adapters import AdapterResult, LlmAdapter  # noqa: E402
from serving.gateway.probes import ProbeResult  # noqa: E402
from serving.gateway.settings import load_settings  # noqa: E402
from serving.gateway.stats import StatsCollector  # noqa: E402
from serving.models.registry import ModelRegistry  # noqa: E402
from serving.services.classification import ClassificationService  # noqa: E402

MOCK_WORKER = PROJECT_ROOT / "serving" / "scripts" / "mock_worker.py"
DEMO_TITLE = "北京二手房成交量创年内新高"
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


def free_tcp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def port_is_closed(port: int, timeout: float = 0.5) -> bool:
    try:
        connection = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    except OSError:
        return True
    connection.close()
    return False


class FakeProber:
    """替代真实 worker 探测：不发起任何网络连接，只让 /models 可用。"""

    def __init__(self, settings) -> None:
        self._settings = settings

    async def probe_all(self) -> dict[str, ProbeResult]:
        return {
            worker_id: ProbeResult(
                worker_id=worker_id,
                status="up",
                latency_ms=1,
                metadata={"display_name": f"fake-{worker_id}"},
            )
            for worker_id in self._settings.workers
        }

    async def close(self) -> None:
        return None


class LocalFakeAdapter:
    """本地模型 fake（data_source=live），避免连接真实 worker。"""

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.model_version: str | None = None

    async def predict(self, title: str, request_id: str) -> AdapterResult:
        return AdapterResult(
            model_id=self.model_id,
            status="ok",
            latency_ms=5,
            pred_class="sports",
            data_source="live",
        )


class MockWorker:
    """隔离的临时端口 mock worker 子进程（用完即关闭）。"""

    def __init__(self, display_name: str) -> None:
        self.port = free_tcp_port()
        self.display_name = display_name
        self.proc: subprocess.Popen | None = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "MockWorker":
        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(MOCK_WORKER),
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                "--model-id",
                "llm",
                "--display-name",
                self.display_name,
            ],
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 15.0
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            while time.monotonic() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"mock worker 提前退出（rc={self.proc.returncode}）")
                try:
                    response = client.get(f"{self.base_url}/metadata")
                    if response.status_code == 200:
                        return self
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
        raise RuntimeError("mock worker 启动超时")

    def stats(self) -> dict[str, Any]:
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            response = client.get(f"{self.base_url}/mock/stats")
            response.raise_for_status()
            return response.json()

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        self.proc = None


def install_fake_classifier(app) -> ClassificationService:
    """4 个本地 fake + 真实 LlmAdapter（HTTP 打临时 mock 或关闭端口）。"""
    settings = app.state.settings
    registry = app.state.registry
    adapters: dict[str, Any] = {}
    for model_id in registry.all_model_ids():
        if model_id == "llm":
            spec = registry.get("llm")
            assert spec is not None
            adapters["llm"] = LlmAdapter(spec, settings.classes, app.state.http_client)
        else:
            adapters[model_id] = LocalFakeAdapter(model_id)
    classifier = ClassificationService(
        registry=registry,
        adapters=adapters,
        classes=settings.classes,
        max_title_chars=settings.max_title_chars,
        stats=app.state.stats,
        logger=logging.getLogger("verify-b9"),
    )
    app.state.classifier = classifier
    return classifier


def prepare_app(llm_url: str, db_path: Path):
    clear_env()
    os.environ["TMF_WORKER_URL_LLM"] = llm_url
    os.environ["TMF_DB_PATH"] = str(db_path)
    from serving.gateway.main import create_app

    return create_app()


def model_ids(response) -> list[str]:
    return [item.get("model_id") for item in response.json().get("results", [])]


def config_checks() -> None:
    print("\n=== 1. 配置：llm.compare_by_default=false，其余模型默认不变 ===")
    clear_env()
    settings = load_settings()
    registry = ModelRegistry(settings)
    default_ids = registry.compare_default_model_ids()

    check(
        "llm enabled but not compare-by-default",
        settings.models["llm"].enabled is True
        and settings.models["llm"].compare_by_default is False,
        f"enabled={settings.models['llm'].enabled} "
        f"compare_by_default={settings.models['llm'].compare_by_default}",
    )
    others = [model_id for model_id in registry.all_model_ids() if model_id != "llm"]
    check(
        "other four models keep compare_by_default=true",
        all(settings.models[model_id].compare_by_default for model_id in others),
        str(others),
    )
    check(
        "default compare set is 4 local models without llm",
        "llm" not in default_ids and set(default_ids) == set(others) and len(default_ids) == 4,
        str(default_ids),
    )
    check(
        "llm remains registered for explicit selection",
        "llm" in registry.all_model_ids() and registry.get("llm") is not None,
        str(registry.all_model_ids()),
    )


def online_checks(client: TestClient, app, mock: MockWorker) -> None:
    print("\n=== 2. mock 在线：默认请求不触发 LLM；显式 llm → data_source=mock ===")
    settings = app.state.settings
    registry = app.state.registry
    default_ids = registry.compare_default_model_ids()

    response = client.get("/api/v1/models")
    body = response.json()
    llm_entry = next(
        (item for item in body.get("models", []) if item.get("model_id") == "llm"), {}
    )
    check(
        "/models still lists llm and it is selectable",
        response.status_code == 200
        and len(body.get("models", [])) == 5
        and llm_entry.get("enabled") is True,
        f"{response.status_code} {llm_entry}",
    )

    # 组合 1：默认请求 × mock 在线 → 结果不含 llm，且 mock 一次 predict 都没收到。
    baseline = mock.stats()["predict_requests"]
    response = client.post("/api/v1/compare", json={"title": DEMO_TITLE})
    body = response.json()
    check(
        "combo1 default compare online: 4 local ok, no llm",
        response.status_code == 200
        and model_ids(response) == default_ids
        and "llm" not in model_ids(response)
        and body.get("summary") == {
            "total": 4,
            "ok": 4,
            "failed": 0,
            "status": "ok",
        }
        and all(item.get("data_source") == "live" for item in body["results"]),
        f"{response.status_code} {str(body)[:300]}",
    )
    check(
        "combo1 default compare made zero LLM predict requests",
        mock.stats()["predict_requests"] == baseline,
        str(mock.stats()),
    )

    # 显式 llm × mock 在线（B11：mock-llm → data_source=mock）。
    response = client.post(
        "/api/v1/classify", json={"title": DEMO_TITLE, "model": "llm"}
    )
    body = response.json()
    check(
        "explicit /classify llm online -> ok, data_source=mock",
        response.status_code == 200
        and body.get("pred_class") == "sports"
        and body.get("data_source") == "mock",
        str(body)[:300],
    )
    check(
        "explicit llm classify made one predict request",
        mock.stats()["predict_requests"] == baseline + 1,
        str(mock.stats()),
    )

    # 组合 3：显式 llm × mock 在线 → 5 项全 ok，llm 标记 mock。
    response = client.post(
        "/api/v1/compare",
        json={"title": DEMO_TITLE, "models": [*default_ids, "llm"]},
    )
    body = response.json()
    results = {item.get("model_id"): item for item in body.get("results", [])}
    check(
        "combo3 explicit compare online: 5 ok, llm data_source=mock",
        response.status_code == 200
        and [item.get("model_id") for item in body.get("results", [])]
        == [*default_ids, "llm"]
        and body.get("summary") == {
            "total": 5,
            "ok": 5,
            "failed": 0,
            "status": "ok",
        }
        and results.get("llm", {}).get("status") == "ok"
        and results["llm"].get("pred_class") == "sports"
        and results["llm"].get("data_source") == "mock"
        and all(
            results[model_id].get("data_source") == "live"
            for model_id in default_ids
        ),
        str(body)[:400],
    )


def offline_checks(client: TestClient, app, offline_port: int) -> None:
    print("\n=== 3. mock 离线：默认请求不受影响；显式 llm → partial + unknown ===")
    registry = app.state.registry
    default_ids = registry.compare_default_model_ids()

    # 组合 2：默认请求 × mock 离线 → 与在线一致（默认集合不含 LLM）。
    started = time.perf_counter()
    response = client.post("/api/v1/compare", json={"title": DEMO_TITLE})
    elapsed = time.perf_counter() - started
    body = response.json()
    check(
        "combo2 default compare offline: 4 local ok, no llm dependency",
        response.status_code == 200
        and model_ids(response) == default_ids
        and body.get("summary") == {
            "total": 4,
            "ok": 4,
            "failed": 0,
            "status": "ok",
        }
        and elapsed < 10.0,
        f"{response.status_code} {str(body)[:300]} elapsed={elapsed:.2f}s",
    )

    # 组合 4：显式 llm × mock 离线 → partial，其他模型结果保留。
    response = client.post(
        "/api/v1/compare",
        json={"title": DEMO_TITLE, "models": [*default_ids, "llm"]},
    )
    body = response.json()
    results = {item.get("model_id"): item for item in body.get("results", [])}
    llm = results.get("llm", {})
    check(
        "combo4 explicit compare offline: summary=partial, local results kept",
        response.status_code == 200
        and body.get("summary", {}).get("status") == "partial"
        and body.get("summary", {}).get("total") == 5
        and body.get("summary", {}).get("ok") == 4
        and body.get("summary", {}).get("failed") == 1
        and all(results[model_id].get("status") == "ok" for model_id in default_ids),
        str(body.get("summary")),
    )
    check(
        "combo4 llm offline is error with unknown source and no pred_class",
        llm.get("status") == "error"
        and llm.get("error", {}).get("code") in {"WORKER_UNAVAILABLE", "WORKER_TIMEOUT"}
        and llm.get("data_source") == "unknown"
        and "pred_class" not in llm,
        str(llm)[:300],
    )
    check(
        "combo4 offline returned promptly (connect refused, no provider wait)",
        port_is_closed(offline_port),
    )


def main() -> int:
    clear_env()
    print("=== B9 LLM opt-in 验收（隔离 mock worker + fake 本地模型，不调真实 provider） ===")
    try:
        return _run()
    except Exception as exc:  # 验收脚本自身失败也要给出 RESULT FAIL / 非 0 退出码
        import traceback

        traceback.print_exc()
        print(f"RESULT FAIL (exception: {type(exc).__name__}: {exc})")
        return 1


def _run() -> int:
    import serving.gateway.main as gateway_main

    config_checks()

    # 用 FakeProber 替换模块级 WorkerProber：/models 不发起任何真实 worker 探测，
    # 也避免替换 lifespan 中已创建的 prober（无需跨事件循环关闭它）。
    original_prober_class = gateway_main.WorkerProber
    gateway_main.WorkerProber = FakeProber
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            online_mock = MockWorker("mock-llm")
            offline_port = free_tcp_port()
            try:
                online_mock.start()
                print(
                    f"mock worker(online) {online_mock.base_url} display_name=mock-llm"
                )
                app_online = prepare_app(online_mock.base_url, temp_path / "b9_online.db")
                with TestClient(app_online) as client:
                    install_fake_classifier(app_online)
                    online_checks(client, app_online, online_mock)

                # mock 离线：LLM URL 指向未监听端口（连接被拒绝；不访问外部服务）。
                offline_url = f"http://127.0.0.1:{offline_port}"
                check(
                    "offline llm port is closed before use",
                    port_is_closed(offline_port),
                    str(offline_port),
                )
                app_offline = prepare_app(offline_url, temp_path / "b9_offline.db")
                with TestClient(app_offline) as client:
                    install_fake_classifier(app_offline)
                    offline_checks(client, app_offline, offline_port)
            finally:
                online_mock.stop()
                check(
                    "temporary mock worker port released",
                    port_is_closed(online_mock.port),
                    str(online_mock.port),
                )
                clear_env()
    finally:
        gateway_main.WorkerProber = original_prober_class

    if FAILURES:
        print(f"RESULT FAIL ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
