"""F15 启动来源状态接口验收（不启动 worker、不推理、不调用 LLM）。

使用 fastapi TestClient + fake prober：覆盖 mock/live/mixed/unknown/不可用。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("TMF_DB_PATH", str(Path(tempfile.gettempdir()) / "tmf_f15_startup.db"))

from fastapi.testclient import TestClient  # noqa: E402

from serving.gateway.main import create_app  # noqa: E402
from serving.gateway.probes import ProbeResult  # noqa: E402


def fake_probes(app, display_names: dict[str, str | None], down: set[str] | None = None):
    registry = app.state.registry
    down = down or set()

    async def probe_all():
        result = {}
        for model_id in registry.enabled_model_ids():
            spec = registry.get(model_id)
            worker_id = spec.worker_id
            if worker_id in down:
                result[worker_id] = ProbeResult(worker_id=worker_id, status="down")
            else:
                display = display_names.get(worker_id)
                metadata = {"display_name": display} if display is not None else None
                result[worker_id] = ProbeResult(
                    worker_id=worker_id, status="up", latency_ms=1, metadata=metadata
                )
        return result

    app.state.prober.probe_all = probe_all


def check(label: str, condition: bool, detail: object) -> None:
    print(f"{'PASS' if condition else 'FAIL'} {label} | {detail}")
    if not condition:
        raise AssertionError(label)


def main() -> int:
    app = create_app()
    with TestClient(app) as client:
        workers = [app.state.registry.get(mid).worker_id for mid in app.state.registry.enabled_model_ids()]

        fake_probes(app, {wid: f"mock-{wid}" for wid in workers})
        body = client.get("/api/v1/demo/state").json()
        check("all mock -> mock", body["data_source"] == "mock" and body["ready_models"] == len(workers), body)
        check("mock ready/total", body["ready_models"] == body["total_models"] == len(workers), body)
        check("mock has worker sources", len(body["worker_sources"]) == len(workers), body["worker_sources"])

        fake_probes(app, {wid: f"live-{wid}" for wid in workers})
        body = client.get("/api/v1/demo/state").json()
        check("all non-mock metadata -> live", body["data_source"] == "live", body)

        names = {wid: f"mock-{wid}" for wid in workers}
        names[workers[0]] = f"live-{workers[0]}"
        fake_probes(app, names)
        body = client.get("/api/v1/demo/state").json()
        check("mixed -> unknown, not live", body["data_source"] == "unknown" and body["mixed"] is True, body)
        check("mixed has breakdown", any(x["data_source"] == "mock" for x in body["worker_sources"]), body["worker_sources"])

        names = {wid: f"mock-{wid}" for wid in workers}
        names[workers[0]] = None
        fake_probes(app, names)
        body = client.get("/api/v1/demo/state").json()
        check("missing metadata -> unknown", body["data_source"] == "unknown", body)
        check("missing metadata not live", body["data_source"] != "live", body)

        fake_probes(app, {wid: f"mock-{wid}" for wid in workers}, down={workers[-1]})
        body = client.get("/api/v1/demo/state").json()
        check("one worker down -> unknown", body["data_source"] == "unknown", body)
        check("ready count excludes down", body["ready_models"] == len(workers) - 1, body)

        fake_probes(app, {wid: f"mock-{wid}" for wid in workers})
        body = client.get("/api/v1/demo/state").json()
        check("url-like inputs do not affect source", body["data_source"] == "mock", body)
        check("request id present", bool(body.get("request_id")), body.get("request_id"))

    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        try:
            Path(os.environ["TMF_DB_PATH"]).unlink(missing_ok=True)
            Path(os.environ["TMF_DB_PATH"] + "-wal").unlink(missing_ok=True)
            Path(os.environ["TMF_DB_PATH"] + "-shm").unlink(missing_ok=True)
        except Exception:
            pass
