#!/usr/bin/env python
"""worker 侧统一预测契约验收（不加载任何真实模型、不访问 LLM Provider）。

运行环境：worker 依赖（flask 等），例如
    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/verify_refactor_workers.py

覆盖：
1. PredictService 模板：标题校验、推理、白名单校验、latency、错误类型；
2. 历史兼容 JSON：成功 / 缺 title / 预测失败（含 LLM 风格错误格式）；
3. Flask 薄壳 create_app：/predict_api、/healthz、/readyz、/metadata；
4. 未就绪的 readyz 返回 503；
5. 5 个 worker 入口已切换为薄壳 + 统一 SERVICE（静态检查）。
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from tmf_common.config import env_port, resolve_bind_host, validate_host  # noqa: E402
from tmf_common.flask_shell import create_app  # noqa: E402
from tmf_common.prediction import (  # noqa: E402
    InferenceError,
    ModelOutputError,
    PredictionError,
    PredictionResult,
    PredictService,
    TitleValidationError,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


CLASSES = [
    "finance",
    "realty",
    "stocks",
    "education",
    "science",
    "society",
    "politics",
    "sports",
    "game",
    "entertainment",
]


class FakeService(PredictService):
    model_id = "fake"
    display_name = "Fake 模型"

    def infer(self, title: str) -> str:
        if title == "bad":
            return "not_a_class"
        if title == "boom":
            raise InferenceError("推理失败")
        return "sports"


class LlmLikeService(FakeService):
    model_id = "llm-fake"
    display_name = "LLM Fake"
    empty_title_message = "缺少 title 字段"
    error_sets_empty_class = True

    def format_error(self, exc: PredictionError) -> str:
        return f"{exc.code}: {exc.message}"


def contract_checks() -> None:
    service = FakeService(CLASSES)
    result = service.predict("正常标题")
    check(
        "predict returns PredictionResult",
        isinstance(result, PredictionResult)
        and result.pred_class == "sports"
        and isinstance(result.latency_ms, float),
        str(result),
    )

    try:
        service.predict("")
        check("empty title rejected", False)
    except TitleValidationError as exc:
        check("empty title rejected", exc.message == "传递的参数中没有title字段")

    try:
        service.predict(None)  # type: ignore[arg-type]
        check("non-string title rejected", False)
    except TitleValidationError:
        check("non-string title rejected", True)

    try:
        service.predict("bad")
        check("invalid class rejected", False)
    except ModelOutputError:
        check("invalid class rejected", True)

    try:
        service.predict("boom")
        check("inference error propagates", False)
    except InferenceError as exc:
        check("inference error propagates", exc.code == "MODEL_ERROR")

    fake = FakeService(CLASSES)
    check("service is ready by default", fake.is_ready() is True)
    check(
        "metadata contract",
        fake.metadata()
        == {"model_id": "fake", "display_name": "Fake 模型", "version": None},
    )


def payload_checks() -> None:
    generic = FakeService(CLASSES)
    result = generic.handle_payload({"title": "正常标题", "extra": 1})
    check(
        "legacy success payload",
        result == {"title": "正常标题", "extra": 1, "pred_class": "sports"},
        str(result),
    )

    result = generic.handle_payload({"wrong": "x"})
    check(
        "legacy missing title payload",
        result == {"wrong": "x", "error": "传递的参数中没有title字段"},
        str(result),
    )

    result = generic.handle_payload({"title": "bad"})
    check(
        "legacy invalid class payload",
        result.get("error") == "模型输出不在类别白名单内",
        str(result),
    )

    llm = LlmLikeService(CLASSES)
    result = llm.handle_payload({"wrong": "x"})
    check(
        "llm missing title payload",
        result == {"wrong": "x", "error": "缺少 title 字段"},
        str(result),
    )

    result = llm.handle_payload({"title": "bad"})
    check(
        "llm error payload keeps code and empty class",
        result.get("error") == "INVALID_MODEL_OUTPUT: 模型输出不在类别白名单内"
        and result.get("pred_class") == "",
        str(result),
    )

    # 非 dict 输入：保持返回 dict（旧实现在部分 worker 上会抛异常，这里统一为安全格式）
    check(
        "non-dict payload handled safely",
        isinstance(generic.handle_payload(None), dict),
    )


def flask_checks() -> None:
    service = FakeService(CLASSES)
    app = create_app(service, import_name="verify-worker")
    client = app.test_client()

    response = client.post("/predict_api", json={"title": "正常标题"})
    body = response.get_json()
    check(
        "flask predict success",
        response.status_code == 200
        and body["title"] == "正常标题"
        and body["pred_class"] == "sports",
        str(body),
    )

    response = client.post("/predict_api", json={"wrong": "x"})
    body = response.get_json()
    check(
        "flask missing title compatibility",
        response.status_code == 200 and body["error"] == "传递的参数中没有title字段",
        str(body),
    )

    response = client.get("/healthz")
    check(
        "flask healthz",
        response.status_code == 200
        and response.get_json() == {"status": "alive", "service": "fake"},
    )

    response = client.get("/readyz")
    check(
        "flask readyz",
        response.status_code == 200
        and response.get_json() == {"status": "ready", "model_id": "fake"},
    )

    response = client.get("/metadata")
    check(
        "flask metadata",
        response.status_code == 200
        and response.get_json()
        == {"model_id": "fake", "display_name": "Fake 模型", "version": None},
    )

    class NotReadyService(FakeService):
        model_id = "not-ready"
        display_name = "Not Ready"

        def is_ready(self) -> bool:
            return False

    app = create_app(NotReadyService(CLASSES), import_name="verify-worker-not-ready")
    response = app.test_client().get("/readyz")
    check(
        "flask readyz not_ready returns 503",
        response.status_code == 503
        and response.get_json()["status"] == "not_ready",
        f"{response.status_code} {response.get_json()}",
    )


def env_checks() -> None:
    old_bind = os.environ.get("TMF_BIND_HOST")
    old_port = os.environ.get("TMF_WORKER_PORT_FAKE")
    try:
        os.environ.pop("TMF_BIND_HOST", None)
        check("default bind host is loopback", resolve_bind_host() == "127.0.0.1")
        os.environ["TMF_BIND_HOST"] = "0.0.0.0"
        check("bind host override", resolve_bind_host() == "0.0.0.0")
        os.environ["TMF_WORKER_PORT_FAKE"] = "18123"
        check("worker port override", env_port("TMF_WORKER_PORT_FAKE", 8101) == 18123)
        try:
            validate_host("http://bad", variable="TMF_BIND_HOST")
            check("invalid bind host rejected", False)
        except ValueError:
            check("invalid bind host rejected", True)
    finally:
        if old_bind is None:
            os.environ.pop("TMF_BIND_HOST", None)
        else:
            os.environ["TMF_BIND_HOST"] = old_bind
        if old_port is None:
            os.environ.pop("TMF_WORKER_PORT_FAKE", None)
        else:
            os.environ["TMF_WORKER_PORT_FAKE"] = old_port


def static_worker_checks() -> None:
    modules = ["01-rf", "02-fasttext", "03-bert", "04-LLM", "05-distill"]
    service_files = {
        "01-rf": "rf_predict_service.py",
        "02-fasttext": "ft_predict_service.py",
        "03-bert": "bert_predict_service.py",
        "04-LLM": "llm_predict_service.py",
        "05-distill": "student_predict_service.py",
    }
    for module in modules:
        service_path = PROJECT_ROOT / module / service_files[module]
        server_path = PROJECT_ROOT / module / "api_flask_server.py"
        service_tree = ast.parse(service_path.read_text(encoding="utf-8"))
        class_names = [node.name for node in ast.walk(service_tree) if isinstance(node, ast.ClassDef)]
        function_names = [
            node.name for node in ast.walk(service_tree) if isinstance(node, ast.FunctionDef)
        ]
        service_text = service_path.read_text(encoding="utf-8")
        check(
            f"{module} service exposes SERVICE + legacy predict",
            "SERVICE =" in service_text and "predict" in function_names,
            str(function_names),
        )
        check(
            f"{module} service has PredictService subclass",
            any(name.endswith("PredictService") for name in class_names),
            str(class_names),
        )
        server_text = server_path.read_text(encoding="utf-8")
        check(
            f"{module} flask entry is thin shell",
            "create_app(SERVICE" in server_text
            and "run_worker(" in server_text
            and "/predict_api" not in server_text,
            server_text[:160].replace("\n", " "),
        )


def main() -> int:
    contract_checks()
    payload_checks()
    flask_checks()
    env_checks()
    static_worker_checks()
    if FAILURES:
        print(f"RESULT FAIL ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
