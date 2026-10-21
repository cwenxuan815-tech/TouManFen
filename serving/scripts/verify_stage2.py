#!/usr/bin/env python
"""阶段 2 轻量验收脚本（不依赖 pytest）。

只验证阶段 2 开放的能力：
- 网关 /healthz、/readyz、/models、/stats；
- request ID 生成/透传/拒绝；
- 5 个 worker 的只读 /healthz、/readyz、/metadata；
- 4 个本地模型 worker 的旧 /predict_api 契约（不调用 LLM）。
- 阶段 3 起 /classify、/compare 已开放，本脚本不再断言它们 404（由 verify_stage3.py 覆盖）。

不调用真实 LLM，不训练，不压测。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tmf_common.config import probe_host, resolve_worker_url  # noqa: E402

PROCESSES_FILE = PROJECT_ROOT / "serving" / "config" / "processes.yaml"
WORKERS_FILE = PROJECT_ROOT / "serving" / "config" / "workers.yaml"
CLASSES_FILE = PROJECT_ROOT / "00-data" / "data" / "class.txt"


def load_classes() -> list[str]:
    return [
        line.strip()
        for line in CLASSES_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> int:
    processes = yaml.safe_load(PROCESSES_FILE.read_text(encoding="utf-8"))
    workers_doc = yaml.safe_load(WORKERS_FILE.read_text(encoding="utf-8"))
    gateway = processes["gateway"]
    gateway_host = probe_host(os.environ.get("TMF_GATEWAY_HOST") or gateway["host"])
    base_url = f"http://{gateway_host}:{gateway['port']}"
    classes = load_classes()
    failures: list[str] = []

    def check(name: str, condition: bool, detail: str = "") -> None:
        if condition:
            print(f"PASS {name}")
        else:
            print(f"FAIL {name} {detail}".rstrip())
            failures.append(name)

    def safe_get(url: str, **kwargs):
        try:
            return httpx.get(url, timeout=10.0, trust_env=False, **kwargs)
        except Exception as exc:
            print(f"FAIL request_error {url} {type(exc).__name__}")
            return None

    def safe_post(url: str, **kwargs):
        try:
            return httpx.post(url, timeout=20.0, trust_env=False, **kwargs)
        except Exception as exc:
            print(f"FAIL request_error {url} {type(exc).__name__}")
            return None

    with httpx.Client(timeout=10.0, trust_env=False) as client:
        response = client.get(f"{base_url}/api/v1/healthz")
        body = response.json() if response.status_code == 200 else {}
        check("healthz status", response.status_code == 200 and body.get("status") == "ok", str(response.status_code))
        request_id = body.get("request_id", "")
        check("healthz request_id header", response.headers.get("X-Request-ID") == request_id)

        response = client.get(
            f"{base_url}/api/v1/healthz",
            headers={"X-Request-ID": "stage2-verify-001"},
        )
        body = response.json() if response.status_code == 200 else {}
        check(
            "request_id custom echo",
            response.headers.get("X-Request-ID") == "stage2-verify-001"
            and body.get("request_id") == "stage2-verify-001",
        )

        response = client.get(
            f"{base_url}/api/v1/healthz",
            headers={"X-Request-ID": "bad id!"},
        )
        body = response.json() if response.status_code == 400 else {}
        check(
            "request_id invalid 400",
            response.status_code == 400
            and body.get("error", {}).get("code") == "INVALID_REQUEST_ID",
            str(response.status_code),
        )

        response = client.get(f"{base_url}/api/v1/readyz")
        body = response.json() if response.status_code == 200 else {}
        workers = body.get("workers", [])
        check("readyz 200", response.status_code == 200)
        check("readyz 5 workers", len(workers) == 5, str(len(workers)))
        check(
            "readyz all up",
            all(item.get("status") == "up" for item in workers),
            str([(item.get("worker_id"), item.get("status")) for item in workers]),
        )

        response = client.get(f"{base_url}/api/v1/models")
        body = response.json() if response.status_code == 200 else {}
        models = body.get("models", [])
        check("models 200", response.status_code == 200)
        check("models 5", len(models) == 5, str(len(models)))
        check("models default", body.get("default_model") == "fasttext")
        check("models classes 10", len(body.get("classes", [])) == 10)
        check(
            "models status up",
            all(item.get("status") == "up" for item in models),
            str([(item.get("model_id"), item.get("status")) for item in models]),
        )
        check(
            "models version safe",
            all(
                item.get("model_version") is None
                or isinstance(item.get("model_version"), str)
                for item in models
            ),
        )

        response = client.get(f"{base_url}/api/v1/stats")
        body = response.json() if response.status_code == 200 else {}
        check("stats 200", response.status_code == 200)
        check("stats process_local", body.get("process_local") is True)
        check("stats total > 0", body.get("total_requests", 0) > 0, str(body.get("total_requests")))

        for name, worker in processes["workers"].items():
            worker_base = resolve_worker_url(
                workers_doc["workers"][name]["base_url"], name
            ).rstrip("/")
            response = safe_get(f"{worker_base}/healthz")
            check(
                f"{name} healthz",
                response is not None
                and response.status_code == 200
                and response.json().get("status") == "alive",
            )
            response = safe_get(f"{worker_base}/readyz")
            check(
                f"{name} readyz",
                response is not None
                and response.status_code == 200
                and response.json().get("status") == "ready",
            )
            response = safe_get(f"{worker_base}/metadata")
            check(
                f"{name} metadata",
                response is not None
                and response.status_code == 200
                and response.json().get("model_id") == name,
            )

        # 旧 /predict_api：只对 4 个本地模型做最小验证，不调用 LLM
        title = "北京二手房成交量创年内新高"
        for name in ("rf", "fasttext", "bert", "distill"):
            worker_base = resolve_worker_url(
                workers_doc["workers"][name]["base_url"], name
            ).rstrip("/")
            response = safe_post(f"{worker_base}/predict_api", json={"title": title})
            body = response.json() if response is not None and response.status_code == 200 else {}
            check(
                f"{name} old predict contract",
                response is not None
                and response.status_code == 200
                and body.get("pred_class") in classes
                and body.get("title") == title,
                str(response.status_code) if response is not None else "no response",
            )

    if failures:
        print(f"RESULT FAIL ({len(failures)}): " + ", ".join(failures))
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
