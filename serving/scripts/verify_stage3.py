#!/usr/bin/env python
"""阶段 3 轻量验收脚本（不依赖 pytest）。

前提：
- 网关已启动（默认 http://127.0.0.1:8000，取自 serving/config/processes.yaml，
  可用 TMF_GATEWAY_HOST / --base-url 覆盖）；
- 4 个本地 worker（rf/fasttext/bert/distill）已启动；
- LLM 模式：
  - `--llm-mode skip`：不测试 LLM，默认 compare 不执行；
  - `--llm-mode mock`：8105 必须是 mock_worker.py（display_name=mock-llm），
    不访问真实 LLM provider，不请求真实 LLM worker 的 /predict_api。

覆盖：
- /classify 成功、默认/显式模型、标题边界、未知模型、request ID；
- /compare 默认五模型、显式顺序、部分失败、全部失败、超时、非法列表；
- LLM mock 的成功/错误/非法输出/超时映射；
- 类别来自 class.txt；禁用模型（离线单元检查）；
- 日志/统计之外的阶段边界（无 confidence、无批量接口）。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import httpx
import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tmf_common.config import probe_host, resolve_worker_url  # noqa: E402

from serving.gateway.errors import ApiError  # noqa: E402
from serving.gateway.settings import load_settings  # noqa: E402
from serving.gateway.stats import StatsCollector  # noqa: E402
from serving.models.registry import ModelRegistry  # noqa: E402
from serving.services.classification import ClassificationService  # noqa: E402

PROCESSES_FILE = PROJECT_ROOT / "serving" / "config" / "processes.yaml"
MODELS_FILE = PROJECT_ROOT / "serving" / "config" / "models.yaml"
CLASSES_FILE = PROJECT_ROOT / "00-data" / "data" / "class.txt"

FAILURES: list[str] = []
CLIENT: httpx.Client | None = None


def load_classes() -> list[str]:
    return [
        line.strip()
        for line in CLASSES_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


def request(method: str, url: str, **kwargs):
    assert CLIENT is not None
    try:
        return CLIENT.request(method, url, **kwargs)
    except Exception as exc:
        print(f"FAIL request_error {method} {url} {type(exc).__name__}")
        return None


def error_code(response) -> str | None:
    if response is None:
        return None
    try:
        return response.json().get("error", {}).get("code")
    except Exception:
        return None


def expect_error(name: str, response, status: int, code: str) -> None:
    check(
        name,
        response is not None
        and response.status_code == status
        and error_code(response) == code,
        f"status={getattr(response, 'status_code', 'none')} code={error_code(response)}",
    )


def unit_checks() -> None:
    settings = load_settings()
    classes = load_classes()
    check("class source is class.txt", list(settings.classes) == classes)
    check("class whitelist 10 unique", len(classes) == 10 and len(set(classes)) == 10)

    with tempfile.TemporaryDirectory() as temp_dir:
        bad_class = Path(temp_dir) / "class9.txt"
        bad_class.write_text("\n".join(classes[:9]) + "\n", encoding="utf-8")
        try:
            load_settings(class_file=bad_class)
            check("invalid class file rejected", False)
        except Exception as exc:
            check("invalid class file rejected", "类别数量错误" in str(exc))

    with tempfile.TemporaryDirectory() as temp_dir:
        raw = yaml.safe_load(MODELS_FILE.read_text(encoding="utf-8"))
        raw = copy.deepcopy(raw)
        raw["models"]["rf"]["enabled"] = False
        disabled_file = Path(temp_dir) / "models_disabled.yaml"
        disabled_file.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        disabled_settings = load_settings(models_file=disabled_file)
        registry = ModelRegistry(disabled_settings)
        service = ClassificationService(
            registry=registry,
            adapters={},  # 禁用模型在调用适配器之前就会拒绝
            classes=disabled_settings.classes,
            max_title_chars=disabled_settings.max_title_chars,
            stats=StatsCollector(),
            logger=logging.getLogger("stage3-unit"),
        )
        try:
            asyncio.run(service.classify("x", "rf", "unit"))
            check("disabled model rejected", False)
        except ApiError as exc:
            check(
                "disabled model rejected",
                exc.code == "MODEL_DISABLED" and exc.status_code == 400,
            )
        except Exception as exc:
            check("disabled model rejected", False, type(exc).__name__)


def main() -> int:
    global CLIENT

    parser = argparse.ArgumentParser()
    parser.add_argument("--llm-mode", choices=("skip", "mock"), default="skip")
    parser.add_argument("--base-url", default=None)
    args = parser.parse_args()

    processes = yaml.safe_load(PROCESSES_FILE.read_text(encoding="utf-8"))
    models_config = yaml.safe_load(MODELS_FILE.read_text(encoding="utf-8"))
    gateway = processes["gateway"]
    gateway_host = probe_host(os.environ.get("TMF_GATEWAY_HOST") or gateway["host"])
    base_url = args.base_url or f"http://{gateway_host}:{gateway['port']}"
    llm_base_url = resolve_worker_url(
        f"http://{gateway['host']}:{processes['workers']['llm']['port']}", "llm"
    )
    classes = load_classes()
    default_compare_order = [
        model_id
        for model_id, model in models_config["models"].items()
        if model.get("enabled", True) and model.get("compare_by_default", True)
    ]

    unit_checks()

    with httpx.Client(timeout=40.0, trust_env=False) as client:
        CLIENT = client

        response = request("GET", f"{base_url}/api/v1/healthz")
        check(
            "gateway healthz",
            response is not None
            and response.status_code == 200
            and response.json().get("status") == "ok",
        )

        response = request("GET", f"{base_url}/api/v1/models")
        body = response.json() if response is not None and response.status_code == 200 else {}
        check(
            "models registry",
            response is not None
            and response.status_code == 200
            and len(body.get("models", [])) == 5
            and body.get("default_model") == "fasttext"
            and len(body.get("classes", [])) == 10,
        )

        title = "北京二手房成交量创年内新高"
        for model_id in ("rf", "fasttext", "bert", "distill"):
            response = request(
                "POST",
                f"{base_url}/api/v1/classify",
                json={"title": title, "model": model_id},
            )
            body = response.json() if response is not None and response.status_code == 200 else {}
            check(
                f"classify {model_id}",
                response is not None
                and response.status_code == 200
                and body.get("model_id") == model_id
                and body.get("pred_class") in classes
                and isinstance(body.get("latency_ms"), int)
                and "confidence" not in body,
                f"status={getattr(response, 'status_code', 'none')}",
            )

        response = request(
            "POST", f"{base_url}/api/v1/classify", json={"title": title}
        )
        body = response.json() if response is not None and response.status_code == 200 else {}
        check(
            "classify default model",
            response is not None
            and response.status_code == 200
            and body.get("model_id") == "fasttext"
            and body.get("pred_class") in classes,
        )

        response = request(
            "POST",
            f"{base_url}/api/v1/classify",
            json={"title": f"  {title}  "},
        )
        check(
            "title trim accepted",
            response is not None
            and response.status_code == 200
            and response.json().get("pred_class") in classes,
        )

        response = request(
            "POST",
            f"{base_url}/api/v1/classify",
            headers={"X-Request-ID": "stage3-verify-001"},
            json={"title": title, "model": "fasttext"},
        )
        body = response.json() if response is not None and response.status_code == 200 else {}
        check(
            "request id custom echo",
            response is not None
            and response.status_code == 200
            and response.headers.get("X-Request-ID") == "stage3-verify-001"
            and body.get("request_id") == "stage3-verify-001",
        )

        response = request(
            "GET",
            f"{base_url}/api/v1/healthz",
            headers={"X-Request-ID": "bad id!"},
        )
        expect_error("invalid request id rejected", response, 400, "INVALID_REQUEST_ID")

        expect_error(
            "empty title rejected",
            request("POST", f"{base_url}/api/v1/classify", json={"title": "   "}),
            400,
            "INVALID_TITLE",
        )
        expect_error(
            "missing title rejected",
            request("POST", f"{base_url}/api/v1/classify", json={"model": "fasttext"}),
            422,
            "INVALID_REQUEST",
        )
        expect_error(
            "non-string title rejected",
            request("POST", f"{base_url}/api/v1/classify", json={"title": 123}),
            422,
            "INVALID_REQUEST",
        )
        expect_error(
            "overlong title rejected",
            request(
                "POST",
                f"{base_url}/api/v1/classify",
                json={"title": "新" * 201},
            ),
            400,
            "TITLE_TOO_LONG",
        )
        expect_error(
            "unknown model rejected",
            request(
                "POST",
                f"{base_url}/api/v1/classify",
                json={"title": title, "model": "not_a_model"},
            ),
            404,
            "UNKNOWN_MODEL",
        )
        expect_error(
            "empty model rejected",
            request(
                "POST",
                f"{base_url}/api/v1/classify",
                json={"title": title, "model": ""},
            ),
            404,
            "UNKNOWN_MODEL",
        )

        response = request(
            "POST",
            f"{base_url}/api/v1/compare",
            json={"title": title, "models": ["bert", "rf"]},
        )
        body = response.json() if response is not None and response.status_code == 200 else {}
        check(
            "compare explicit order",
            response is not None
            and response.status_code == 200
            and [item.get("model_id") for item in body.get("results", [])]
            == ["bert", "rf"]
            and body.get("summary", {}).get("total") == 2
            and body.get("summary", {}).get("ok") == 2
            and body.get("summary", {}).get("status") == "ok"
            and all(
                item.get("pred_class") in classes
                and "confidence" not in item
                for item in body.get("results", [])
            ),
        )

        expect_error(
            "compare empty models rejected",
            request(
                "POST",
                f"{base_url}/api/v1/compare",
                json={"title": title, "models": []},
            ),
            400,
            "INVALID_MODELS",
        )
        expect_error(
            "compare duplicate models rejected",
            request(
                "POST",
                f"{base_url}/api/v1/compare",
                json={"title": title, "models": ["rf", "rf"]},
            ),
            400,
            "INVALID_MODELS",
        )
        expect_error(
            "compare unknown model rejected",
            request(
                "POST",
                f"{base_url}/api/v1/compare",
                json={"title": title, "models": ["not_a_model"]},
            ),
            404,
            "UNKNOWN_MODEL",
        )

        response = request("GET", f"{base_url}/api/v1/stats")
        body = response.json() if response is not None and response.status_code == 200 else {}
        check(
            "stats model_calls",
            response is not None
            and response.status_code == 200
            and isinstance(body.get("model_calls"), dict)
            and "rf" in body.get("model_calls", {}),
        )

        response = request(
            "POST", f"{base_url}/api/v1/classify/batch", json={"items": []}
        )
        check(
            "batch endpoint not implemented",
            response is not None and response.status_code == 404,
        )

        if args.llm_mode == "mock":
            metadata = request("GET", f"{llm_base_url}/metadata")
            mock_ready = (
                metadata is not None
                and metadata.status_code == 200
                and metadata.json().get("display_name") == "mock-llm"
            )
            check(
                "mock llm detected",
                mock_ready,
                "8105 不是 mock worker；请先停止真实 LLM worker 并启动 mock_worker.py",
            )
            if mock_ready:
                llm_cases = [
                    ("MOCK_OK", 200, None),
                    ("MOCK_ERROR", 502, "MODEL_ERROR"),
                    ("MOCK_MISSING_CLASS", 502, "WORKER_BAD_RESPONSE"),
                    ("MOCK_INVALID_CLASS", 502, "INVALID_MODEL_OUTPUT"),
                    ("MOCK_EMPTY_CLASS", 502, "WORKER_BAD_RESPONSE"),
                    ("MOCK_INVALID_JSON", 502, "WORKER_BAD_RESPONSE"),
                    ("MOCK_HTTP_500", 502, "WORKER_HTTP_ERROR"),
                    ("MOCK_SLOW", 504, "WORKER_TIMEOUT"),
                ]
                for mock_title, status, code in llm_cases:
                    response = request(
                        "POST",
                        f"{base_url}/api/v1/classify",
                        json={"title": mock_title, "model": "llm"},
                    )
                    if code is None:
                        check(
                            "llm mock success",
                            response is not None
                            and response.status_code == 200
                            and response.json().get("pred_class") == "sports",
                        )
                        continue
                    expect_error(f"llm {mock_title}", response, status, code)
                    if response is not None:
                        check(
                            f"llm {mock_title} no raw error",
                            "mock error" not in response.text
                            and "not-json" not in response.text
                            and "Traceback" not in response.text,
                        )

                response = request(
                    "POST",
                    f"{base_url}/api/v1/compare",
                    json={"title": "MOCK_OK"},
                )
                body = response.json() if response is not None and response.status_code == 200 else {}
                results = body.get("results", [])
                check(
                    "compare default five ok",
                    response is not None
                    and response.status_code == 200
                    and [item.get("model_id") for item in results]
                    == default_compare_order
                    and body.get("summary", {}).get("status") == "ok"
                    and body.get("summary", {}).get("ok") == 5
                    and all(item.get("pred_class") in classes for item in results)
                    and all("confidence" not in item for item in results),
                )

                response = request(
                    "POST",
                    f"{base_url}/api/v1/compare",
                    json={"title": "MOCK_ERROR"},
                )
                body = response.json() if response is not None and response.status_code == 200 else {}
                results = body.get("results", [])
                llm_result = next(
                    (item for item in results if item.get("model_id") == "llm"), {}
                )
                check(
                    "compare partial llm failure",
                    response is not None
                    and response.status_code == 200
                    and body.get("summary", {}).get("status") == "partial"
                    and body.get("summary", {}).get("ok") == 4
                    and body.get("summary", {}).get("failed") == 1
                    and llm_result.get("status") == "error"
                    and llm_result.get("error", {}).get("code") == "MODEL_ERROR"
                    and "pred_class" not in llm_result,
                )

                response = request(
                    "POST",
                    f"{base_url}/api/v1/compare",
                    json={"title": "MOCK_ERROR", "models": ["llm"]},
                )
                body = response.json() if response is not None and response.status_code == 200 else {}
                results = body.get("results", [])
                check(
                    "compare all_failed",
                    response is not None
                    and response.status_code == 200
                    and body.get("summary", {}).get("status") == "all_failed"
                    and body.get("summary", {}).get("ok") == 0
                    and len(results) == 1
                    and results[0].get("status") == "error"
                    and "pred_class" not in results[0],
                )

                response = request(
                    "POST",
                    f"{base_url}/api/v1/compare",
                    json={"title": "MOCK_SLOW", "models": ["llm"]},
                )
                body = response.json() if response is not None and response.status_code == 200 else {}
                results = body.get("results", [])
                llm_result = results[0] if results else {}
                check(
                    "compare llm timeout",
                    response is not None
                    and response.status_code == 200
                    and body.get("summary", {}).get("status") == "all_failed"
                    and llm_result.get("error", {}).get("code") == "WORKER_TIMEOUT"
                    and "pred_class" not in llm_result
                    and llm_result.get("latency_ms", 0) >= 14000,
                )
        else:
            print("SKIP LLM tests (--llm-mode skip)")

    CLIENT = None
    if FAILURES:
        print(f"RESULT FAIL ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
