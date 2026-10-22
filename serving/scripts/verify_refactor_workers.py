#!/usr/bin/env python
"""worker 侧统一预测契约验收（不加载任何真实模型、不访问 LLM Provider）。

运行环境：worker 依赖（flask 等），例如
    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/verify_refactor_workers.py

覆盖：
1. PredictService 模板：标题校验、推理、白名单校验、latency、错误类型；
2. 历史兼容 JSON：成功 / 缺 title / 预测失败（含 LLM 风格错误格式）；
3. Flask 薄壳 create_app：/predict_api、/healthz、/readyz、/metadata；
4. 未就绪的 readyz 返回 503；
5. 5 个 worker 入口已切换为薄壳 + 统一 SERVICE（静态检查）；
6. B10：各 worker 的 ``scores`` / ``score_kind`` / ``top_k`` 字段契约（静态
   provenance + 动态 payload），以及 **worker 不输出 ``data_source``**
   （data_source 只由网关适配器按 ``/metadata.display_name`` 判定，B11）。
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
    DEFAULT_TOP_K,
    InferenceError,
    ModelOutput,
    ModelOutputError,
    PredictionError,
    PredictionResult,
    PredictService,
    SCORE_KIND_RAW_SOFTMAX,
    SCORE_KINDS,
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


def sports_scores() -> tuple[float, ...]:
    """与 CLASSES 索引对应的有效分数（sports 最高，总和为 1）。"""
    return tuple(0.91 if name == "sports" else 0.01 for name in CLASSES)


class FakeService(PredictService):
    model_id = "fake"
    display_name = "Fake 模型"

    def infer(self, title: str) -> ModelOutput:
        if title == "bad":
            return ModelOutput(pred_class="not_a_class")
        if title == "boom":
            raise InferenceError("推理失败")
        if title == "scored":
            return ModelOutput(
                pred_class="sports",
                scores=sports_scores(),
                score_kind=SCORE_KIND_RAW_SOFTMAX,
            )
        if title == "scored-bad-length":
            return ModelOutput(
                pred_class="sports",
                scores=(0.5, 0.5),
                score_kind=SCORE_KIND_RAW_SOFTMAX,
            )
        if title == "scored-nan":
            scores = list(sports_scores())
            scores[0] = float("nan")
            return ModelOutput("sports", tuple(scores), SCORE_KIND_RAW_SOFTMAX)
        if title == "scored-inf":
            scores = list(sports_scores())
            scores[0] = float("inf")
            return ModelOutput("sports", tuple(scores), SCORE_KIND_RAW_SOFTMAX)
        if title == "scored-out-of-range":
            scores = list(sports_scores())
            scores[0] = 1.5
            return ModelOutput("sports", tuple(scores), SCORE_KIND_RAW_SOFTMAX)
        if title == "scored-non-numeric":
            scores = list(sports_scores())
            scores[0] = "0.01"  # type: ignore[assignment]
            return ModelOutput("sports", tuple(scores), SCORE_KIND_RAW_SOFTMAX)
        if title == "scored-bool":
            scores = list(sports_scores())
            scores[0] = True  # type: ignore[assignment]
            return ModelOutput("sports", tuple(scores), SCORE_KIND_RAW_SOFTMAX)
        if title == "scored-kind-unknown":
            return ModelOutput("sports", sports_scores(), "calibrated")
        if title == "scored-pred-mismatch":
            return ModelOutput("finance", sports_scores(), SCORE_KIND_RAW_SOFTMAX)
        return ModelOutput("sports")


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


def score_checks() -> None:
    """B1 分数契约：PredictionResult / top_k / worker 字段与降级。"""
    classes = ["a", "b", "c", "d"]
    tie = PredictionResult(
        pred_class="b",
        latency_ms=1.0,
        scores=(0.1, 0.4, 0.4, 0.1),
        score_kind=SCORE_KIND_RAW_SOFTMAX,
    )
    check(
        "top_k desc + tie by class id",
        tie.top_k(classes, 3)
        == [
            {"class_id": "b", "score": 0.4},
            {"class_id": "c", "score": 0.4},
            {"class_id": "a", "score": 0.1},
        ],
        str(tie.top_k(classes, 3)),
    )
    check(
        "top_k k > classes returns all",
        [item["class_id"] for item in tie.top_k(classes, 99)]
        == ["b", "c", "a", "d"],
        str(tie.top_k(classes, 99)),
    )
    check("top_k default is 3", len(tie.top_k(classes)) == DEFAULT_TOP_K)
    check(
        "top_k without scores is empty",
        PredictionResult(pred_class="a", latency_ms=1.0).top_k(classes) == [],
    )
    for bad_k in (0, -1, True, "3", 1.5, None):
        try:
            tie.top_k(classes, bad_k)  # type: ignore[arg-type]
            rejected = False
        except ValueError:
            rejected = True
        check(f"top_k rejects k={bad_k!r}", rejected)

    unscored = PredictionResult(pred_class="sports", latency_ms=1.0)
    check(
        "PredictionResult score defaults",
        unscored.scores is None and unscored.score_kind is None,
    )
    check(
        "score_kind enum",
        SCORE_KINDS == {"raw_proba", "raw_fasttext", "raw_softmax"},
        str(SCORE_KINDS),
    )

    scored = FakeService(CLASSES)
    payload = scored.handle_payload({"title": "scored"})
    check(
        "scored worker payload fields",
        set(payload) == {"title", "pred_class", "scores", "score_kind", "top_k"},
        str(payload),
    )
    check(
        "scored worker payload has no data_source (adapter judges, not worker)",
        "data_source" not in payload,
        str(sorted(payload)),
    )
    check(
        "scored worker scores mapped by class id",
        isinstance(payload.get("scores"), dict)
        and len(payload["scores"]) == len(CLASSES)
        and payload["scores"]["sports"] == 0.91
        and payload.get("score_kind") == SCORE_KIND_RAW_SOFTMAX,
        str(payload.get("scores")),
    )
    top = payload.get("top_k")
    check(
        "scored worker top_k sorted and paired",
        isinstance(top, list)
        and len(top) == DEFAULT_TOP_K
        and top[0] == {"class_id": "sports", "score": 0.91}
        and [item["score"] for item in top]
        == sorted((item["score"] for item in top), reverse=True)
        and all(
            payload["scores"][item["class_id"]] == item["score"] for item in top
        ),
        str(top),
    )

    invalid_cases = {
        "scored-bad-length": "sports",
        "scored-nan": "sports",
        "scored-inf": "sports",
        "scored-out-of-range": "sports",
        "scored-non-numeric": "sports",
        "scored-bool": "sports",
        "scored-kind-unknown": "sports",
        "scored-pred-mismatch": "finance",
    }
    for title, expected_class in invalid_cases.items():
        body = scored.handle_payload({"title": title})
        check(
            f"invalid scores dropped ({title})",
            body.get("pred_class") == expected_class
            and "error" not in body
            and not {"scores", "score_kind", "top_k"} & set(body),
            str(body),
        )

    llm = LlmLikeService(CLASSES)
    body = llm.handle_payload({"title": "正常标题"})
    check(
        "LLM payload has no score fields",
        body == {"title": "正常标题", "pred_class": "sports"},
        str(body),
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

    response = client.post("/predict_api", json={"title": "scored"})
    body = response.get_json()
    check(
        "flask scored payload",
        response.status_code == 200
        and body["pred_class"] == "sports"
        and body["score_kind"] == SCORE_KIND_RAW_SOFTMAX
        and body["scores"]["sports"] == 0.91
        and len(body["top_k"]) == DEFAULT_TOP_K
        and body["top_k"][0] == {"class_id": "sports", "score": 0.91},
        str(body),
    )
    check(
        "flask scored payload has no data_source (worker contract)",
        "data_source" not in body,
        str(sorted(body)),
    )

    response = client.post("/predict_api", json={"title": "正常标题"})
    body = response.get_json()
    check(
        "flask unscored payload has no score fields",
        response.status_code == 200
        and body == {"title": "正常标题", "pred_class": "sports"},
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
    metadata_body = response.get_json()
    check(
        "flask metadata",
        response.status_code == 200
        and metadata_body
        == {"model_id": "fake", "display_name": "Fake 模型", "version": None},
    )
    check(
        "flask metadata has no data_source (adapter judges, not worker)",
        "data_source" not in metadata_body,
        str(metadata_body),
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
        if module == "04-LLM":
            check(
                "04-LLM does not fabricate scores",
                "scores=" not in service_text
                and "return ModelOutput(pred_class=pred)" in service_text,
                "LLM 无本地概率，不应输出 scores",
            )
        score_marks = {
            "01-rf": "predict_proba",
            "02-fasttext": "SCORE_KIND_RAW_FASTTEXT",
            "03-bert": "torch.softmax",
            "05-distill": "torch.softmax",
        }
        if module in score_marks:
            check(
                f"{module} computes real scores",
                score_marks[module] in service_text,
                score_marks[module],
            )


SCORE_PROVENANCE = {
    "01-rf": "SCORE_KIND_RAW_PROBA",
    "02-fasttext": "SCORE_KIND_RAW_FASTTEXT",
    "03-bert": "SCORE_KIND_RAW_SOFTMAX",
    "05-distill": "SCORE_KIND_RAW_SOFTMAX",
}
WORKER_SERVICE_FILES = {
    "01-rf": "rf_predict_service.py",
    "02-fasttext": "ft_predict_service.py",
    "03-bert": "bert_predict_service.py",
    "04-LLM": "llm_predict_service.py",
    "05-distill": "student_predict_service.py",
}


def _model_output_calls(tree: ast.Module) -> list[ast.Call]:
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ModelOutput"
        ):
            calls.append(node)
    return calls


def worker_score_field_checks() -> None:
    """B10：各 worker 的分数 / top_k / data_source 字段契约（静态，不加载模型）。

    - 4 个本地模型必须把 ``scores`` + ``score_kind`` 传给 ``ModelOutput``，
      且 ``score_kind`` 与模型来源一一对应（raw_proba / raw_fasttext / raw_softmax）；
    - LLM 无本地概率，**不得**伪造 ``scores``；
    - ``top_k`` 只由共享契约 ``tmf_common.prediction.handle_payload`` 生成，
      worker 不自行拼装（避免 5 份重复实现漂移）；
    - worker 响应**不含** ``data_source``：三态由网关适配器读取
      ``/metadata.display_name`` 判定（B11），worker 不伪造 live/mock。
    """
    shared_prediction = (PROJECT_ROOT / "tmf_common" / "prediction.py").read_text(
        encoding="utf-8"
    )
    check(
        "shared worker contract generates top_k, never data_source",
        "def top_k(" in shared_prediction
        and "data_source" not in shared_prediction,
        "tmf_common/prediction.py",
    )

    for module, filename in WORKER_SERVICE_FILES.items():
        path = PROJECT_ROOT / module / filename
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        calls = _model_output_calls(tree)
        keyword_names = {
            keyword.arg for call in calls for keyword in call.keywords if keyword.arg
        }

        if module == "04-LLM":
            check(
                f"{module}: ModelOutput has no scores/score_kind (no fabricated probs)",
                "scores" not in keyword_names and "score_kind" not in keyword_names
                and "SCORE_KIND_RAW_" not in text,
                str(sorted(keyword_names)),
            )
        else:
            const_name = SCORE_PROVENANCE[module]
            check(
                f"{module}: ModelOutput passes scores + score_kind",
                {"scores", "score_kind"} <= keyword_names,
                str(sorted(keyword_names)),
            )
            check(
                f"{module}: score_kind provenance is {const_name}",
                f"score_kind={const_name}" in text.replace(" ", ""),
                const_name,
            )
        check(
            f"{module}: worker response never adds data_source",
            "data_source" not in text,
            "data_source 只应由网关适配器判定（B11）",
        )
        check(
            f"{module}: top_k not reimplemented in worker (shared contract only)",
            "top_k" not in text,
            "应在 tmf_common.prediction 统一生成",
        )

    # LLM 动态 payload 同样不带分数字段 / data_source。
    llm = LlmLikeService(CLASSES)
    payload = llm.handle_payload({"title": "正常标题"})
    check(
        "LLM payload: no scores/top_k/data_source",
        payload == {"title": "正常标题", "pred_class": "sports"}
        and "data_source" not in payload,
        str(payload),
    )


def main() -> int:
    contract_checks()
    score_checks()
    payload_checks()
    flask_checks()
    env_checks()
    static_worker_checks()
    worker_score_field_checks()
    if FAILURES:
        print(f"RESULT FAIL ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
