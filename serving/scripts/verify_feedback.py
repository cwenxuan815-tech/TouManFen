#!/usr/bin/env python
"""B4 反馈 API / SQLite / 数据生命周期验收（fake，TestClient；不启动真实端口与 LLM）。

运行环境（与项目 README 一致，通过 PYTHONPATH 注入网关依赖）：
    $env:PYTHONPATH='E:\\AIModels\\test\\TouManFen\\venv-gateway\\Lib\\site-packages'
    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/verify_feedback.py

覆盖点（对应 docs/工程实施工作清单_75工时.md §4.4 / §6.3 / B4）：
1. 标题规范化（NFKC -> casefold -> 仅保留 alnum 含 CJK）、3-gram、Jaccard 手算对拍；
2. 成功提交 total+1 / status=pending / 文案合法；重复提交 409 FEEDBACK_DUPLICATE
   且不重复计数（含首尾空白 trim 后去重）；
3. 20 并发写无 sqlite 写锁错误；关闭并重建 service/store 后数据仍在；
4. 恶意 title / SQL 注入样式字段安全存储，feedback 表结构与其他表数据不受影响；
5. 相似度 <0.75 不 hint、>=0.75 返回 pending hint；原 pred_class/latency/scores 不变；
   hint 不含反馈原文/ID；并列规则（相似度 -> created_at 最新 -> feedback_id 最大）；
   候选只取 status=pending、同题/跨模型均可、不按 pred_class 过滤；
6. 30 天旧反馈在启动与 /feedback/stats 时被惰性清理，vote/arena 数据不被删；
7. GET /feedback/stats 只返回聚合，无原始行/标题；日志无 title/IP/UA；
8. tools/purge_feedback.py：dry-run 只打印数量，--purge 只清 feedback；
9. feedback 与 arena 共用同一个 TMF_DB_PATH 库，不产生第二个 DB。

输出：逐项 PASS/FAIL，最后 RESULT PASS|FAIL，退出码 0/1。
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from serving.adapters.protocol import ScoreEntry  # noqa: E402
from serving.gateway.settings import load_settings  # noqa: E402
from serving.models.registry import ModelRegistry  # noqa: E402
from serving.services.arena import ArenaStore  # noqa: E402
from serving.services.classification import (  # noqa: E402
    ClassifyOutcome,
    CompareItem,
    CompareOutcome,
)
from serving.services.feedback import (  # noqa: E402
    DEFAULT_DB_PATH,
    FEEDBACK_ACCEPTED_MESSAGE,
    FEEDBACK_RETENTION_DAYS,
    SIMILARITY_THRESHOLD,
    DuplicateFeedbackError,
    FeedbackService,
    FeedbackStore,
    character_ngrams,
    jaccard_similarity,
    normalize_title,
)

try:  # 让中文输出在 cp936 控制台也不至于抛 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

FAILURES: list[str] = []
TOTAL = 0

SETTINGS = load_settings()
REGISTRY = ModelRegistry(SETTINGS)
CLASSES = SETTINGS.classes
MAX_TITLE_CHARS = SETTINGS.max_title_chars
MODEL_IDS = REGISTRY.enabled_model_ids()
LOGGER = logging.getLogger("verify-feedback")
LOGGER.setLevel(logging.INFO)
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False

# 相似/不相似标题（3-gram Jaccard 手算见 pure_checks）：
TITLE_A = "央行宣布全面降准释放长期资金"
TITLE_B = "央行宣布全面降准释放长期资本"  # 与 A 仅末字不同：Jaccard = 11/13
TITLE_FAR = "国足客场战胜对手取得三连胜"  # 与 A：Jaccard = 0
TITLE_PREFIX = "央行宣布全面降准释放长期资金支持实体经济发展"  # 与 A：0.6
MALICIOUS_TITLE = "Robert'); DROP TABLE feedback;--"
UA_PROBE = "verify-feedback-UA/9.9"
FORBIDDEN_WORDING = ("已学会", "已重新训练")


def check(name: str, condition: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


class CaptureHandler(logging.Handler):
    """捕获业务日志，用于断言日志里没有标题/用户原文/IP/UA。"""

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        payload = {
            key: getattr(record, key, None)
            for key in ("event", "request_id", "error_code", "model_id", "total")
        }
        payload["message"] = record.getMessage()
        self.entries.append(json.dumps(payload, ensure_ascii=False, default=str))


CAPTURE = CaptureHandler()
LOGGER.addHandler(CAPTURE)


# ---------------------------------------------------------------------------
# 通用 DB 辅助
# ---------------------------------------------------------------------------
def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def count_rows(db_path: Path, table: str) -> int:
    conn = connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"])
    finally:
        conn.close()


def table_names(db_path: Path) -> set[str]:
    conn = connect(db_path)
    try:
        return {
            str(row["name"])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()


def column_names(db_path: Path, table: str) -> set[str]:
    conn = connect(db_path)
    try:
        return {
            str(row["name"])
            for row in conn.execute(f"PRAGMA table_info({table})")
        }
    finally:
        conn.close()


def update_feedback_time(db_path: Path, feedback_id: int, epoch: float) -> None:
    iso = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE feedback SET created_at_epoch = ?, created_at = ?"
            " WHERE feedback_id = ?",
            (epoch, iso, feedback_id),
        )
        conn.execute("COMMIT")
    finally:
        conn.close()


def insert_feedback_row(
    db_path: Path,
    *,
    client_id: str,
    title: str,
    normalized_title: str,
    model_id: str,
    pred_class: str,
    corrected_class: str,
    score: float | None,
    source: str,
    status: str,
    epoch: float,
) -> int:
    iso = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "INSERT INTO feedback"
            " (client_id, title, normalized_title, model_id, pred_class,"
            "  corrected_class, score, source, status, created_at,"
            "  created_at_epoch)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                client_id,
                title,
                normalized_title,
                model_id,
                pred_class,
                corrected_class,
                score,
                source,
                status,
                iso,
                epoch,
            ),
        )
        conn.execute("COMMIT")
        return int(cursor.lastrowid or 0)
    finally:
        conn.close()


def seed_arena(db_path: Path, *, votes: int = 1) -> dict[str, int]:
    """用 B7 ArenaStore 在共享库里创建 arena/vote 数据，返回各表行数。"""
    store = ArenaStore(db_path)
    try:
        store.ensure_models(MODEL_IDS)
        for index in range(votes):
            item_id = f"verify-item-{index}"
            store.save_item(
                item_id=item_id,
                title=f"竞技场验收样例 {index}",
                true_label=index % len(CLASSES),
                candidates=[
                    {
                        "anon_id": f"anon-{index}",
                        "model_id": "rf",
                        "pred_class": "finance",
                        "latency_ms": 5,
                    }
                ],
            )
            store.record_vote(
                client_id=f"arena-client-{index}",
                item_id=item_id,
                anon_id=f"anon-{index}",
                model_id="rf",
                pred_class="finance",
                true_label=index % len(CLASSES),
                opponent_model_ids=[],
            )
    finally:
        store.close()
    return {
        table: count_rows(db_path, table)
        for table in ("arena_item", "arena_model", "vote", "vote_item")
    }


def arena_counts(db_path: Path) -> dict[str, int]:
    return {
        table: count_rows(db_path, table)
        for table in ("arena_item", "arena_model", "vote", "vote_item")
    }


def make_service(
    db_path: Path, *, logger: logging.Logger | None = None
) -> FeedbackService:
    return FeedbackService(
        registry=REGISTRY,
        classes=CLASSES,
        max_title_chars=MAX_TITLE_CHARS,
        logger=logger or LOGGER,
        db_path=db_path,
    )


def submit(
    service: FeedbackService,
    *,
    title: str = TITLE_A,
    client_id: str = "client-1",
    model_id: str = "fasttext",
    pred_class: str = "sports",
    corrected_class: str = "finance",
    score: float | None = 0.5,
    source: str | None = None,
    request_id: str = "req-submit",
):
    return asyncio.run(
        service.submit(
            client_id=client_id,
            title=title,
            model_id=model_id,
            pred_class=pred_class,
            corrected_class=corrected_class,
            score=score,
            source=source,
            request_id=request_id,
        )
    )


def expect_api_error(callable_, *, status: int, code: str, field: str | None = None):
    """执行 callable，返回 (ok, 说明)；ok 表示异常类型/状态码/错误码/字段均符合。"""
    from serving.gateway.errors import ApiError

    try:
        callable_()
    except ApiError as exc:
        if exc.status_code != status or exc.code != code:
            return False, f"{exc.status_code}/{exc.code}"
        if field is not None and (exc.details or {}).get("field") != field:
            return False, f"field={(exc.details or {}).get('field')}"
        return True, ""
    except Exception as exc:  # pragma: no cover - 便于定位
        return False, f"{type(exc).__name__}: {exc}"
    return False, "no exception"


def allowed_message_ok(message: str) -> bool:
    allowed = {"已记录，待审核", "反馈已提交，待审核", FEEDBACK_ACCEPTED_MESSAGE}
    return (
        message in allowed
        and not any(word in message for word in FORBIDDEN_WORDING)
    )


# ---------------------------------------------------------------------------
# 1. 纯函数：规范化 / 3-gram / Jaccard
# ---------------------------------------------------------------------------
def pure_checks() -> None:
    check(
        "pure: NFKC+casefold+alnum normalization (full-width latin)",
        normalize_title("  ＡＢＣ，Hello！ ") == "abchello",
        repr(normalize_title("  ＡＢＣ，Hello！ ")),
    )
    check(
        "pure: CJK kept, spaces/punctuation removed",
        normalize_title("央行 宣布，降准！") == "央行宣布降准",
    )
    check(
        "pure: full-width digits normalized",
        normalize_title("２０２４年GDP增长5%") == "2024年gdp增长5",
    )
    check(
        "pure: 3-gram size for 14-char title == 12",
        len(character_ngrams(normalize_title(TITLE_A))) == 12,
        str(len(character_ngrams(normalize_title(TITLE_A)))),
    )
    check(
        "pure: short text (<=3) falls back to whole string gram",
        character_ngrams("ab") == frozenset({"ab"}),
        str(character_ngrams("ab")),
    )
    a, b = normalize_title(TITLE_A), normalize_title(TITLE_B)
    check("pure: jaccard identical == 1.0", jaccard_similarity(a, a) == 1.0)
    check(
        "pure: jaccard A/B hand case == 11/13",
        abs(jaccard_similarity(a, b) - 11.0 / 13.0) < 1e-12,
        repr(jaccard_similarity(a, b)),
    )
    check(
        "pure: jaccard A/far == 0.0",
        jaccard_similarity(a, normalize_title(TITLE_FAR)) == 0.0,
        repr(jaccard_similarity(a, normalize_title(TITLE_FAR))),
    )
    check(
        "pure: jaccard A/prefix hand case == 0.6 (< threshold)",
        abs(
            jaccard_similarity(a, normalize_title(TITLE_PREFIX)) - 0.6
        )
        < 1e-12,
        repr(jaccard_similarity(a, normalize_title(TITLE_PREFIX))),
    )
    check(
        "pure: similarity threshold constant is 0.75",
        SIMILARITY_THRESHOLD == 0.75,
        repr(SIMILARITY_THRESHOLD),
    )
    check(
        "pure: empty normalized text yields 0.0",
        jaccard_similarity(normalize_title("！！！"), normalize_title(TITLE_A)) == 0.0,
    )


# ---------------------------------------------------------------------------
# 2. service 提交/去重/校验/恶意输入
# ---------------------------------------------------------------------------
def service_flow_checks(tmp: Path) -> None:
    flow_dir = tmp / "flow"
    flow_dir.mkdir(parents=True, exist_ok=True)
    db_path = flow_dir / "tmf.db"
    seed_arena(db_path, votes=1)
    arena_before = arena_counts(db_path)

    service = make_service(db_path)
    outcome = submit(service)
    check(
        "service: first submit -> feedback_id>0, status pending, total 1",
        outcome.feedback_id > 0 and outcome.status == "pending" and outcome.total == 1,
        repr(outcome),
    )
    stats = asyncio.run(service.stats(request_id="req-stats-1"))
    check(
        "service: stats total/pending/by_class/by_model/retention",
        stats.total == 1
        and stats.pending == 1
        and stats.by_class.get("finance") == 1
        and stats.by_model.get("fasttext") == 1
        and stats.retention_days == FEEDBACK_RETENTION_DAYS,
        repr(stats),
    )

    ok, detail = expect_api_error(
        lambda: submit(service),
        status=409,
        code="FEEDBACK_DUPLICATE",
    )
    check("service: duplicate client+title+model -> FEEDBACK_DUPLICATE", ok, detail)
    check(
        "service: duplicate does not increase total",
        asyncio.run(service.stats(request_id="req-stats-2")).total == 1,
    )
    ok, detail = expect_api_error(
        lambda: submit(service, title=f"  {TITLE_A}  "),
        status=409,
        code="FEEDBACK_DUPLICATE",
    )
    check("service: dedupe uses trimmed title", ok, detail)

    other_model = submit(
        service, title=TITLE_A, model_id="bert", corrected_class="realty"
    )
    check(
        "service: same client+title different model is a new row",
        other_model.total == 2,
        repr(other_model),
    )
    stats2 = asyncio.run(service.stats(request_id="req-stats-3"))
    check(
        "service: by_model/by_class aggregate across rows",
        stats2.by_model.get("bert") == 1 and stats2.by_class.get("realty") == 1,
        repr(stats2),
    )

    # -- 字段校验矩阵（错误信息不回显原文） -------------------------------
    invalid_cases = (
        ("title empty", {"title": ""}, "title"),
        ("title blank", {"title": "   "}, "title"),
        ("title too long", {"title": "长" * (MAX_TITLE_CHARS + 1)}, "title"),
        ("client_id invalid", {"client_id": "bad id!"}, "client_id"),
        ("model unknown", {"model_id": "no-such-model"}, "model_id"),
        ("pred_class invalid", {"pred_class": "no-such-class"}, "pred_class"),
        (
            "corrected_class invalid",
            {"corrected_class": "no-such-class"},
            "corrected_class",
        ),
        ("score below range", {"score": -0.1}, "score"),
        ("score above range", {"score": 1.1}, "score"),
        ("score nan", {"score": float("nan")}, "score"),
        ("score inf", {"score": float("inf")}, "score"),
        ("source invalid chars", {"source": "bad source!"}, "source"),
        ("source too long", {"source": "x" * 33}, "source"),
    )
    total_before_invalid = asyncio.run(
        service.stats(request_id="req-stats-4")
    ).total
    for label, overrides, field in invalid_cases:
        kwargs: dict[str, Any] = {
            "title": f"校验样例 {label}",
            "score": 0.5,
            "source": None,
        }
        kwargs.update(overrides)
        ok, detail = expect_api_error(
            lambda kwargs=kwargs: submit(service, **kwargs),
            status=400,
            code="FEEDBACK_INVALID",
            field=field,
        )
        check(f"service: invalid {label} -> 400 FEEDBACK_INVALID", ok, detail)
    check(
        "service: invalid submissions never counted",
        asyncio.run(service.stats(request_id="req-stats-5")).total
        == total_before_invalid,
    )

    # 合法边界：score=0/1/None、自定义 source。
    ok_edges = [
        submit(service, title="边界分数 0", client_id="edge-1", score=0.0),
        submit(service, title="边界分数 1", client_id="edge-2", score=1.0),
        submit(service, title="无分数", client_id="edge-3", score=None),
        submit(
            service,
            title="自定义来源",
            client_id="edge-4",
            score=0.25,
            source="web-ui",
        ),
    ]
    check(
        "service: score 0/1/None and custom source accepted",
        all(item.total >= 1 for item in ok_edges),
        str([item.total for item in ok_edges]),
    )
    conn = connect(db_path)
    try:
        row = conn.execute(
            "SELECT score, source, status FROM feedback WHERE title = ?",
            ("自定义来源",),
        ).fetchone()
    finally:
        conn.close()
    check(
        "service: score/source/status persisted as expected",
        row is not None
        and float(row["score"]) == 0.25
        and row["source"] == "web-ui"
        and row["status"] == "pending",
        repr(dict(row)) if row is not None else "missing row",
    )

    # -- 恶意输入：参数化 SQL，不改表结构/其他数据 ------------------------
    total_before_attack = count_rows(db_path, "feedback")
    attack = submit(
        service,
        client_id="attack-client",
        title=MALICIOUS_TITLE,
        pred_class="sports",
        corrected_class="finance",
    )
    check(
        "security: SQL-injection-style title stored safely as data",
        attack.total == total_before_attack + 1,
        repr(attack),
    )
    conn = connect(db_path)
    try:
        stored = conn.execute(
            "SELECT title FROM feedback WHERE feedback_id = ?",
            (attack.feedback_id,),
        ).fetchone()
    finally:
        conn.close()
    check(
        "security: malicious title stored verbatim (trimmed), not executed",
        stored is not None and stored["title"] == MALICIOUS_TITLE,
        repr(stored["title"]) if stored is not None else "missing",
    )
    expected_columns = {
        "feedback_id",
        "client_id",
        "title",
        "normalized_title",
        "model_id",
        "pred_class",
        "corrected_class",
        "score",
        "source",
        "status",
        "created_at",
        "created_at_epoch",
    }
    check(
        "security: feedback table still exists with expected columns",
        "feedback" in table_names(db_path)
        and expected_columns <= column_names(db_path, "feedback"),
        str(sorted(column_names(db_path, "feedback"))),
    )
    ok, detail = expect_api_error(
        lambda: submit(
            service,
            title="注入字段样例",
            pred_class="sports'); DELETE FROM feedback;--",
        ),
        status=400,
        code="FEEDBACK_INVALID",
        field="pred_class",
    )
    check(
        "security: SQL-injection-style field value rejected, no deletion",
        ok and count_rows(db_path, "feedback") == total_before_attack + 1,
        f"{detail} count={count_rows(db_path, 'feedback')}",
    )
    check(
        "security: arena/vote data untouched by malicious feedback",
        arena_counts(db_path) == arena_before,
        f"{arena_counts(db_path)} != {arena_before}",
    )

    # -- 持久化：关闭并重建 service --------------------------------------
    expected_total = count_rows(db_path, "feedback")
    service.close()
    reopened = make_service(db_path)
    stats_reopened = asyncio.run(reopened.stats(request_id="req-reopen"))
    check(
        "persistence: feedback survives service close/reopen",
        stats_reopened.total == expected_total and stats_reopened.pending == expected_total,
        f"total={stats_reopened.total} expected={expected_total}",
    )
    hint = asyncio.run(reopened.similar_hint(TITLE_A))
    check(
        "persistence: similar hint works after reopen",
        hint is not None
        and hint.corrected_class in {"finance", "realty"}
        and hint.status == "pending",
        repr(hint),
    )
    reopened.close()

    # -- 日志：不含标题/原文/IP/UA ---------------------------------------
    log_text = "\n".join(CAPTURE.entries)
    check(
        "logs(service): malicious title / client values not written to logs",
        MALICIOUS_TITLE not in log_text,
        f"entries={len(CAPTURE.entries)}",
    )
    check(
        "logs(service): no UA/IP style fields in feedback logs",
        UA_PROBE not in log_text
        and "User-Agent" not in log_text
        and "127.0.0.1" not in log_text,
        f"entries={len(CAPTURE.entries)}",
    )


# ---------------------------------------------------------------------------
# 3. 并发（WAL + busy_timeout，无写锁错误）
# ---------------------------------------------------------------------------
def concurrency_checks(tmp: Path) -> None:
    conc_dir = tmp / "concurrency"
    conc_dir.mkdir(parents=True, exist_ok=True)
    db_path = conc_dir / "tmf.db"

    store = FeedbackStore(db_path)
    now = time.time()

    def write_one(index: int) -> tuple[bool, str]:
        try:
            store.add(
                client_id=f"conc-client-{index}",
                title=f"并发反馈标题 {index}",
                normalized_title=f"并发反馈标题{index}",
                model_id="fasttext",
                pred_class="sports",
                corrected_class="finance",
                score=0.5,
                source="api",
                now_epoch=now,
            )
            return True, ""
        except Exception as exc:  # noqa: BLE001 - 记录任何 sqlite 写锁错误
            return False, f"{type(exc).__name__}: {exc}"

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(write_one, range(20)))
    failed = [(index, detail) for index, (ok, detail) in enumerate(results) if not ok]
    check(
        "concurrency: 20 parallel store writes without sqlite lock errors",
        not failed,
        str(failed[:3]),
    )
    check(
        "concurrency: 20 distinct rows persisted",
        count_rows(db_path, "feedback") == 20,
        str(count_rows(db_path, "feedback")),
    )
    store.close()

    # service 层 20 并发提交（asyncio.to_thread 路径）
    service = make_service(db_path)

    async def run_service_concurrent() -> list[Any]:
        return await asyncio.gather(
            *(
                service.submit(
                    client_id=f"async-client-{index}",
                    title=f"异步并发标题 {index}",
                    model_id="bert",
                    pred_class="sports",
                    corrected_class="realty",
                    score=0.1,
                    source=None,
                    request_id=f"req-conc-{index}",
                )
                for index in range(20)
            ),
            return_exceptions=True,
        )

    outcomes = asyncio.run(run_service_concurrent())
    exceptions = [item for item in outcomes if isinstance(item, BaseException)]
    check(
        "concurrency: 20 concurrent service submissions without lock errors",
        not exceptions,
        str([f"{type(item).__name__}: {item}" for item in exceptions[:3]]),
    )
    check(
        "concurrency: concurrent submissions counted exactly",
        count_rows(db_path, "feedback") == 40,
        str(count_rows(db_path, "feedback")),
    )
    conn = connect(db_path)
    try:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()
    check("concurrency: feedback db uses WAL", mode == "wal", mode)
    service.close()

    # 并发去重竞态：同一 client+title+model 并发写入，只允许成功一次。
    dup_db = conc_dir / "dup.db"
    dup_store = FeedbackStore(dup_db)

    def same_write(_: int) -> str:
        try:
            dup_store.add(
                client_id="same-client",
                title="并发同题反馈",
                normalized_title="并发同题反馈",
                model_id="fasttext",
                pred_class="sports",
                corrected_class="finance",
                score=0.5,
                source="api",
                now_epoch=now,
            )
            return "ok"
        except DuplicateFeedbackError:
            return "duplicate"
        except Exception as exc:  # noqa: BLE001
            return f"error:{type(exc).__name__}"

    with ThreadPoolExecutor(max_workers=10) as pool:
        outcomes = list(pool.map(same_write, range(10)))
    check(
        "concurrency: concurrent duplicate race -> exactly one success, rest 409 path",
        outcomes.count("ok") == 1
        and outcomes.count("duplicate") == 9
        and not any(item.startswith("error:") for item in outcomes)
        and count_rows(dup_db, "feedback") == 1,
        str(outcomes),
    )
    dup_store.close()


# ---------------------------------------------------------------------------
# 4. 相似 hint 规则
# ---------------------------------------------------------------------------
def hint_checks(tmp: Path) -> None:
    hint_dir = tmp / "hint"
    hint_dir.mkdir(parents=True, exist_ok=True)
    db_path = hint_dir / "tmf.db"
    service = make_service(db_path)

    first = submit(
        service,
        title=TITLE_A,
        client_id="hint-client-1",
        model_id="fasttext",
        pred_class="sports",
        corrected_class="finance",
    )
    check("hint: seed pending feedback stored", first.total == 1, repr(first))

    hint_a = asyncio.run(service.similar_hint(TITLE_A))
    check(
        "hint: exact normalized title >=0.75 -> pending hint",
        hint_a is not None
        and hint_a.corrected_class == "finance"
        and hint_a.status == "pending",
        repr(hint_a),
    )
    check(
        "hint: hint only exposes corrected_class/status (no title/id)",
        hint_a is not None
        and {field.name for field in dataclasses.fields(hint_a)}
        == {"corrected_class", "status"},
        repr(hint_a),
    )
    hint_b = asyncio.run(service.similar_hint(TITLE_B))
    check(
        "hint: similarity 11/13 (>=0.75) -> hint",
        hint_b is not None and hint_b.corrected_class == "finance",
        repr(hint_b),
    )
    check(
        "hint: similarity 0 (<0.75) -> no hint",
        asyncio.run(service.similar_hint(TITLE_FAR)) is None,
    )
    check(
        "hint: similarity 0.6 (<0.75) -> no hint",
        asyncio.run(service.similar_hint(TITLE_PREFIX)) is None,
    )
    check(
        "hint: empty/punctuation-only title -> no hint",
        asyncio.run(service.similar_hint("")) is None
        and asyncio.run(service.similar_hint("！！！")) is None,
    )

    # 候选不按 pred_class 过滤、同题/跨模型均可。
    submit(
        service,
        title=TITLE_B,
        client_id="hint-client-2",
        model_id="llm",
        pred_class="education",
        corrected_class="realty",
    )
    hint_cross = asyncio.run(service.similar_hint(TITLE_B))
    check(
        "hint: cross-model pending feedback participates (not filtered by pred_class)",
        hint_cross is not None and hint_cross.corrected_class == "realty",
        repr(hint_cross),
    )

    # 只取 status=pending：伪造 approved 行不得参与。
    approved_id = insert_feedback_row(
        db_path,
        client_id="hint-approved",
        title=TITLE_A,
        normalized_title=normalize_title(TITLE_A),
        model_id="fasttext",
        pred_class="sports",
        corrected_class="education",
        score=None,
        source="api",
        status="approved",
        epoch=time.time() + 100.0,
    )
    hint_after_approved = asyncio.run(service.similar_hint(TITLE_A))
    check(
        "hint: only status=pending candidates are considered",
        approved_id > 0
        and hint_after_approved is not None
        and hint_after_approved.corrected_class == "finance",
        repr(hint_after_approved),
    )

    serialized_hint = json.dumps(
        dataclasses.asdict(hint_after_approved), ensure_ascii=False
    )
    check(
        "hint: serialized hint has no title/feedback_id/raw user data",
        TITLE_A not in serialized_hint
        and "feedback_id" not in serialized_hint
        and "client_id" not in serialized_hint,
        serialized_hint,
    )

    # 并列规则：相似度降序 -> created_at 最新 -> feedback_id 最大。
    tie_db = hint_dir / "tie.db"
    tie_service = make_service(tie_db)
    tie_first = submit(
        tie_service,
        title=TITLE_A,
        client_id="tie-client-1",
        corrected_class="finance",
    )
    tie_second = submit(
        tie_service,
        title=TITLE_A,
        client_id="tie-client-2",
        corrected_class="realty",
    )
    update_feedback_time(tie_db, tie_first.feedback_id, 1000.0)
    update_feedback_time(tie_db, tie_second.feedback_id, 1000.0)
    tie_hint = asyncio.run(tie_service.similar_hint(TITLE_A))
    check(
        "hint: tie on similarity+created_at -> larger feedback_id wins",
        tie_hint is not None and tie_hint.corrected_class == "realty",
        repr(tie_hint),
    )
    update_feedback_time(tie_db, tie_first.feedback_id, 2000.0)
    update_feedback_time(tie_db, tie_second.feedback_id, 1000.0)
    newer_hint = asyncio.run(tie_service.similar_hint(TITLE_A))
    check(
        "hint: newer created_at wins over larger feedback_id",
        newer_hint is not None and newer_hint.corrected_class == "finance",
        repr(newer_hint),
    )
    tie_service.close()
    service.close()


# ---------------------------------------------------------------------------
# 5. 保留期清理（只删 feedback，不删 vote/arena）
# ---------------------------------------------------------------------------
def retention_checks(tmp: Path) -> None:
    ret_dir = tmp / "retention"
    ret_dir.mkdir(parents=True, exist_ok=True)
    db_path = ret_dir / "tmf.db"
    arena_before = seed_arena(db_path, votes=2)

    service = make_service(db_path)
    now = time.time()
    recent = submit(service, title="近期反馈", client_id="ret-1")
    old_1 = submit(service, title="过期反馈一", client_id="ret-2")
    old_2 = submit(service, title="过期反馈二", client_id="ret-3")
    boundary = submit(service, title="边界反馈", client_id="ret-4")
    update_feedback_time(db_path, old_1.feedback_id, now - 31 * 86400)
    update_feedback_time(db_path, old_2.feedback_id, now - 30 * 86400 - 60)
    update_feedback_time(db_path, boundary.feedback_id, now - 30 * 86400 + 60)

    removed = asyncio.run(service.cleanup_expired(now_epoch=now))
    check(
        "retention: startup-style cleanup removes only >30d feedback",
        removed == 2,
        f"removed={removed}",
    )
    remaining_titles = {
        str(row["title"])
        for row in _rows(db_path, "SELECT title FROM feedback")
    }
    check(
        "retention: recent + boundary feedback retained",
        remaining_titles == {"近期反馈", "边界反馈"},
        str(sorted(remaining_titles)),
    )
    check(
        "retention: vote/arena data not deleted by feedback cleanup",
        arena_counts(db_path) == arena_before,
        f"{arena_counts(db_path)} != {arena_before}",
    )
    check(
        "retention: arena tables still exist after cleanup",
        {"arena_item", "arena_model", "vote", "vote_item"} <= table_names(db_path),
        str(sorted(table_names(db_path))),
    )

    lazy = submit(service, title="统计时惰性清理", client_id="ret-lazy")
    update_feedback_time(db_path, lazy.feedback_id, now - 45 * 86400)
    stats = asyncio.run(service.stats(request_id="req-stats-clean", now_epoch=now))
    check(
        "retention: /feedback/stats lazily removes expired feedback",
        stats.total == 2 and count_rows(db_path, "feedback") == 2,
        f"total={stats.total} rows={count_rows(db_path, 'feedback')}",
    )
    check(
        "retention: lazy cleanup leaves vote/arena data untouched",
        arena_counts(db_path) == arena_before,
        f"{arena_counts(db_path)} != {arena_before}",
    )
    service.close()


def _rows(db_path: Path, sql: str) -> list[sqlite3.Row]:
    conn = connect(db_path)
    try:
        return list(conn.execute(sql).fetchall())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 6. HTTP 契约（TestClient，不监听真实端口）
# ---------------------------------------------------------------------------
class StubClassifier:
    """固定返回分类结果，避免任何真实 worker/LLM 调用。"""

    def __init__(
        self,
        *,
        pred_class: str = "sports",
        latency_ms: int = 42,
    ) -> None:
        self._pred_class = pred_class
        self._latency_ms = latency_ms

    async def classify(
        self, title: str, model_id: str | None, request_id: str
    ) -> ClassifyOutcome:
        return ClassifyOutcome(
            model_id=model_id or SETTINGS.default_model,
            model_version=None,
            pred_class=self._pred_class,
            latency_ms=self._latency_ms,
            scores=(ScoreEntry(class_id="sports", score=0.9),),
            score_kind="raw_softmax",
            top_k=(ScoreEntry(class_id="sports", score=0.9),),
        )

    async def compare(
        self, title: str, models: list[str] | None, request_id: str
    ) -> CompareOutcome:
        return CompareOutcome(
            items=[
                CompareItem(
                    model_id="rf",
                    status="ok",
                    latency_ms=5,
                    model_version=None,
                    pred_class="sports",
                )
            ],
            total=1,
            ok=1,
            failed=0,
            status="ok",
        )


class BrokenHintService:
    async def similar_hint(self, title: str):
        raise RuntimeError("simulated hint failure")


def _error_payload(body: dict) -> tuple[str | None, dict | None]:
    error = body.get("error")
    if not isinstance(error, dict):
        return None, None
    details = error.get("details")
    return error.get("code"), details if isinstance(details, dict) else None


def http_checks(tmp: Path) -> None:
    http_dir = tmp / "http"
    http_dir.mkdir(parents=True, exist_ok=True)
    db_path = http_dir / "tmf.db"

    # 预置 arena 数据 + 一条 31 天前反馈，用于验证「启动时惰性清理」。
    arena_before = seed_arena(db_path, votes=1)
    preseed = make_service(db_path)
    old = submit(preseed, title="开机清理旧反馈", client_id="preseed-1")
    update_feedback_time(db_path, old.feedback_id, time.time() - 31 * 86400)
    preseed.close()

    from serving.gateway.main import create_app

    os.environ["TMF_DB_PATH"] = str(db_path)
    gateway_logger = logging.getLogger("tmf.gateway")
    capture = CaptureHandler()
    gateway_logger.addHandler(capture)
    try:
        app = create_app()
        with TestClient(app) as client:
            _http_contract_checks(
                client, app, db_path, http_dir, arena_before, capture
            )
    finally:
        gateway_logger.removeHandler(capture)
        os.environ.pop("TMF_DB_PATH", None)


def _http_contract_checks(
    client: TestClient,
    app: object,
    db_path: Path,
    http_dir: Path,
    arena_before: dict[str, int],
    capture: CaptureHandler,
) -> None:
    tables = table_names(db_path)
    check(
        "http: feedback + arena share one sqlite file (no second db)",
        "feedback" in tables
        and {"arena_item", "arena_model", "vote", "vote_item"} <= tables
        and [path.name for path in http_dir.glob("*.db")] == ["tmf.db"],
        str(sorted(tables)),
    )
    check(
        "http: startup lazily cleans expired feedback",
        count_rows(db_path, "feedback") == 0,
        str(count_rows(db_path, "feedback")),
    )
    check(
        "http: startup cleanup leaves vote/arena data untouched",
        arena_counts(db_path) == arena_before,
        f"{arena_counts(db_path)} != {arena_before}",
    )
    check(
        "http: app.state.feedback assembled by composition root",
        getattr(app.state, "feedback", None) is not None,
    )

    title_http = TITLE_A
    payload = {
        "client_id": "http-client-1",
        "title": title_http,
        "model_id": "fasttext",
        "pred_class": "sports",
        "corrected_class": "finance",
        "score": 0.42,
    }
    response = client.post("/api/v1/feedback", json=payload)
    body = response.json()
    check(
        "http: POST /feedback 200 contract fields",
        response.status_code == 200
        and set(body) == {"request_id", "feedback_id", "status", "total", "message"}
        and isinstance(body.get("feedback_id"), int)
        and body["feedback_id"] > 0,
        str(body),
    )
    check(
        "http: success status=pending, total+1, allowed message",
        body.get("status") == "pending"
        and body.get("total") == 1
        and allowed_message_ok(str(body.get("message") or "")),
        str(body),
    )
    check(
        "http: success response echoes request_id and has no forbidden wording",
        response.headers.get("X-Request-ID") == body.get("request_id")
        and not any(
            word in json.dumps(body, ensure_ascii=False)
            for word in FORBIDDEN_WORDING
        ),
        str(body),
    )
    check(
        "http: success response does not echo title",
        title_http not in response.text,
        response.text[:200],
    )

    duplicate = client.post("/api/v1/feedback", json=payload)
    dup_body = duplicate.json()
    dup_code, dup_details = _error_payload(dup_body)
    check(
        "http: duplicate -> 409 FEEDBACK_DUPLICATE with unified body",
        duplicate.status_code == 409
        and dup_code == "FEEDBACK_DUPLICATE"
        and isinstance(dup_body.get("request_id"), str)
        and (dup_details or {}).get("total") == 1,
        f"{duplicate.status_code}/{dup_code}/{dup_details}",
    )
    check(
        "http: duplicate response does not leak title",
        title_http not in duplicate.text,
        duplicate.text[:200],
    )
    stats_after_dup = client.get("/api/v1/feedback/stats").json()
    check(
        "http: duplicate does not increase total",
        stats_after_dup.get("total") == 1,
        str(stats_after_dup),
    )

    # -- 校验 -------------------------------------------------------------
    invalid_http = (
        ("title empty", {"title": ""}, "title", 400),
        ("title too long", {"title": "长" * (MAX_TITLE_CHARS + 1)}, "title", 400),
        ("client invalid", {"client_id": "bad id!"}, "client_id", 400),
        ("model unknown", {"model_id": "nope"}, "model_id", 400),
        ("pred_class invalid", {"pred_class": "nope"}, "pred_class", 400),
        ("corrected invalid", {"corrected_class": "nope"}, "corrected_class", 400),
        ("score out of range", {"score": 1.5}, "score", 400),
        ("source invalid", {"source": "bad source!"}, "source", 400),
    )
    for label, overrides, field, status_code in invalid_http:
        case = dict(payload)
        case["client_id"] = "http-validation"
        case.update(overrides)
        bad = client.post("/api/v1/feedback", json=case)
        code, details = _error_payload(bad.json())
        check(
            f"http: invalid {label} -> {status_code} FEEDBACK_INVALID",
            bad.status_code == status_code
            and code == "FEEDBACK_INVALID"
            and (details or {}).get("field") == field,
            f"{bad.status_code}/{code}/{details}",
        )
    long_title = "长" * (MAX_TITLE_CHARS + 1)
    too_long = client.post(
        "/api/v1/feedback",
        json={
            "client_id": "http-validation",
            "title": long_title,
            "model_id": "fasttext",
            "pred_class": "sports",
            "corrected_class": "finance",
        },
    )
    check(
        "http: title-too-long error does not echo raw title",
        too_long.status_code == 400 and long_title not in too_long.text,
        too_long.text[:200],
    )
    missing_title = client.post(
        "/api/v1/feedback",
        json={"client_id": "http-validation", "model_id": "fasttext"},
    )
    missing_code, _ = _error_payload(missing_title.json())
    check(
        "http: missing title -> 422 INVALID_REQUEST",
        missing_title.status_code == 422 and missing_code == "INVALID_REQUEST",
        f"{missing_title.status_code}/{missing_code}",
    )
    stats_after_invalid = client.get("/api/v1/feedback/stats").json()
    check(
        "http: invalid submissions never counted",
        stats_after_invalid.get("total") == 1,
        str(stats_after_invalid),
    )

    # -- stats 只返回聚合 -------------------------------------------------
    stats_response = client.get("/api/v1/feedback/stats")
    stats = stats_response.json()
    check(
        "http: GET /feedback/stats contract fields exactly",
        stats_response.status_code == 200
        and set(stats)
        == {
            "request_id",
            "total",
            "pending",
            "by_class",
            "by_model",
            "similar_hits",
            "retention_days",
        },
        str(sorted(stats)),
    )
    check(
        "http: stats aggregates total/pending/by_class/by_model/retention",
        stats.get("total") == 1
        and stats.get("pending") == 1
        and stats.get("by_class", {}).get("finance") == 1
        and stats.get("by_model", {}).get("fasttext") == 1
        and stats.get("retention_days") == FEEDBACK_RETENTION_DAYS,
        str(stats),
    )
    stats_text = json.dumps(stats, ensure_ascii=False)
    check(
        "http: stats exposes no raw rows/title/client/user fields",
        title_http not in stats_text
        and '"title"' not in stats_text
        and '"client_id"' not in stats_text
        and '"feedback_id"' not in stats_text
        and '"rows"' not in stats_text
        and '"items"' not in stats_text,
        stats_text[:300],
    )

    # -- /classify hint：不改 pred_class / latency / scores ---------------
    app.state.classifier = StubClassifier(pred_class="sports", latency_ms=42)
    classify = client.post(
        "/api/v1/classify", json={"title": title_http, "model": "rf"}
    )
    classify_body = classify.json()
    hint = classify_body.get("human_feedback_hint")
    check(
        "http: classify with similar pending feedback -> path 200",
        classify.status_code == 200,
        str(classify.status_code),
    )
    check(
        "http: classify hint = {corrected_class,status:pending}",
        isinstance(hint, dict)
        and set(hint) == {"corrected_class", "status"}
        and hint.get("corrected_class") == "finance"
        and hint.get("status") == "pending",
        str(hint),
    )
    check(
        "http: hint does not change pred_class/latency/scores/top_k",
        classify_body.get("pred_class") == "sports"
        and classify_body.get("latency_ms") == 42
        and classify_body.get("scores") == {"sports": 0.9}
        and classify_body.get("top_k") == [{"class_id": "sports", "score": 0.9}],
        str(classify_body)[:400],
    )
    classify_text = json.dumps(classify_body, ensure_ascii=False)
    check(
        "http: hint response contains no feedback title/id/raw data",
        title_http not in classify_text
        and '"feedback_id"' not in classify_text
        and '"client_id"' not in classify_text,
        classify_text[:400],
    )

    far = client.post(
        "/api/v1/classify", json={"title": TITLE_FAR, "model": "rf"}
    )
    far_body = far.json()
    check(
        "http: classify with dissimilar title -> human_feedback_hint null",
        far.status_code == 200 and far_body.get("human_feedback_hint") is None,
        str(far_body.get("human_feedback_hint")),
    )
    check(
        "http: dissimilar title keeps pred_class/latency unchanged",
        far_body.get("pred_class") == "sports"
        and far_body.get("latency_ms") == 42,
        str(far_body)[:300],
    )

    # 未装配 / hint 查询异常时安全跳过，不影响分类。
    app.state.feedback = None
    missing_service_classify = client.post(
        "/api/v1/classify", json={"title": title_http, "model": "rf"}
    )
    check(
        "http: classify safely skips hint when feedback service missing",
        missing_service_classify.status_code == 200
        and missing_service_classify.json().get("human_feedback_hint") is None,
        missing_service_classify.text[:200],
    )
    feedback_missing = client.post("/api/v1/feedback", json=payload)
    missing_code, _ = _error_payload(feedback_missing.json())
    check(
        "http: /feedback returns 503 FEEDBACK_UNAVAILABLE when not assembled",
        feedback_missing.status_code == 503
        and missing_code == "FEEDBACK_UNAVAILABLE",
        f"{feedback_missing.status_code}/{missing_code}",
    )
    app.state.feedback = BrokenHintService()
    broken_hint = client.post(
        "/api/v1/classify", json={"title": title_http, "model": "rf"}
    )
    check(
        "http: classify safely skips hint on hint query failure",
        broken_hint.status_code == 200
        and broken_hint.json().get("human_feedback_hint") is None,
        broken_hint.text[:200],
    )
    # 恢复真实 service：直接重建一个指向同一库的实例。
    restored = make_service(db_path)
    app.state.feedback = restored

    # /compare 保持原契约（不加 hint 字段）。
    compare = client.post(
        "/api/v1/compare", json={"title": title_http, "models": ["rf"]}
    )
    check(
        "http: /compare unchanged (no human_feedback_hint field)",
        compare.status_code == 200
        and "human_feedback_hint" not in compare.json(),
        compare.text[:300],
    )

    # -- stats 时惰性清理 -------------------------------------------------
    insert_feedback_row(
        db_path,
        client_id="http-old",
        title="统计时过期反馈",
        normalized_title=normalize_title("统计时过期反馈"),
        model_id="fasttext",
        pred_class="sports",
        corrected_class="realty",
        score=None,
        source="api",
        status="pending",
        epoch=time.time() - 31 * 86400,
    )
    stats_clean = client.get("/api/v1/feedback/stats").json()
    check(
        "http: stats lazily cleans >30d feedback and keeps count stable",
        stats_clean.get("total") == 1
        and count_rows(db_path, "feedback") == 1,
        f"total={stats_clean.get('total')} rows={count_rows(db_path, 'feedback')}",
    )
    check(
        "http: stats cleanup does not delete vote/arena data",
        arena_counts(db_path) == arena_before,
        f"{arena_counts(db_path)} != {arena_before}",
    )

    # -- 日志：无 title / IP / UA ----------------------------------------
    log_text = "\n".join(capture.entries)
    check(
        "http logs: request/feedback logs contain no news title",
        bool(capture.entries) and title_http not in log_text,
        f"entries={len(capture.entries)}",
    )
    check(
        "http logs: no UA / client IP / raw title in gateway logs",
        UA_PROBE not in log_text
        and "User-Agent" not in log_text
        and "127.0.0.1" not in log_text,
        f"entries={len(capture.entries)}",
    )


# ---------------------------------------------------------------------------
# 7. TMF_DB_PATH 单一库 override
# ---------------------------------------------------------------------------
def env_override_checks(tmp: Path) -> None:
    env_dir = tmp / "env"
    env_dir.mkdir(parents=True, exist_ok=True)
    db_path = env_dir / "custom.db"
    os.environ["TMF_DB_PATH"] = str(db_path)
    try:
        arena_store = ArenaStore(db_path)
        arena_store.ensure_models(MODEL_IDS)
        arena_store.close()
        service = FeedbackService(
            registry=REGISTRY,
            classes=CLASSES,
            max_title_chars=MAX_TITLE_CHARS,
            logger=LOGGER,
        )
    finally:
        os.environ.pop("TMF_DB_PATH", None)
    tables = table_names(db_path)
    check(
        "env: TMF_DB_PATH override places arena + feedback in one db",
        db_path.is_file()
        and "feedback" in tables
        and {"arena_item", "arena_model", "vote", "vote_item"} <= tables,
        str(sorted(tables)),
    )
    check(
        "env: no second sqlite file created",
        [path.name for path in env_dir.glob("*.db")] == ["custom.db"],
        str([path.name for path in env_dir.glob("*.db")]),
    )
    check(
        "env: default DEFAULT_DB_PATH is serving/run/tmf.db",
        DEFAULT_DB_PATH == PROJECT_ROOT / "serving" / "run" / "tmf.db",
        str(DEFAULT_DB_PATH),
    )
    service.close()


# ---------------------------------------------------------------------------
# 8. tools/purge_feedback.py
# ---------------------------------------------------------------------------
def purge_checks(tmp: Path) -> None:
    purge_dir = tmp / "purge"
    purge_dir.mkdir(parents=True, exist_ok=True)
    db_path = purge_dir / "tmf.db"
    arena_before = seed_arena(db_path, votes=1)
    service = make_service(db_path)
    submit(service, title="待清除反馈一", client_id="purge-1")
    submit(service, title="待清除反馈二", client_id="purge-2")
    service.close()
    tool = PROJECT_ROOT / "tools" / "purge_feedback.py"
    env = os.environ.copy()
    env["TMF_DB_PATH"] = str(db_path)

    dry = subprocess.run(
        [sys.executable, str(tool)],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    check(
        "purge: no --purge -> dry-run prints counts, nothing deleted",
        dry.returncode == 0
        and "dry-run" in dry.stdout
        and "2" in dry.stdout
        and count_rows(db_path, "feedback") == 2,
        f"rc={dry.returncode} out={dry.stdout.strip()[:200]} err={dry.stderr[:100]}",
    )

    purge = subprocess.run(
        [sys.executable, str(tool), "--purge"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    check(
        "purge: --purge deletes feedback only",
        purge.returncode == 0
        and count_rows(db_path, "feedback") == 0
        and arena_counts(db_path) == arena_before,
        f"rc={purge.returncode} feedback={count_rows(db_path, 'feedback')} "
        f"arena={arena_counts(db_path)} err={purge.stderr[:100]}",
    )

    no_env = os.environ.copy()
    no_env.pop("TMF_DB_PATH", None)
    refused = subprocess.run(
        [sys.executable, str(tool), "--purge"],
        cwd=str(PROJECT_ROOT),
        env=no_env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    check(
        "purge: refuses to run without TMF_DB_PATH (no default db operation)",
        refused.returncode == 2
        and "TMF_DB_PATH" in (refused.stderr + refused.stdout),
        f"rc={refused.returncode} out={refused.stdout[:100]} err={refused.stderr[:200]}",
    )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    for env_key in ("TMF_DB_PATH", "TMF_ARENA_TEST_FILE"):
        os.environ.pop(env_key, None)

    with tempfile.TemporaryDirectory(prefix="tmf-feedback-verify-") as raw_tmp:
        tmp = Path(raw_tmp)
        sections = (
            ("pure", lambda _: pure_checks()),
            ("service", service_flow_checks),
            ("concurrency", concurrency_checks),
            ("hint", hint_checks),
            ("retention", retention_checks),
            ("http", http_checks),
            ("env", env_override_checks),
            ("purge", purge_checks),
        )
        for label, section in sections:
            try:
                section(tmp)
            except Exception as exc:  # 保证最后仍输出 RESULT 行
                check(
                    f"{label} section completed without exception",
                    False,
                    f"{type(exc).__name__}: {exc}",
                )
                traceback.print_exc()
    os.environ.pop("TMF_DB_PATH", None)

    if FAILURES:
        print(f"RESULT FAIL ({len(FAILURES)} failed / {TOTAL} checks)")
        for name in FAILURES:
            print(f"  FAILED: {name}")
        return 1
    print(f"RESULT PASS ({TOTAL} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
