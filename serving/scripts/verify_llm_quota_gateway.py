#!/usr/bin/env python
"""阶段 4B 隔离验收：网关 LLM 配额接入 + 竞技场排除 LLM。

只使用临时 SQLite、fake adapter、TestClient/ASGI app：
- 不启动 uvicorn/worker，不连接真实网关/Provider；
- 不写 serving/run/tmf.db；
- HMAC key 在测试运行时随机生成，不写入仓库；
- 不实现/调用管理员命令。

运行：
    $env:PYTHONPATH='E:\\AIModels\\test\\TouManFen\\venv-gateway\\Lib\\site-packages'
    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/verify_llm_quota_gateway.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from serving.adapters.protocol import AdapterResult  # noqa: E402
from serving.adapters.quotaed_llm import QuotaedLlmAdapter  # noqa: E402
from serving.gateway.errors import ApiError  # noqa: E402
from serving.gateway.main import create_app  # noqa: E402
from serving.services.arena import ArenaService  # noqa: E402
from serving.services.classification import ClassificationService  # noqa: E402
from serving.services.llm_quota import LlmQuotaStoreError  # noqa: E402

FAILURES: list[str] = []
TOTAL = 0
TEST_KEY = secrets.token_hex(32)
ENV_KEYS = (
    "TMF_LLM_QUOTA_HMAC_KEY",
    "TMF_DB_PATH",
    "TMF_TRUSTED_PROXY_CIDRS",
)
IP = "203.0.113.10"


def check(name: str, condition: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


def configure_env(db_path: Path, *, hmac: bool = True, trusted: str = "") -> None:
    for key in ENV_KEYS:
        os.environ.pop(key, None)
    if hmac:
        os.environ["TMF_LLM_QUOTA_HMAC_KEY"] = TEST_KEY
    os.environ["TMF_DB_PATH"] = str(db_path)
    if trusted:
        os.environ["TMF_TRUSTED_PROXY_CIDRS"] = trusted


def clear_env() -> None:
    for key in ENV_KEYS:
        os.environ.pop(key, None)


class LocalFake:
    model_id: str
    model_version: str | None = None

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.calls = 0

    async def predict(self, title: str, request_id: str) -> AdapterResult:
        self.calls += 1
        return AdapterResult(
            model_id=self.model_id,
            status="ok",
            latency_ms=3,
            pred_class="finance",
            data_source="mock",
        )


class BrokenQuota:
    """模拟只读/损坏/写失败：任何读都返回 store error。"""

    def get_usage(self, ip: str):
        raise LlmQuotaStoreError("forced quota store failure")


class FakeLlm:
    model_id = "llm"
    model_version: str | None = None

    def __init__(self) -> None:
        self.calls = 0
        self.mode = "ok"
        self.delay = 0.0

    async def predict(self, title: str, request_id: str) -> AdapterResult:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.mode == "timeout":
            return AdapterResult(
                model_id="llm",
                status="error",
                latency_ms=0,
                error_code="WORKER_TIMEOUT",
                error_message="fake timeout",
                data_source="unknown",
            )
        if self.mode == "provider_error":
            return AdapterResult(
                model_id="llm",
                status="error",
                latency_ms=0,
                error_code="MODEL_ERROR",
                error_message="fake provider error",
                data_source="unknown",
            )
        if self.mode == "invalid_output":
            return AdapterResult(
                model_id="llm",
                status="error",
                latency_ms=0,
                error_code="INVALID_MODEL_OUTPUT",
                error_message="fake invalid output",
                data_source="unknown",
            )
        return AdapterResult(
            model_id="llm",
            status="ok",
            latency_ms=5,
            pred_class="finance",
            data_source="mock",
        )


def install_fake_classifier(app, quota_store, llm_inner: FakeLlm):
    settings = app.state.settings
    registry = app.state.registry
    adapters: dict[str, Any] = {}
    for model_id in registry.all_model_ids():
        spec = registry.get(model_id)
        if spec is None:
            continue
        if spec.worker_id == "llm":
            adapters[model_id] = QuotaedLlmAdapter(llm_inner, quota_store)
        else:
            adapters[model_id] = LocalFake(model_id)
    service = ClassificationService(
        registry=registry,
        adapters=adapters,
        classes=settings.classes,
        max_title_chars=settings.max_title_chars,
        stats=app.state.stats,
        logger=logging.getLogger("verify-quota"),
        reject=settings.reject,
        compare_total_timeout_ms=settings.compare_total_timeout_ms,
        compare_concurrency=settings.compare_concurrency,
        batch_max_items=settings.batch_max_items,
        batch_max_total_chars=settings.batch_max_total_chars,
        batch_concurrency=settings.batch_concurrency,
        batch_total_timeout_ms=settings.batch_total_timeout_ms,
        llm_quota=quota_store,
        llm_quota_enforced=True,
    )
    app.state.classifier = service
    app.state.adapters = adapters
    return service


def install_fake_arena(app, root: Path, llm_inner: FakeLlm):
    settings = app.state.settings
    registry = app.state.registry
    adapters: dict[str, Any] = {}
    for model_id in registry.all_model_ids():
        spec = registry.get(model_id)
        if spec is None:
            continue
        if spec.worker_id == "llm":
            adapters[model_id] = llm_inner
        else:
            adapters[model_id] = LocalFake(model_id)
    sample = root / "arena.txt"
    sample.write_text("测试标题一\t0\n测试标题二\t1\n测试标题三\t2\n", encoding="utf-8")
    original = getattr(app.state, "arena", None)
    if original is not None:
        try:
            original.close()
        except Exception:
            pass
    arena = ArenaService(
        registry=registry,
        adapters=adapters,
        classes=settings.classes,
        logger=logging.getLogger("verify-quota-arena"),
        db_path=root / "arena.db",
        test_file=sample,
    )
    app.state.arena = arena
    return arena


def make_client(app, client_ip: str = IP) -> TestClient:
    return TestClient(app, client=(client_ip, 50000))


def error_code(response) -> str | None:
    try:
        return response.json().get("error", {}).get("code")
    except Exception:
        return None


class CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.entries: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        values = {
            key: value
            for key, value in record.__dict__.items()
            if key
            not in {
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "exc_info",
                "exc_text",
                "stack_info",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "processName",
                "process",
            }
        }
        self.entries.append(
            record.getMessage() + " " + json.dumps(values, default=str, ensure_ascii=False)
        )


def test_classify_and_persistence(root: Path, capture: CaptureHandler) -> None:
    print("\n=== 1. classify 1/2/3 允许、第 4 次 403、跨 app 持久 ===")
    db_path = root / "classify.db"
    configure_env(db_path)
    app = create_app()
    inner = FakeLlm()
    with make_client(app, IP) as client:
        install_fake_classifier(app, app.state.llm_quota, inner)
        statuses = []
        for _ in range(3):
            response = client.post(
                "/api/v1/classify",
                json={"title": "测试标题", "model": "llm"},
            )
            statuses.append(response.status_code)
        fourth = client.post(
            "/api/v1/classify",
            json={"title": "测试标题", "model": "llm"},
        )
        check("classify first 3 status 200", statuses == [200, 200, 200], str(statuses))
        check(
            "classify 4th 403 LLM_QUOTA_EXHAUSTED",
            fourth.status_code == 403 and error_code(fourth) == "LLM_QUOTA_EXHAUSTED",
            f"{fourth.status_code}/{error_code(fourth)}",
        )
        check("fake LLM adapter called exactly 3 times", inner.calls == 3, str(inner.calls))
        usage = app.state.llm_quota.get_usage(IP)
        check("quota used == 3 and blocked", usage.used == 3 and usage.blocked, str(usage))
        body_text = fourth.text
        check("403 body has no raw IP", IP not in body_text, body_text[:300])
        check("403 body has no HMAC key", TEST_KEY not in body_text, body_text[:300])

    # 新 app 使用同一临时 DB：第 4 次仍拒绝。
    configure_env(db_path)
    app2 = create_app()
    inner2 = FakeLlm()
    with make_client(app2, IP) as client:
        install_fake_classifier(app2, app2.state.llm_quota, inner2)
        fourth2 = client.post(
            "/api/v1/classify",
            json={"title": "测试标题", "model": "llm"},
        )
        check(
            "new app/store sees persisted 4th denial",
            fourth2.status_code == 403
            and error_code(fourth2) == "LLM_QUOTA_EXHAUSTED"
            and inner2.calls == 0,
            f"{fourth2.status_code}/{error_code(fourth2)} calls={inner2.calls}",
        )


def test_errors_consume(root: Path) -> None:
    print("\n=== 2. 超时/Provider 错误/无效输出各消耗一次且不退款 ===")
    db_path = root / "errors.db"
    configure_env(db_path)
    app = create_app()
    inner = FakeLlm()
    with make_client(app, "203.0.113.20") as client:
        service = install_fake_classifier(app, app.state.llm_quota, inner)
        cases = (
            ("timeout", 504, "WORKER_TIMEOUT"),
            ("provider_error", 502, "MODEL_ERROR"),
            ("invalid_output", 502, "INVALID_MODEL_OUTPUT"),
        )
        for index, (mode, expected_status, expected_code) in enumerate(cases):
            client_ip = f"203.0.113.{21 + index}"
            inner.mode = mode
            try:
                asyncio.run(
                    service.classify(
                        "测试标题",
                        "llm",
                        f"req-{mode}",
                        client_ip=client_ip,
                    )
                )
                check(f"{mode}: classify raises", False)
                continue
            except ApiError as exc:
                check(
                    f"{mode}: status/code mapping",
                    exc.status_code == expected_status and exc.code == expected_code,
                    f"{exc.status_code}/{exc.code}",
                )
            usage = app.state.llm_quota.get_usage(client_ip)
            check(f"{mode}: consumes exactly one quota", usage.used == 1, str(usage))
            check(f"{mode}: no refund", usage.used == 1, str(usage))
        check("fake LLM calls for 3 error cases", inner.calls == 3, str(inner.calls))


def test_concurrency_and_arena(root: Path) -> None:
    print("\n=== 3. 5 并发 classify 只允许 3 个；compare/stream/arena/batch ===")
    db_path = root / "concurrency.db"
    configure_env(db_path)
    app = create_app()
    inner = FakeLlm()
    with make_client(app, "203.0.113.40") as client:
        service = install_fake_classifier(app, app.state.llm_quota, inner)
        inner.delay = 0.05

        async def run_five() -> list[object]:
            return await asyncio.gather(
                *(
                    service.classify(
                        "测试标题",
                        "llm",
                        f"concurrent-{index}",
                        client_ip="203.0.113.41",
                    )
                    for index in range(5)
                ),
                return_exceptions=True,
            )

        results = asyncio.run(run_five())
        successes = [result for result in results if not isinstance(result, Exception)]
        quota_errors = [
            result
            for result in results
            if isinstance(result, ApiError)
            and result.code == "LLM_QUOTA_EXHAUSTED"
        ]
        check("5 concurrent classify: exactly 3 successes", len(successes) == 3, str(results))
        check("5 concurrent classify: exactly 2 quota errors", len(quota_errors) == 2, str(results))
        check("5 concurrent classify: adapter calls == 3", inner.calls == 3, str(inner.calls))
        check(
            "5 concurrent classify: DB used == 3",
            app.state.llm_quota.get_usage("203.0.113.41").used == 3,
        )

        # compare 不含 LLM 不扣额度。
        local_before = {
            model_id: app.state.adapters[model_id].calls
            for model_id in ("rf", "fasttext", "bert", "distill")
        }
        usage_before = app.state.llm_quota.get_usage("203.0.113.42").used
        local_compare = client.post(
            "/api/v1/compare",
            json={"title": "测试标题", "models": ["rf", "fasttext", "bert", "distill"]},
        )
        check("compare without LLM returns 200", local_compare.status_code == 200, str(local_compare.status_code))
        check(
            "compare without LLM does not consume quota",
            app.state.llm_quota.get_usage("203.0.113.42").used == usage_before,
        )
        check(
            "compare without LLM does not call LLM adapter",
            inner.calls == 3,
            str(inner.calls),
        )

        # compare 显式含 LLM 且额度可用：只为 LLM 调用 reserve 一次。
        llm_before = inner.calls
        compare_with_llm = client.post(
            "/api/v1/compare",
            json={"title": "测试标题", "models": ["rf", "llm"]},
        )
        check("compare with LLM returns 200", compare_with_llm.status_code == 200, str(compare_with_llm.status_code))
        check(
            "compare with LLM reserves exactly one quota",
            app.state.llm_quota.get_usage("203.0.113.40").used == 1,
        )
        check(
            "compare with LLM calls LLM once",
            inner.calls == llm_before + 1,
            f"{llm_before}->{inner.calls}",
        )

        # 耗尽 IP 后 compare/stream 必须在 fanout 前 403，本地 adapter 不启动。
        exhausted_ip = "203.0.113.50"
        with make_client(app, exhausted_ip) as exhausted_client:
            install_fake_classifier(app, app.state.llm_quota, inner)
            for _ in range(3):
                exhausted_client.post(
                    "/api/v1/classify",
                    json={"title": "测试标题", "model": "llm"},
                )
            local_calls_before = sum(
                getattr(app.state.adapters[mid], "calls", 0)
                for mid in ("rf", "fasttext", "bert", "distill")
                if hasattr(app.state.adapters.get(mid), "calls")
            )
            llm_calls_before = inner.calls
            compare = exhausted_client.post(
                "/api/v1/compare",
                json={"title": "测试标题", "models": ["rf", "llm"]},
            )
            check(
                "exhausted compare -> 403 before fanout",
                compare.status_code == 403
                and error_code(compare) == "LLM_QUOTA_EXHAUSTED",
                f"{compare.status_code}/{error_code(compare)}",
            )
            local_calls_after = sum(
                getattr(app.state.adapters[mid], "calls", 0)
                for mid in ("rf", "fasttext", "bert", "distill")
                if hasattr(app.state.adapters.get(mid), "calls")
            )
            check("exhausted compare: no local adapter started", local_calls_after == local_calls_before)
            check("exhausted compare: no LLM adapter started", inner.calls == llm_calls_before)

            stream = exhausted_client.get(
                "/api/v1/compare/stream",
                params={"title": "测试标题", "models": "rf,llm"},
            )
            check(
                "exhausted stream -> 403 before StreamingResponse",
                stream.status_code == 403
                and error_code(stream) == "LLM_QUOTA_EXHAUSTED",
                f"{stream.status_code}/{error_code(stream)}",
            )
            check("exhausted stream: no LLM adapter started", inner.calls == llm_calls_before)

        # stream 允许时只 reserve 一次 LLM。
        stream_ip = "203.0.113.60"
        inner.calls = 0
        with make_client(app, stream_ip) as stream_client:
            install_fake_classifier(app, app.state.llm_quota, inner)
            stream = stream_client.get(
                "/api/v1/compare/stream",
                params={"title": "测试标题", "models": "rf,llm"},
            )
            check("allowed stream returns 200", stream.status_code == 200, str(stream.status_code))
            check("allowed stream emits model event", "event: model" in stream.text, stream.text[:200])
            check(
                "allowed stream reserves exactly one LLM quota",
                app.state.llm_quota.get_usage(stream_ip).used == 1,
            )
            check("allowed stream calls LLM once", inner.calls == 1, str(inner.calls))

        # batch LLM 仍拒绝且不扣配额。
        batch_ip = "203.0.113.70"
        with make_client(app, batch_ip) as batch_client:
            install_fake_classifier(app, app.state.llm_quota, inner)
            batch = batch_client.post(
                "/api/v1/classify/batch",
                json={
                    "model": "llm",
                    "items": [{"item_id": "r000001", "title": "测试标题"}],
                },
            )
            check(
                "batch LLM still 400 BATCH_LLM_NOT_ALLOWED",
                batch.status_code == 400 and error_code(batch) == "BATCH_LLM_NOT_ALLOWED",
                f"{batch.status_code}/{error_code(batch)}",
            )
            check(
                "batch LLM does not consume quota",
                app.state.llm_quota.get_usage(batch_ip).used == 0,
            )

        # 竞技场移除 LLM：/arena/next 不调用 fake LLM。
        internal_llm = FakeLlm()
        install_fake_arena(app, root, internal_llm)
        arena = client.get("/api/v1/arena/next")
        arena_body = arena.json()
        check("arena next 200", arena.status_code == 200, str(arena.status_code))
        check(
            "arena next returns 4 local candidates",
            len(arena_body.get("candidates") or []) == 4,
            str(arena_body.get("candidates")),
        )
        check("arena next does not call fake LLM adapter", internal_llm.calls == 0, str(internal_llm.calls))


def test_proxy_identity(root: Path) -> None:
    print("\n=== 4. 未授权 XFF 忽略；可信代理显式配置后才生效 ===")
    # 默认 trusted 为空：伪造 XFF 不能改变 quota key。
    db_path = root / "proxy-default.db"
    configure_env(db_path)
    app = create_app()
    inner = FakeLlm()
    with make_client(app, "203.0.113.80") as client:
        install_fake_classifier(app, app.state.llm_quota, inner)
        response = client.post(
            "/api/v1/classify",
            headers={"X-Forwarded-For": "198.51.100.77"},
            json={"title": "测试标题", "model": "llm"},
        )
        check("default trusted empty classify 200", response.status_code == 200, str(response.status_code))
        check(
            "untrusted XFF ignored: peer quota used",
            app.state.llm_quota.get_usage("203.0.113.80").used == 1,
        )
        check(
            "untrusted XFF ignored: spoofed IP has no quota",
            app.state.llm_quota.get_usage("198.51.100.77").used == 0,
        )

    # 显式可信代理：peer=10.0.0.5，XFF=198.51.100.77 -> 后者为 quota key。
    db_path = root / "proxy-trusted.db"
    configure_env(db_path, trusted="10.0.0.0/8")
    app = create_app()
    inner = FakeLlm()
    with make_client(app, "10.0.0.5") as client:
        install_fake_classifier(app, app.state.llm_quota, inner)
        response = client.post(
            "/api/v1/classify",
            headers={"X-Forwarded-For": "198.51.100.77"},
            json={"title": "测试标题", "model": "llm"},
        )
        check("trusted proxy classify 200", response.status_code == 200, str(response.status_code))
        check(
            "trusted proxy uses forwarded client quota",
            app.state.llm_quota.get_usage("198.51.100.77").used == 1,
        )
        check(
            "trusted proxy does not charge proxy peer",
            app.state.llm_quota.get_usage("10.0.0.5").used == 0,
        )


def test_store_unavailable(root: Path, capture: CaptureHandler) -> None:
    print("\n=== 5. quota store 不可用时 fail-closed 503，不调用 adapter ===")
    db_path = root / "missing-key.db"
    configure_env(db_path, hmac=False)
    app = create_app()
    inner = FakeLlm()
    with make_client(app, "203.0.113.90") as client:
        # 真实 classifier 使用包装器 + store=None；内部 LlmAdapter 不应被调用。
        response = client.post(
            "/api/v1/classify",
            json={"title": "测试标题", "model": "llm"},
        )
        check(
            "missing quota config -> 503 LLM_QUOTA_UNAVAILABLE",
            response.status_code == 503
            and error_code(response) == "LLM_QUOTA_UNAVAILABLE",
            f"{response.status_code}/{error_code(response)}",
        )

        # compare fanout 前也应 503，不启动 fake 本地模型。
        install_fake_classifier(app, None, inner)
        compare = client.post(
            "/api/v1/compare",
            json={"title": "测试标题", "models": ["rf", "llm"]},
        )
        check(
            "missing quota store compare -> 503 before fanout",
            compare.status_code == 503
            and error_code(compare) == "LLM_QUOTA_UNAVAILABLE",
            f"{compare.status_code}/{error_code(compare)}",
        )
        check("missing quota compare: fake LLM not called", inner.calls == 0, str(inner.calls))

        # store 读写故障：预检/包装器均 fail-closed，fake adapter 不启动。
        install_fake_classifier(app, BrokenQuota(), inner)
        client = make_client(app, "203.0.113.91")
        with client:
            broken = client.post(
                "/api/v1/classify",
                json={"title": "测试标题", "model": "llm"},
            )
            check(
                "broken quota store -> 503 LLM_QUOTA_UNAVAILABLE",
                broken.status_code == 503
                and error_code(broken) == "LLM_QUOTA_UNAVAILABLE",
                f"{broken.status_code}/{error_code(broken)}",
            )
            check("broken quota store: fake LLM not called", inner.calls == 0, str(inner.calls))


def test_database_privacy(root: Path) -> None:
    print("\n=== 6. quota DB 不含明文 IP/标题/payload；日志无明文 IP/key ===")
    db_path = root / "privacy.db"
    configure_env(db_path)
    app = create_app()
    inner = FakeLlm()
    test_ip = "203.0.113.100"
    test_title = "隐私验收标题-不应入库"
    with make_client(app, test_ip) as client:
        install_fake_classifier(app, app.state.llm_quota, inner)
        for _ in range(3):
            client.post(
                "/api/v1/classify",
                json={"title": test_title, "model": "llm"},
            )
    conn = sqlite3.connect(str(db_path))
    try:
        values: list[str] = []
        for table in ("llm_ip_quota", "llm_quota_event"):
            rows = conn.execute(f"SELECT * FROM {table}").fetchall()
            for row in rows:
                values.extend(str(value) for value in row if value is not None)
    finally:
        conn.close()
    joined = "\n".join(values)
    check("quota DB has no plaintext IP", test_ip not in joined)
    check("quota DB has no title", test_title not in joined)
    check("quota DB has no HMAC key", TEST_KEY not in joined)
    check("quota DB has no provider payload keys", "payload" not in joined.lower())


def main() -> int:
    gateway_logger = logging.getLogger("tmf.gateway")
    capture = CaptureHandler()
    gateway_logger.addHandler(capture)
    try:
        with tempfile.TemporaryDirectory(prefix="tmf-quota-gateway-") as raw_tmp:
            root = Path(raw_tmp)
            test_classify_and_persistence(root, capture)
            test_errors_consume(root)
            test_concurrency_and_arena(root)
            test_proxy_identity(root)
            test_store_unavailable(root, capture)
            test_database_privacy(root)
    finally:
        gateway_logger.removeHandler(capture)
        clear_env()

    log_text = "\n".join(capture.entries)
    check("logs do not contain plaintext test IP", IP not in log_text)
    check("logs do not contain HMAC key", TEST_KEY not in log_text)
    check("logs do not contain test title", "测试标题" not in log_text)
    if FAILURES:
        print(f"\nRESULT FAIL ({len(FAILURES)} failed / {TOTAL} checks)")
        for name in FAILURES:
            print(f"  FAILED: {name}")
        return 1
    print(f"\nRESULT PASS ({TOTAL} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
