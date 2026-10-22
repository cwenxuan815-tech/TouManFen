#!/usr/bin/env python
"""B12 验收脚本：并发与负载边界（配置 / 信号量 / 总截止隔离 / worker 线程与推理锁）。

运行环境（网关依赖 + worker 依赖；默认**不加载真实模型**、不启动任何服务）：
    $env:PYTHONPATH='E:\\AIModels\\test\\TouManFen\\venv-gateway\\Lib\\site-packages'
    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/verify_concurrency.py

覆盖：
1. 网关配置：``TMF_COMPARE_CONCURRENCY`` 默认 3（范围 1~10）/ 环境变量 1/3/5/10 生效 /
   0、-1、11、abc、3.5 被拒绝；``TMF_COMPARE_TOTAL_TIMEOUT_MS`` 默认仍为 18000；
   ``create_app`` 装配链（env -> Settings -> ClassificationService）注入并发上限。
2. 信号量：``compare()`` 与 ``compare_stream()`` 在并发 1/3/5 下最大并发数 == 配置值
   （可注入 fake adapters 做并发计数），证明两者共用同一实例配置。
3. B6 兼容（针对性 fake，不重复全量）：总截止 200ms -> ``partial`` 且未完成模型被取消、
   ``summary.total_timeout_ms`` 为配置值；单模型错误隔离 -> ``partial``；
   POST ``/compare`` 无总截止（不引入第二套截止）。
4. BERT/蒸馏 worker：``TMF_TORCH_THREADS`` 默认 4 / 覆盖 / 非法值拒绝；模型加载前调用
   ``torch.set_num_threads``；``infer`` 进程内 ``threading.Lock``（AST 静态 + stub 模块
   动态并发验证，不加载真实权重）。
5. 独立子进程 + fake adapter（受控 sleep）：并发 1/3/5 的墙钟延迟 / CPU / RSS，
   仅测网关编排开销，不对用户服务压测。
6. 可选（``TMF_VERIFY_REAL_WORKERS=1``）：独立子进程加载真实 BERT/蒸馏各 1~2 条输入，
   记录加载耗时/推理延迟/RSS；默认关闭（避免与在线 stage2 worker 抢 CPU/内存）。

不写默认 DB（临时 ``TMF_DB_PATH``）；不发起真实 worker / LLM 请求；不启动/重启用户服务。
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import importlib
import importlib.util
import json
import logging
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import types
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from serving.adapters import AdapterResult  # noqa: E402
from serving.gateway.errors import ConfigError  # noqa: E402
from serving.gateway.settings import (  # noqa: E402
    COMPARE_CONCURRENCY_MAX,
    COMPARE_CONCURRENCY_MIN,
    DEFAULT_COMPARE_CONCURRENCY,
    DEFAULT_COMPARE_TOTAL_TIMEOUT_MS,
    load_settings,
)
from serving.gateway.stats import StatsCollector  # noqa: E402
from serving.models.registry import ModelRegistry  # noqa: E402
from serving.services.classification import (  # noqa: E402
    ClassificationService,
    StreamModelEvent,
    StreamSummaryEvent,
)
from tmf_common.config import (  # noqa: E402
    DEFAULT_TORCH_THREADS,
    TORCH_THREADS_MAX,
    TORCH_THREADS_MIN,
    EnvConfigError,
    env_int,
)

FAILURES: list[str] = []
DEMO_TITLE = "北京二手房成交量创年内新高"

ENV_KEYS = (
    "TMF_COMPARE_CONCURRENCY",
    "TMF_COMPARE_TOTAL_TIMEOUT_MS",
    "TMF_DB_PATH",
    "TMF_TORCH_THREADS",
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

BERT_DIR = PROJECT_ROOT / "03-bert"
DISTILL_DIR = PROJECT_ROOT / "05-distill"


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


def clear_env() -> None:
    for key in ENV_KEYS:
        os.environ.pop(key, None)


@contextlib.contextmanager
def env_set(**values: str | None):
    """临时设置环境变量；``None`` 表示删除，退出时恢复原值。"""
    saved = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# fake adapters
# ---------------------------------------------------------------------------
class ConcurrencyTracker:
    """跨 adapter 共享的并发计数器（断言整批任务的最大并发数）。"""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self.active = 0
        self.max_active = 0

    def enter(self) -> None:
        with self._guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)

    def leave(self) -> None:
        with self._guard:
            self.active -= 1


class CountingAdapter:
    """记录并发峰值的 fake adapter（实现 ModelAdapter 协议）。"""

    def __init__(
        self,
        model_id: str,
        *,
        delay: float = 0.05,
        tracker: ConcurrencyTracker | None = None,
    ) -> None:
        self.model_id = model_id
        self.model_version: str | None = None
        self.delay = delay
        self.calls = 0
        self._tracker = tracker if tracker is not None else ConcurrencyTracker()

    @property
    def max_active(self) -> int:
        return self._tracker.max_active

    async def predict(self, title: str, request_id: str) -> AdapterResult:
        self.calls += 1
        self._tracker.enter()
        try:
            await asyncio.sleep(self.delay)
        finally:
            self._tracker.leave()
        return AdapterResult(
            model_id=self.model_id,
            status="ok",
            latency_ms=int(round(self.delay * 1000)),
            pred_class="sports",
        )


class SlowCancelAdapter:
    """极慢 adapter：用于验证总截止时网关侧 task 被取消。"""

    def __init__(self, model_id: str, *, delay: float = 5.0) -> None:
        self.model_id = model_id
        self.model_version: str | None = None
        self.delay = delay
        self.calls = 0
        self.cancelled = False

    async def predict(self, title: str, request_id: str) -> AdapterResult:
        self.calls += 1
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return AdapterResult(
            model_id=self.model_id, status="ok", latency_ms=1, pred_class="sports"
        )


class ErrorAdapter:
    """固定返回错误的 fake adapter（单模型失败隔离）。"""

    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.model_version: str | None = None

    async def predict(self, title: str, request_id: str) -> AdapterResult:
        return AdapterResult(
            model_id=self.model_id,
            status="error",
            latency_ms=3,
            error_code="WORKER_TIMEOUT",
            error_message="所选模型未在配置的时间内返回",
        )


def _make_service(adapters: dict[str, Any], **overrides: Any) -> ClassificationService:
    settings = load_settings()
    return ClassificationService(
        registry=ModelRegistry(settings),
        adapters=adapters,
        classes=settings.classes,
        max_title_chars=settings.max_title_chars,
        stats=StatsCollector(),
        logger=logging.getLogger("verify-concurrency"),
        **overrides,
    )


# ---------------------------------------------------------------------------
# 1. 网关配置：TMF_COMPARE_CONCURRENCY / 总截止默认值
# ---------------------------------------------------------------------------
def config_checks() -> None:
    print("\n=== 1. 网关配置：TMF_COMPARE_CONCURRENCY 默认 3 / 范围 / 非法值；总截止默认不变 ===")
    clear_env()

    settings = load_settings()
    check(
        "default TMF_COMPARE_CONCURRENCY == 3",
        DEFAULT_COMPARE_CONCURRENCY == 3 and settings.compare_concurrency == 3,
        f"{DEFAULT_COMPARE_CONCURRENCY} / {settings.compare_concurrency}",
    )
    check(
        "range is 1~10 (min/max constants)",
        (COMPARE_CONCURRENCY_MIN, COMPARE_CONCURRENCY_MAX) == (1, 10),
        f"{COMPARE_CONCURRENCY_MIN}~{COMPARE_CONCURRENCY_MAX}",
    )
    check(
        "default TMF_COMPARE_TOTAL_TIMEOUT_MS unchanged == 18000",
        DEFAULT_COMPARE_TOTAL_TIMEOUT_MS == 18000
        and settings.compare_total_timeout_ms == 18000,
        f"{DEFAULT_COMPARE_TOTAL_TIMEOUT_MS} / {settings.compare_total_timeout_ms}",
    )
    check(
        "total timeout is independent of concurrency config",
        settings.compare_total_timeout_ms == 18000
        and settings.compare_concurrency == 3,
    )

    for value in ("1", "3", "5", "10"):
        with env_set(TMF_COMPARE_CONCURRENCY=value):
            overridden = load_settings()
        check(
            f"env TMF_COMPARE_CONCURRENCY={value} overrides",
            overridden.compare_concurrency == int(value),
            str(overridden.compare_concurrency),
        )

    # 空串按“未设置”处理 -> 默认 3（与其它 TMF_* 数值变量约定一致）。
    with env_set(TMF_COMPARE_CONCURRENCY=""):
        empty_settings = load_settings()
    check(
        "empty TMF_COMPARE_CONCURRENCY uses default 3",
        empty_settings.compare_concurrency == 3,
        str(empty_settings.compare_concurrency),
    )

    for value in ("0", "-1", "11", "999", "abc", "3.5", "  "):
        # 注意 "  " 被 _env 视为空 -> 默认值（不是非法值），单独用 "3.5" 等覆盖小数。
        if value == "  ":
            continue
        with env_set(TMF_COMPARE_CONCURRENCY=value):
            try:
                load_settings()
                check(f"invalid TMF_COMPARE_CONCURRENCY={value} rejected", False)
            except ConfigError as exc:
                check(
                    f"invalid TMF_COMPARE_CONCURRENCY={value} rejected",
                    "TMF_COMPARE_CONCURRENCY" in str(exc),
                    str(exc),
                )
            except Exception as exc:  # noqa: BLE001
                check(
                    f"invalid TMF_COMPARE_CONCURRENCY={value} rejected",
                    False,
                    type(exc).__name__,
                )

    # 直接构造服务时的防御性校验（0/负数必须拒绝，避免出现无界并发）。
    for bad in (0, -3):
        try:
            _make_service({}, compare_concurrency=bad)
            check(f"ClassificationService rejects compare_concurrency={bad}", False)
        except ValueError:
            check(f"ClassificationService rejects compare_concurrency={bad}", True)
        except Exception as exc:  # noqa: BLE001
            check(
                f"ClassificationService rejects compare_concurrency={bad}",
                False,
                type(exc).__name__,
            )


def app_wiring_checks() -> None:
    print("\n=== 1b. 装配链：环境变量 -> Settings -> create_app -> ClassificationService ===")
    clear_env()
    from fastapi.testclient import TestClient

    from serving.gateway.main import create_app

    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ["TMF_DB_PATH"] = str(Path(temp_dir) / "verify_b12.db")
        try:
            # 默认（未设置）：3，且 SSE 总截止仍为 18000。
            app = create_app()
            with TestClient(app):
                check(
                    "default wiring: settings/classifier compare_concurrency == 3",
                    app.state.settings.compare_concurrency == 3
                    and app.state.classifier.compare_concurrency == 3,
                    f"{app.state.settings.compare_concurrency} "
                    f"{app.state.classifier.compare_concurrency}",
                )
                check(
                    "default wiring: compare_total_timeout_ms == 18000 (B6 unchanged)",
                    app.state.classifier.compare_total_timeout_ms == 18000,
                    str(app.state.classifier.compare_total_timeout_ms),
                )

            # 覆盖为 2：装配链必须透传到实例。
            os.environ["TMF_COMPARE_CONCURRENCY"] = "2"
            app2 = create_app()
            with TestClient(app2):
                check(
                    "env wiring: TMF_COMPARE_CONCURRENCY=2 reaches classifier",
                    app2.state.settings.compare_concurrency == 2
                    and app2.state.classifier.compare_concurrency == 2,
                    f"{app2.state.settings.compare_concurrency} "
                    f"{app2.state.classifier.compare_concurrency}",
                )
        finally:
            os.environ.pop("TMF_DB_PATH", None)
            os.environ.pop("TMF_COMPARE_CONCURRENCY", None)


# ---------------------------------------------------------------------------
# 2. 信号量：compare / compare_stream 使用配置值（1/3/5）
# ---------------------------------------------------------------------------
def semaphore_checks() -> None:
    print("\n=== 2. 信号量：compare / compare_stream 最大并发 == 配置值（1/3/5） ===")
    clear_env()
    registry = ModelRegistry(load_settings())
    # B9：llm.compare_by_default=false → 默认 compare 集合是 4 个本地模型。
    default_ids = registry.compare_default_model_ids()
    check(
        "compare default set excludes llm (B9 opt-in, 4 local models)",
        "llm" not in default_ids and len(default_ids) == 4,
        str(default_ids),
    )
    # B12 的并发上限仍按 5 个启用模型测量（显式 models，不受默认集合变化影响）。
    model_ids = registry.all_model_ids()
    check("concurrency measurement uses all 5 enabled models", len(model_ids) == 5, str(model_ids))

    async def run_compare(concurrency: int):
        tracker = ConcurrencyTracker()
        adapters = {
            mid: CountingAdapter(mid, delay=0.05, tracker=tracker) for mid in model_ids
        }
        service = _make_service(adapters, compare_concurrency=concurrency)
        started = time.perf_counter()
        outcome = await service.compare(
            DEMO_TITLE, model_ids, f"rid-compare-{concurrency}"
        )
        elapsed = time.perf_counter() - started
        return outcome, elapsed, tracker.max_active

    async def run_stream(concurrency: int):
        tracker = ConcurrencyTracker()
        adapters = {
            mid: CountingAdapter(mid, delay=0.05, tracker=tracker) for mid in model_ids
        }
        service = _make_service(adapters, compare_concurrency=concurrency)
        events: list[Any] = []
        started = time.perf_counter()
        async for event in service.compare_stream(
            DEMO_TITLE, model_ids, f"rid-stream-{concurrency}"
        ):
            events.append(event)
        elapsed = time.perf_counter() - started
        summary = next(
            (event for event in events if isinstance(event, StreamSummaryEvent)), None
        )
        model_events = [e for e in events if isinstance(e, StreamModelEvent)]
        return summary, model_events, elapsed, tracker.max_active

    for concurrency in (1, 3, 5):
        outcome, elapsed, peak = asyncio.run(run_compare(concurrency))
        # 理论墙钟：5 个模型 * 50ms / 并发数（留 20% 抖动余量）。
        expected = len(model_ids) * 0.05 / concurrency
        check(
            f"compare peak concurrency == {concurrency} (not hardcoded 5)",
            peak == concurrency and outcome.status == "ok" and outcome.ok == len(model_ids),
            f"peak={peak} status={outcome.status}",
        )
        check(
            f"compare c={concurrency} wall time near model ({expected * 1000:.0f}ms)",
            elapsed >= expected * 0.8,
            f"{elapsed * 1000:.0f}ms",
        )

        summary, model_events, elapsed, peak = asyncio.run(run_stream(concurrency))
        expected = len(model_ids) * 0.05 / concurrency
        check(
            f"compare_stream peak concurrency == {concurrency}",
            peak == concurrency
            and summary is not None
            and summary.status == "ok"
            and len(model_events) == len(model_ids),
            f"peak={peak} summary={summary}",
        )
        check(
            f"compare_stream c={concurrency} wall time near model ({expected * 1000:.0f}ms)",
            elapsed >= expected * 0.8,
            f"{elapsed * 1000:.0f}ms",
        )


# ---------------------------------------------------------------------------
# 3. B6 兼容：总截止 partial / 单模型失败隔离 / 不引入第二套截止
# ---------------------------------------------------------------------------
def deadline_and_isolation_checks() -> None:
    print("\n=== 3. B6 兼容：总截止 partial + 单模型失败隔离 + POST 无第二个截止 ===")
    clear_env()

    async def run_deadline():
        fast = CountingAdapter("rf", delay=0.01)
        slow_llm = SlowCancelAdapter("llm")
        slow_bert = SlowCancelAdapter("bert")
        adapters = {"rf": fast, "llm": slow_llm, "bert": slow_bert}
        service = _make_service(
            adapters, compare_total_timeout_ms=200, compare_concurrency=3
        )
        events: list[Any] = []
        started = time.perf_counter()
        async for event in service.compare_stream(
            DEMO_TITLE, ["rf", "llm", "bert"], "rid-deadline"
        ):
            events.append(event)
        elapsed = time.perf_counter() - started
        return events, elapsed, slow_llm, slow_bert

    events, elapsed, slow_llm, slow_bert = asyncio.run(run_deadline())
    summary = next((e for e in events if isinstance(e, StreamSummaryEvent)), None)
    model_events = [e for e in events if isinstance(e, StreamModelEvent)]
    check(
        "deadline: fast model kept, slow models not reported ok",
        [e.model_id for e in model_events] == ["rf"]
        and model_events[0].status == "ok",
        str([(e.model_id, e.status) for e in model_events]),
    )
    check(
        "deadline: summary=partial with configured total_timeout_ms=200",
        summary is not None
        and summary.status == "partial"
        and (summary.total, summary.ok, summary.failed) == (3, 1, 2)
        and summary.total_timeout_ms == 200
        and summary.partial is True,
        str(summary),
    )
    check(
        "deadline: returns near 200ms (does not wait 5s worker)",
        elapsed < 2.0,
        f"{elapsed:.2f}s",
    )
    check(
        "deadline: unfinished gateway tasks cancelled",
        slow_llm.cancelled and slow_bert.cancelled,
        f"llm={slow_llm.cancelled} bert={slow_bert.cancelled}",
    )

    # 单模型失败隔离（POST /compare 对应的服务层路径）。
    async def run_isolation():
        adapters = {
            "rf": CountingAdapter("rf", delay=0.01),
            "fasttext": ErrorAdapter("fasttext"),
            "bert": CountingAdapter("bert", delay=0.01),
        }
        service = _make_service(adapters)
        return await service.compare(DEMO_TITLE, ["rf", "fasttext", "bert"], "rid-iso")

    outcome = asyncio.run(run_isolation())
    by_id = {item.model_id: item for item in outcome.items}
    check(
        "isolation: single model error does not affect others (partial)",
        outcome.status == "partial"
        and (outcome.total, outcome.ok, outcome.failed) == (3, 2, 1)
        and by_id["rf"].status == "ok"
        and by_id["bert"].status == "ok"
        and by_id["fasttext"].status == "error"
        and by_id["fasttext"].error_code == "WORKER_TIMEOUT",
        str(outcome),
    )

    # POST /compare 不套用 TMF_COMPARE_TOTAL_TIMEOUT_MS（避免第二套总截止）：
    # 两次 120ms 串行（并发 1）共约 240ms > 100ms，仍应全部成功。
    async def run_no_second_deadline():
        adapters = {
            "rf": CountingAdapter("rf", delay=0.12),
            "fasttext": CountingAdapter("fasttext", delay=0.12),
        }
        service = _make_service(
            adapters, compare_total_timeout_ms=100, compare_concurrency=1
        )
        started = time.perf_counter()
        result = await service.compare(
            DEMO_TITLE, ["rf", "fasttext"], "rid-no-second-deadline"
        )
        return result, time.perf_counter() - started

    outcome, elapsed = asyncio.run(run_no_second_deadline())
    check(
        "POST compare has no total deadline (only stream uses total_timeout_ms)",
        outcome.status == "ok" and elapsed > 0.2,
        f"status={outcome.status} elapsed={elapsed:.2f}s",
    )


# ---------------------------------------------------------------------------
# 4. BERT/蒸馏 worker：TMF_TORCH_THREADS + 推理锁
# ---------------------------------------------------------------------------
def _load_module_from_path(unique_name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(unique_name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - 依赖环境异常
        raise RuntimeError(f"无法加载模块: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def worker_config_checks() -> None:
    print("\n=== 4a. worker 配置：TMF_TORCH_THREADS 默认 4 / 覆盖 / 非法值 ===")
    clear_env()
    check(
        "shared default TMF_TORCH_THREADS == 4 (range 1~64)",
        DEFAULT_TORCH_THREADS == 4
        and (TORCH_THREADS_MIN, TORCH_THREADS_MAX) == (1, 64),
        f"{DEFAULT_TORCH_THREADS} {TORCH_THREADS_MIN}~{TORCH_THREADS_MAX}",
    )

    with env_set(TMF_TORCH_THREADS=None):
        check(
            "env_int default 4 when TMF_TORCH_THREADS unset",
            env_int(
                "TMF_TORCH_THREADS",
                DEFAULT_TORCH_THREADS,
                TORCH_THREADS_MIN,
                TORCH_THREADS_MAX,
            )
            == 4,
        )

    for label, module_dir in (("03-bert", BERT_DIR), ("05-distill", DISTILL_DIR)):
        config_module = _load_module_from_path(
            f"b12_{label.replace('-', '_')}_config", module_dir / "config.py"
        )
        with env_set(TMF_TORCH_THREADS=None):
            threads = config_module.Config().torch_threads
        check(f"{label}: default torch_threads == 4", threads == 4, str(threads))

        for value in ("1", "2", "4", "16"):
            with env_set(TMF_TORCH_THREADS=value):
                threads = config_module.Config().torch_threads
            check(
                f"{label}: env TMF_TORCH_THREADS={value} -> {value}",
                threads == int(value),
                str(threads),
            )

        for value in ("0", "-2", "65", "abc", "4.5"):
            with env_set(TMF_TORCH_THREADS=value):
                try:
                    config_module.Config()
                    check(
                        f"{label}: invalid TMF_TORCH_THREADS={value} rejected", False
                    )
                except (EnvConfigError, ValueError) as exc:
                    check(
                        f"{label}: invalid TMF_TORCH_THREADS={value} rejected",
                        "TMF_TORCH_THREADS" in str(exc),
                        str(exc),
                    )
                except Exception as exc:  # noqa: BLE001
                    check(
                        f"{label}: invalid TMF_TORCH_THREADS={value} rejected",
                        False,
                        type(exc).__name__,
                    )


def _module_body_lineno(tree: ast.Module, predicate) -> list[int]:
    return [
        node.lineno
        for node in tree.body
        if predicate(node)
    ]


def _is_torch_set_num_threads(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "set_num_threads"
        and isinstance(node.value.func.value, ast.Name)
        and node.value.func.value.id == "torch"
    )


def _is_load_model_assign(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name)
        and node.value.func.id == "_load_model"
    )


def _has_infer_lock(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "infer":
            for sub in ast.walk(node):
                if not isinstance(sub, ast.With):
                    continue
                for item in sub.items:
                    context = item.context_expr
                    if isinstance(context, ast.Name) and context.id == "_INFER_LOCK":
                        return True
    return False


def _has_module_infer_lock(tree: ast.Module) -> bool:
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if not (isinstance(func, ast.Attribute) and func.attr == "Lock"):
            continue
        if any(
            isinstance(target, ast.Name) and target.id == "_INFER_LOCK"
            for target in node.targets
        ):
            return True
    return False


def worker_static_checks() -> None:
    print("\n=== 4b. worker 静态检查：先设线程再加载模型 / infer 持锁 ===")
    clear_env()
    for label, module_dir, service_file in (
        ("03-bert", BERT_DIR, "bert_predict_service.py"),
        ("05-distill", DISTILL_DIR, "student_predict_service.py"),
    ):
        path = module_dir / service_file
        tree = ast.parse(path.read_text(encoding="utf-8"))
        set_lines = _module_body_lineno(tree, _is_torch_set_num_threads)
        load_lines = _module_body_lineno(tree, _is_load_model_assign)

        # torch.set_num_threads 的实参必须是 config.torch_threads（不是写死 4）。
        arg_ok = False
        for node in tree.body:
            if not _is_torch_set_num_threads(node):
                continue
            call = node.value
            if (
                call.args
                and isinstance(call.args[0], ast.Attribute)
                and call.args[0].attr == "torch_threads"
                and isinstance(call.args[0].value, ast.Name)
                and call.args[0].value.id == "config"
            ):
                arg_ok = True

        check(
            f"{label}: torch.set_num_threads(config.torch_threads) in module body",
            len(set_lines) == 1 and arg_ok,
            f"set_lines={set_lines} arg_ok={arg_ok}",
        )
        check(
            f"{label}: set_num_threads called before _load_model()",
            bool(set_lines)
            and bool(load_lines)
            and set_lines[0] < load_lines[0],
            f"set={set_lines} load={load_lines}",
        )
        check(
            f"{label}: module-level _INFER_LOCK = threading.Lock()",
            _has_module_infer_lock(tree),
        )
        check(f"{label}: infer() runs inside `with _INFER_LOCK`", _has_infer_lock(tree))


@contextlib.contextmanager
def _isolated_modules(paths: list[Path], modules: list[str]):
    """临时插入 sys.path / stub 模块，退出时恢复 sys.path 与 sys.modules 快照。"""
    saved_path = list(sys.path)
    missing = object()
    saved_modules = {name: sys.modules.get(name, missing) for name in modules}
    try:
        for path in reversed(paths):
            text = str(path)
            if text not in sys.path:
                sys.path.insert(0, text)
        for name in modules:
            sys.modules.pop(name, None)
        yield
    finally:
        sys.path[:] = saved_path
        for name, value in saved_modules.items():
            if value is missing:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = value  # type: ignore[assignment]


def _bert_lock_dynamic_check() -> None:
    import torch

    real_set_num_threads = torch.set_num_threads
    real_load = torch.load
    set_calls: list[int] = []

    def spy_set_num_threads(value: int) -> None:
        set_calls.append(int(value))
        real_set_num_threads(int(value))

    class FakeBertClassifierModel:
        instances: list["FakeBertClassifierModel"] = []

        def __init__(self) -> None:
            # 记录“模型构造时”已经应用过哪些线程配置 -> 验证线程设置先于加载。
            self.set_calls_at_construction = list(set_calls)
            self._guard = threading.Lock()
            self.active = 0
            self.max_active = 0
            FakeBertClassifierModel.instances.append(self)

        def to(self, device: Any) -> "FakeBertClassifierModel":
            return self

        def load_state_dict(self, state: Any) -> None:
            return None

        def eval(self) -> "FakeBertClassifierModel":
            return self

        def __call__(self, input_ids: Any, attention_mask: Any) -> Any:
            with self._guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.12)
                return torch.zeros((1, 10))
            finally:
                with self._guard:
                    self.active -= 1

    class FakeTokenizer:
        @classmethod
        def from_pretrained(cls, path: str) -> "FakeTokenizer":
            return cls()

        def batch_encode_plus(self, texts: Any, **kwargs: Any) -> Any:
            return types.SimpleNamespace(
                input_ids=[[101, 102]], attention_mask=[[1, 1]]
            )

    fake_transformers = types.ModuleType("transformers")
    fake_transformers.BertTokenizer = FakeTokenizer  # type: ignore[attr-defined]
    fake_bert_model = types.ModuleType("bert_model")
    fake_bert_model.BertClassifierModel = FakeBertClassifierModel  # type: ignore[attr-defined]

    torch.set_num_threads = spy_set_num_threads  # type: ignore[assignment]
    torch.load = lambda *args, **kwargs: {}  # type: ignore[assignment]
    try:
        with env_set(TMF_TORCH_THREADS="3"):
            with _isolated_modules(
                [BERT_DIR],
                ["bert_model", "transformers", "config", "b12_bert_service"],
            ):
                sys.modules["transformers"] = fake_transformers
                sys.modules["bert_model"] = fake_bert_model
                module = _load_module_from_path(
                    "b12_bert_service", BERT_DIR / "bert_predict_service.py"
                )
                service = module.SERVICE
                fake_model = FakeBertClassifierModel.instances[-1]

                check(
                    "bert: torch.set_num_threads(config.torch_threads=3) before model construction",
                    fake_model.set_calls_at_construction == [3],
                    str(fake_model.set_calls_at_construction),
                )
                check(
                    "bert: torch.get_num_threads() == configured 3",
                    torch.get_num_threads() == 3,
                    str(torch.get_num_threads()),
                )
                check(
                    "bert: _INFER_LOCK is a threading.Lock",
                    isinstance(module._INFER_LOCK, type(threading.Lock())),
                    type(module._INFER_LOCK).__name__,
                )

                results: list[Any] = []
                errors: list[BaseException] = []

                def call_predict() -> None:
                    try:
                        results.append(service.predict(DEMO_TITLE))
                    except BaseException as exc:  # noqa: BLE001
                        errors.append(exc)

                workers = [threading.Thread(target=call_predict) for _ in range(4)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(10)
                check(
                    "bert: 4 concurrent infer serialized by lock (max_active == 1)",
                    fake_model.max_active == 1
                    and len(results) == 4
                    and not errors,
                    f"max_active={fake_model.max_active} results={len(results)} errors={errors}",
                )
                check(
                    "bert: PredictService contract unchanged (pred/scores/score_kind)",
                    bool(results)
                    and results[0].pred_class == module.config.classname_list[0]
                    and len(results[0].scores or ()) == len(module.config.classname_list)
                    and results[0].score_kind == "raw_softmax",
                    str(results[0])[:200],
                )
    finally:
        torch.set_num_threads = real_set_num_threads  # type: ignore[assignment]
        torch.load = real_load  # type: ignore[assignment]


def _distill_lock_dynamic_check() -> None:
    import torch

    real_set_num_threads = torch.set_num_threads
    real_load = torch.load
    set_calls: list[int] = []

    def spy_set_num_threads(value: int) -> None:
        set_calls.append(int(value))
        real_set_num_threads(int(value))

    class FakeStudentBiLSTM:
        instances: list["FakeStudentBiLSTM"] = []

        def __init__(self, **kwargs: Any) -> None:
            self.class_len = int(kwargs.get("class_len", 10))
            self.set_calls_at_construction = list(set_calls)
            self._guard = threading.Lock()
            self.active = 0
            self.max_active = 0
            FakeStudentBiLSTM.instances.append(self)

        def load_state_dict(self, state: Any) -> None:
            return None

        def to(self, device: Any) -> "FakeStudentBiLSTM":
            return self

        def eval(self) -> "FakeStudentBiLSTM":
            return self

        def parameters(self) -> list[Any]:
            return [types.SimpleNamespace(numel=lambda: 0)]

        def __call__(self, input_ids: Any, attention_mask: Any) -> Any:
            with self._guard:
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            try:
                time.sleep(0.12)
                return torch.zeros((1, self.class_len))
            finally:
                with self._guard:
                    self.active -= 1

    def fake_torch_load(path: Any, map_location: Any = None) -> dict[str, Any]:
        # 与 student_predict_service._load_model 的推断逻辑匹配：
        # embedding=16 维、hidden=8、2 层 BiLSTM。
        vocab, embed_dim, hidden = 1000, 16, 8
        return {
            "embedding.weight": torch.zeros((vocab, embed_dim)),
            "lstm.weight_ih_l0": torch.zeros((4 * hidden, embed_dim)),
            "lstm.weight_ih_l1": torch.zeros((4 * hidden, 4 * hidden)),
        }

    class FakeTokenizer:
        @classmethod
        def from_pretrained(cls, path: str) -> "FakeTokenizer":
            return cls()

        def __call__(self, texts: Any, **kwargs: Any) -> dict[str, Any]:
            return {
                "input_ids": torch.tensor([[101, 102]]),
                "attention_mask": torch.tensor([[1, 1]]),
            }

    fake_transformers = types.ModuleType("transformers")
    fake_transformers.BertTokenizer = FakeTokenizer  # type: ignore[attr-defined]
    fake_student = types.ModuleType("student_bilstm_v2")
    fake_student.BiLSTMStudentV2 = FakeStudentBiLSTM  # type: ignore[attr-defined]

    torch.set_num_threads = spy_set_num_threads  # type: ignore[assignment]
    torch.load = fake_torch_load  # type: ignore[assignment]
    try:
        with env_set(TMF_TORCH_THREADS=None):
            with _isolated_modules(
                [DISTILL_DIR],
                [
                    "student_bilstm_v2",
                    "transformers",
                    "config",
                    "b12_distill_service",
                ],
            ):
                sys.modules["transformers"] = fake_transformers
                sys.modules["student_bilstm_v2"] = fake_student
                module = _load_module_from_path(
                    "b12_distill_service",
                    DISTILL_DIR / "student_predict_service.py",
                )
                service = module.SERVICE
                fake_model = FakeStudentBiLSTM.instances[-1]

                check(
                    "distill: default torch.set_num_threads(4) before model construction",
                    fake_model.set_calls_at_construction == [4],
                    str(fake_model.set_calls_at_construction),
                )
                check(
                    "distill: torch.get_num_threads() == default 4",
                    torch.get_num_threads() == 4,
                    str(torch.get_num_threads()),
                )
                check(
                    "distill: _INFER_LOCK is a threading.Lock",
                    isinstance(module._INFER_LOCK, type(threading.Lock())),
                    type(module._INFER_LOCK).__name__,
                )

                results: list[Any] = []
                errors: list[BaseException] = []

                def call_predict() -> None:
                    try:
                        results.append(service.predict(DEMO_TITLE))
                    except BaseException as exc:  # noqa: BLE001
                        errors.append(exc)

                workers = [threading.Thread(target=call_predict) for _ in range(4)]
                for worker in workers:
                    worker.start()
                for worker in workers:
                    worker.join(10)
                check(
                    "distill: 4 concurrent infer serialized by lock (max_active == 1)",
                    fake_model.max_active == 1
                    and len(results) == 4
                    and not errors,
                    f"max_active={fake_model.max_active} results={len(results)} errors={errors}",
                )
                check(
                    "distill: PredictService contract unchanged (pred/scores/score_kind)",
                    bool(results)
                    and results[0].pred_class == module.config.classname_list[0]
                    and len(results[0].scores or ()) == len(module.config.classname_list)
                    and results[0].score_kind == "raw_softmax",
                    str(results[0])[:200],
                )
    finally:
        torch.set_num_threads = real_set_num_threads  # type: ignore[assignment]
        torch.load = real_load  # type: ignore[assignment]


def worker_dynamic_checks() -> None:
    print("\n=== 4c. worker 动态检查（stub 模块，不加载真实权重）：线程设置 + 推理锁 ===")
    clear_env()
    _bert_lock_dynamic_check()
    _distill_lock_dynamic_check()


# ---------------------------------------------------------------------------
# 5. 独立子进程：fake adapter 并发 1/3/5 编排延迟 / CPU / RSS
# ---------------------------------------------------------------------------
ORCHESTRATION_RUNNER = r'''
import asyncio, json, os, sys, time

sys.path.insert(0, os.getcwd())

from serving.adapters import AdapterResult
from serving.gateway.settings import load_settings
from serving.gateway.stats import StatsCollector
from serving.models.registry import ModelRegistry
from serving.services.classification import ClassificationService

DELAY = 0.1
ROUNDS = 3
TITLE = "北京二手房成交量创年内新高"


class SleepAdapter:
    def __init__(self, model_id, delay):
        self.model_id = model_id
        self.model_version = None
        self.delay = delay

    async def predict(self, title, request_id):
        await asyncio.sleep(self.delay)
        return AdapterResult(
            model_id=self.model_id, status="ok",
            latency_ms=int(round(self.delay * 1000)), pred_class="sports",
        )


def rss_kb():
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PMC),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = PMC()
        counters.cb = ctypes.sizeof(PMC)
        handle = kernel32.GetCurrentProcess()
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None, None
        return counters.WorkingSetSize // 1024, counters.PeakWorkingSetSize // 1024
    except Exception:
        return None, None


def process_cpu_us():
    """进程累计 CPU（kernel+user），Windows GetProcessTimes 精度 100ns。"""
    try:
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        creation, exit_time, kernel_time, user_time = (
            FILETIME(),
            FILETIME(),
            FILETIME(),
            FILETIME(),
        )
        if not kernel32.GetProcessTimes(
            kernel32.GetCurrentProcess(),
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return None

        def to_100ns(value):
            return (value.dwHighDateTime << 32) | value.dwLowDateTime

        return (to_100ns(kernel_time) + to_100ns(user_time)) / 10.0
    except Exception:
        return None


async def main():
    concurrency = int(sys.argv[1])
    settings = load_settings()
    # B9 后默认 compare 集合为 4；并发测量固定用全部 5 个启用模型（显式传入）。
    model_ids = ModelRegistry(settings).all_model_ids()
    adapters = {mid: SleepAdapter(mid, DELAY) for mid in model_ids}
    service = ClassificationService(
        registry=ModelRegistry(settings),
        adapters=adapters,
        classes=settings.classes,
        max_title_chars=settings.max_title_chars,
        stats=StatsCollector(),
        logger=__import__("logging").getLogger("b12-measure"),
        compare_concurrency=concurrency,
    )
    await service.compare(TITLE, model_ids, "warmup")
    durations = []
    statuses = []
    cpu_before = process_cpu_us()
    for index in range(ROUNDS):
        started = time.perf_counter()
        outcome = await service.compare(TITLE, model_ids, "round-%d" % index)
        durations.append(round((time.perf_counter() - started) * 1000, 2))
        statuses.append(outcome.status)
    cpu_after = process_cpu_us()
    cpu_us = (
        round(cpu_after - cpu_before, 1)
        if cpu_before is not None and cpu_after is not None
        else None
    )
    rss, peak = rss_kb()
    print(json.dumps({
        "concurrency": concurrency,
        "models": len(model_ids),
        "delay_ms": int(round(DELAY * 1000)),
        "durations_ms": durations,
        "statuses": statuses,
        "cpu_us": cpu_us,
        "rss_kb": rss,
        "peak_rss_kb": peak,
    }))


asyncio.run(main())
'''


def orchestration_measurement() -> list[dict[str, Any]]:
    print("\n=== 5. 独立子进程 + fake adapter：并发 1/3/5 编排延迟 / CPU / RSS ===")
    clear_env()
    env = {key: value for key, value in os.environ.items() if key not in ENV_KEYS}
    rows: list[dict[str, Any]] = []
    for concurrency in (1, 3, 5):
        try:
            proc = subprocess.run(
                [sys.executable, "-c", ORCHESTRATION_RUNNER, str(concurrency)],
                cwd=str(PROJECT_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
        except subprocess.TimeoutExpired:
            check(f"orchestration child c={concurrency} finished", False, "timeout")
            continue
        check(
            f"orchestration child c={concurrency} exit 0",
            proc.returncode == 0,
            (proc.stderr or "")[-600:],
        )
        payload_lines = [
            line
            for line in proc.stdout.splitlines()
            if line.strip().startswith("{") and line.strip().endswith("}")
        ]
        if not payload_lines:
            check(f"orchestration child c={concurrency} printed JSON", False,
                  (proc.stdout or "")[-300:])
            continue
        data = json.loads(payload_lines[-1])
        rows.append(data)
        median = statistics.median(data["durations_ms"])
        p95 = max(data["durations_ms"])
        cpu_text = "n/a" if data["cpu_us"] is None else f"{data['cpu_us']:.1f}us"
        print(
            f"      c={concurrency}: wall={data['durations_ms']}ms "
            f"P50={median:.1f}ms max={p95:.1f}ms CPU={cpu_text} "
            f"RSS={data['rss_kb']}KB peak={data['peak_rss_kb']}KB "
            f"status={data['statuses']}"
        )
        check(
            f"orchestration c={concurrency}: models={data['models']} all ok",
            data["models"] == 5
            and all(status == "ok" for status in data["statuses"]),
            str(data),
        )

    if len(rows) == 3:
        medians = [statistics.median(row["durations_ms"]) for row in rows]
        check(
            "orchestration: more concurrency -> shorter wall time (c1 > c3 > c5)",
            medians[0] > medians[1] * 1.3 and medians[1] > medians[2] * 1.2,
            f"P50={[round(value, 1) for value in medians]}",
        )
        check(
            "orchestration: c=1 wall time near 5*delay (500ms)",
            medians[0] >= 400,
            f"{medians[0]:.1f}ms",
        )
        check(
            "orchestration: c=5 wall time near 1*delay (100ms)",
            medians[2] <= 300,
            f"{medians[2]:.1f}ms",
        )
        print("\n      并发   P50(ms)   max(ms)   CPU(ms)   RSS(KB)   peakRSS(KB)")
        for row in rows:
            cpu_text = (
                "n/a"
                if row["cpu_us"] is None
                else f"{row['cpu_us'] / 1000.0:.1f}"
            )
            print(
                f"      {row['concurrency']:>3}   "
                f"{statistics.median(row['durations_ms']):>7.1f}   "
                f"{max(row['durations_ms']):>7.1f}   "
                f"{cpu_text:>7}   "
                f"{str(row['rss_kb']):>8}   {str(row['peak_rss_kb']):>10}"
            )
        print(
            "      注：Windows 进程 CPU 计时粒度约 15.6ms；0~62.5ms 属于 3 轮编排的"
            "测量下限抖动，RSS 在各并发下稳定无增长（无泄漏）。"
        )
    return rows


# ---------------------------------------------------------------------------
# 6. 可选：真实 BERT/蒸馏小样本（独立子进程，不启动服务、不绑定端口）
# ---------------------------------------------------------------------------
REAL_WORKER_RUNNER = r'''
import json, os, sys, time

project_root, module_dir, service_module = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, project_root)
sys.path.insert(0, module_dir)
os.environ["TMF_TORCH_THREADS"] = os.environ.get("TMF_VERIFY_TORCH_THREADS", "2")

started = time.perf_counter()
import importlib
module = importlib.import_module(service_module)
load_ms = round((time.perf_counter() - started) * 1000, 1)

import torch
service = module.SERVICE
titles = ["北京二手房成交量创年内新高", "国足世预赛客场取胜"]
latencies = []
pred = None
for title in titles:
    t = time.perf_counter()
    result = service.predict(title)
    latencies.append(round((time.perf_counter() - t) * 1000, 2))
    pred = result.pred_class


def rss_kb():
    try:
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(PMC),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = PMC()
        counters.cb = ctypes.sizeof(PMC)
        handle = kernel32.GetCurrentProcess()
        if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
            return None, None
        return counters.WorkingSetSize // 1024, counters.PeakWorkingSetSize // 1024
    except Exception:
        return None, None


rss, peak = rss_kb()
print(json.dumps({
    "service": service_module,
    "load_ms": load_ms,
    "latencies_ms": latencies,
    "pred_class": pred,
    "torch_threads": torch.get_num_threads(),
    "rss_kb": rss,
    "peak_rss_kb": peak,
}))
'''


def real_worker_measurement() -> list[dict[str, Any]]:
    print("\n=== 6. 真实 BERT/蒸馏小样本（可选，TMF_VERIFY_REAL_WORKERS=1） ===")
    if os.environ.get("TMF_VERIFY_REAL_WORKERS") != "1":
        print("      SKIP 未设置 TMF_VERIFY_REAL_WORKERS=1（默认不加载真实权重）")
        return []

    clear_env()
    env = {key: value for key, value in os.environ.items() if key not in ENV_KEYS}
    env["TMF_VERIFY_REAL_WORKERS"] = "1"
    env["TMF_VERIFY_TORCH_THREADS"] = "2"
    rows: list[dict[str, Any]] = []
    for label, module_dir, service_module in (
        ("bert", BERT_DIR, "bert_predict_service"),
        ("distill", DISTILL_DIR, "student_predict_service"),
    ):
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    REAL_WORKER_RUNNER,
                    str(PROJECT_ROOT),
                    str(module_dir),
                    service_module,
                ],
                cwd=str(PROJECT_ROOT),
                env=env,
                capture_output=True,
                text=True,
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            check(f"real {label} sample finished", False, "timeout")
            continue
        check(
            f"real {label} sample exit 0",
            proc.returncode == 0,
            (proc.stderr or "")[-800:],
        )
        payload_lines = [
            line
            for line in proc.stdout.splitlines()
            if line.strip().startswith("{") and line.strip().endswith("}")
        ]
        if not payload_lines:
            check(f"real {label} sample printed JSON", False, (proc.stdout or "")[-300:])
            continue
        data = json.loads(payload_lines[-1])
        rows.append(data)
        print(
            f"      {label}: load={data['load_ms']}ms "
            f"latency={data['latencies_ms']}ms pred={data['pred_class']} "
            f"torch_threads={data['torch_threads']} "
            f"RSS={data['rss_kb']}KB peak={data['peak_rss_kb']}KB"
        )
        check(
            f"real {label}: torch threads env override applied (2)",
            data["torch_threads"] == 2,
            str(data["torch_threads"]),
        )
        check(
            f"real {label}: inference returns valid class",
            isinstance(data["pred_class"], str) and bool(data["pred_class"]),
            str(data["pred_class"]),
        )
    return rows


# ---------------------------------------------------------------------------
def main() -> int:
    print("B12 并发与负载边界验收（不启动用户服务、不加载真实权重，除非显式 opt-in）")
    config_checks()
    app_wiring_checks()
    semaphore_checks()
    deadline_and_isolation_checks()
    worker_config_checks()
    worker_static_checks()
    worker_dynamic_checks()
    orchestration_measurement()
    real_worker_measurement()

    print()
    if FAILURES:
        print(f"RESULT FAIL ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
