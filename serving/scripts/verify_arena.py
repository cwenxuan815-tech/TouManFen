#!/usr/bin/env python
"""B7 竞技场投票 API 与 Elo 验收（fake adapter；不加载模型、不启动真实端口、不访问外网）。

运行环境（与项目 README 一致，通过 PYTHONPATH 注入网关依赖）：
    $env:PYTHONPATH='E:\\AIModels\\test\\TouManFen\\venv-gateway\\Lib\\site-packages'
    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/verify_arena.py

覆盖点：
1. Elo 纯函数 2 组手算对拍（1200 vs 1200、1200 vs 1400）与 K 默认值；
2. 10 次投票 -> vote_item 恰好 40 行（每票 4 场）；Elo 可从 vote_item 从头复算；
3. /arena/next 序列化文本、响应头中都 grep 不到 model_id/display_name/
   model_version/真实标签；投票后才揭晓被选中的模型（且不泄露其他候选身份）；
4. 同 client+item 重复投票 409；同一 client 21 票/分钟触发 429；
5. leaderboard 票数 <50 -> warming_up、rank 全 null、Wilson 上下界合法且
   lower<=upper；>=50（本脚本连投 50）才 ready 并按 Elo 给 rank；
6. PRAGMA journal_mode=wal；关闭/新建 service 后 vote/vote_item/Elo 仍在；
7. fake adapter 部分失败返回可投子集（partial），全部失败/不足 2 个 -> 503；
8. 无效 item/anon、空测试集、SQLite 异常返回统一错误体，不泄露堆栈；
9. 初始化使用 CREATE TABLE IF NOT EXISTS，不 DROP 其他表（预置 feedback 表验证）。

输出：逐项 PASS/FAIL，最后 RESULT PASS|FAIL，退出码 0/1。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from serving.adapters.protocol import AdapterResult, ModelAdapter  # noqa: E402
from serving.gateway.settings import load_settings  # noqa: E402
from serving.models.registry import ModelRegistry  # noqa: E402
from serving.services.arena import (  # noqa: E402
    ELO_INITIAL,
    ELO_K,
    WARMUP_MIN_VOTES,
    ArenaService,
    ArenaStoreError,
    DuplicateVoteError,
    apply_elo,
    expected_score,
    wilson_interval,
)

try:  # 让中文输出在 cp936 控制台也不至于抛 UnicodeEncodeError
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

FAILURES: list[str] = []
TOTAL = 0

# 5 个参赛模型：测试用 fake adapter 让每个模型返回不同（合法）类别，便于检查泄露。
MODEL_IDS = ("rf", "fasttext", "bert", "llm", "distill")
MODEL_CLASSES = {
    "rf": "finance",
    "fasttext": "realty",
    "bert": "stocks",
    "llm": "education",
    "distill": "science",
}
DISPLAY_NAMES = {
    "rf": "随机森林",
    "fasttext": "fastText",
    "bert": "BERT",
    "llm": "大模型",
    "distill": "蒸馏 BiLSTM",
}
IDENTITY_PATTERNS = [f'"{model_id}"' for model_id in MODEL_IDS] + [
    f'"{name}"' for name in DISPLAY_NAMES.values()
]

SETTINGS = load_settings()
REGISTRY = ModelRegistry(SETTINGS)
CLASSES = SETTINGS.classes
MODEL_ORDER = REGISTRY.all_model_ids()
LOGGER = logging.getLogger("verify-arena")
LOGGER.setLevel(logging.INFO)
LOGGER.addHandler(logging.NullHandler())
LOGGER.propagate = False


def check(name: str, condition: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


# ---------------------------------------------------------------------------
# fake adapter（只实现 ModelAdapter 协议；不加载模型、不发网络请求）
# ---------------------------------------------------------------------------
class FakeAdapter:
    def __init__(
        self,
        model_id: str,
        *,
        pred_class: str,
        latency_ms: int = 7,
        raises: bool = False,
        error_result: bool = False,
    ) -> None:
        self.model_id = model_id
        self.model_version: str | None = None
        self._pred_class = pred_class
        self._latency_ms = latency_ms
        self._raises = raises
        self._error_result = error_result

    async def predict(self, title: str, request_id: str) -> AdapterResult:
        if self._raises:
            raise RuntimeError("fake adapter failure")
        if self._error_result:
            return AdapterResult(
                model_id=self.model_id,
                status="error",
                latency_ms=self._latency_ms,
                error_code="WORKER_TIMEOUT",
                error_message="fake timeout",
            )
        return AdapterResult(
            model_id=self.model_id,
            status="ok",
            latency_ms=self._latency_ms,
            pred_class=self._pred_class,
        )


def make_fake_adapters(
    *, raising: tuple[str, ...] = (), erroring: tuple[str, ...] = ()
) -> dict[str, ModelAdapter]:
    adapters: dict[str, ModelAdapter] = {}
    for model_id in MODEL_IDS:
        adapters[model_id] = FakeAdapter(
            model_id,
            pred_class=MODEL_CLASSES[model_id],
            raises=model_id in raising,
            error_result=model_id in erroring,
        )
    return adapters


def write_sample_file(path: Path, count: int = 80) -> Path:
    lines = [f"竞技场验收样例标题 {index}\t{index % len(CLASSES)}" for index in range(count)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def make_service(
    tmp: Path,
    db_name: str,
    *,
    adapters: dict[str, ModelAdapter] | None = None,
    test_file: Path | None = None,
    store: object | None = None,
    rng_seed: int = 20261021,
) -> ArenaService:
    return ArenaService(
        registry=REGISTRY,
        adapters=adapters if adapters is not None else make_fake_adapters(),
        classes=CLASSES,
        logger=LOGGER,
        db_path=tmp / db_name,
        test_file=test_file or write_sample_file(tmp / "arena_test.txt"),
        store=store,  # type: ignore[arg-type]
        rng=random.Random(rng_seed),
    )


# ---------------------------------------------------------------------------
# 1. Elo 纯函数手算对拍
# ---------------------------------------------------------------------------
def elo_checks() -> None:
    check(
        "elo constants: initial=1200 k=24",
        ELO_INITIAL == 1200.0 and ELO_K == 24.0,
        f"initial={ELO_INITIAL} k={ELO_K}",
    )

    # 手算组 1：1200 vs 1200 -> E=0.5；胜者 1212，负者 1188。
    expected_equal = expected_score(1200.0, 1200.0)
    winner_equal, loser_equal = apply_elo(1200.0, 1200.0)
    check(
        "elo hand case 1: E(1200,1200)=0.5",
        abs(expected_equal - 0.5) < 1e-12,
        repr(expected_equal),
    )
    check(
        "elo hand case 1: winner 1212.0 loser 1188.0",
        abs(winner_equal - 1212.0) < 1e-12 and abs(loser_equal - 1188.0) < 1e-12,
        f"{winner_equal!r}/{loser_equal!r}",
    )
    check(
        "elo hand case 1: sum conserved",
        abs((winner_equal + loser_equal) - 2400.0) < 1e-9,
        repr(winner_equal + loser_equal),
    )

    # 手算组 2：1200 vs 1400 -> E=0.2402530733520421；
    # 胜者 1218.233926239551，负者 1381.766073760449（K=24）。
    expected_underdog = expected_score(1200.0, 1400.0)
    winner_underdog, loser_underdog = apply_elo(1200.0, 1400.0)
    check(
        "elo hand case 2: E(1200,1400)=0.2402530733520421",
        abs(expected_underdog - 0.2402530733520421) < 1e-12,
        repr(expected_underdog),
    )
    check(
        "elo hand case 2: winner 1218.233926239551",
        abs(winner_underdog - 1218.233926239551) < 1e-9,
        repr(winner_underdog),
    )
    check(
        "elo hand case 2: loser 1381.766073760449",
        abs(loser_underdog - 1381.766073760449) < 1e-9,
        repr(loser_underdog),
    )
    check(
        "elo hand case 2: symmetry E(a,b)+E(b,a)=1",
        abs(expected_score(1200.0, 1400.0) + expected_score(1400.0, 1200.0) - 1.0)
        < 1e-12,
    )
    print(
        "   elo reference: E(1200,1400)=%.15f winner=%.12f loser=%.12f"
        % (expected_underdog, winner_underdog, loser_underdog)
    )


# ---------------------------------------------------------------------------
# 2. Wilson 区间
# ---------------------------------------------------------------------------
def wilson_checks() -> None:
    lower, upper = wilson_interval(5, 10)
    check(
        "wilson: known case wins=5 n=10",
        abs(lower - 0.23658959361548731) < 1e-12
        and abs(upper - 0.7634104063845126) < 1e-12,
        f"{lower!r}/{upper!r}",
    )
    check(
        "wilson: empty sample interval (0,1)",
        wilson_interval(0, 0) == (0.0, 1.0),
        str(wilson_interval(0, 0)),
    )
    bounds_ok = True
    detail = ""
    for wins, n in ((0, 10), (10, 10), (0, 50), (25, 50), (49, 50), (1, 500)):
        lo, hi = wilson_interval(wins, n)
        if not (0.0 <= lo <= hi <= 1.0):
            bounds_ok = False
            detail = f"wins={wins} n={n} -> {lo},{hi}"
            break
    check("wilson: bounds in [0,1] and lower<=upper", bounds_ok, detail)


# ---------------------------------------------------------------------------
# 3. 投票流程 / DB / Elo 复算 / 持久化
# ---------------------------------------------------------------------------
def _async_vote_flow(
    service: ArenaService, votes: int, *, picker: random.Random
) -> list[object]:
    async def run() -> list[object]:
        outcomes: list[object] = []
        for index in range(votes):
            next_item = await service.next_item(f"req-flow-{index}")
            candidate = picker.choice(next_item.candidates)
            outcome = await service.vote(
                client_id=f"flow-client-{index}",
                item_id=next_item.item_id,
                anon_id=candidate.anon_id,
                request_id=f"req-flow-{index}",
            )
            outcomes.append(outcome)
        return outcomes

    return asyncio.run(run())


def _replay_elo(
    db_path: Path,
) -> tuple[list[str], bool, dict[str, float]]:
    """从 vote_item 从头复算 Elo，返回 (mismatches, final_ok, ratings)。"""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        items = conn.execute(
            "SELECT * FROM vote_item ORDER BY vote_id, vote_item_id"
        ).fetchall()
        stored_models = {
            row["model_id"]: float(row["rating"])
            for row in conn.execute("SELECT model_id, rating FROM arena_model")
        }
    finally:
        conn.close()

    ratings = {model_id: ELO_INITIAL for model_id in stored_models}
    mismatches: list[str] = []
    for row in items:
        winner = row["winner_model_id"]
        opponent = row["opponent_model_id"]
        if winner not in ratings or opponent not in ratings:
            mismatches.append(f"unknown model in vote_item {row['vote_item_id']}")
            continue
        expected = expected_score(ratings[winner], ratings[opponent])
        winner_after, opponent_after = apply_elo(ratings[winner], ratings[opponent])
        if abs(row["expected_winner"] - expected) > 1e-12:
            mismatches.append(f"expected mismatch vote_item={row['vote_item_id']}")
        if abs(row["winner_before"] - ratings[winner]) > 1e-12:
            mismatches.append(f"winner_before mismatch vote_item={row['vote_item_id']}")
        if abs(row["opponent_before"] - ratings[opponent]) > 1e-12:
            mismatches.append(f"opponent_before mismatch vote_item={row['vote_item_id']}")
        if abs(row["winner_after"] - winner_after) > 1e-9:
            mismatches.append(f"winner_after mismatch vote_item={row['vote_item_id']}")
        if abs(row["opponent_after"] - opponent_after) > 1e-9:
            mismatches.append(f"opponent_after mismatch vote_item={row['vote_item_id']}")
        ratings[winner] = winner_after
        ratings[opponent] = opponent_after

    final_ok = all(
        abs(stored_models[model_id] - ratings[model_id]) < 1e-9
        for model_id in ratings
    )
    return mismatches, final_ok, ratings


def flow_checks(tmp: Path) -> None:
    flow_dir = tmp / "flow"
    flow_dir.mkdir(parents=True, exist_ok=True)
    db_path = flow_dir / "tmf.db"

    # 预置一张「其他模块」表与数据，验证 CREATE TABLE IF NOT EXISTS 不会 DROP。
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS feedback (feedback_id INTEGER PRIMARY KEY, note TEXT)")
        conn.execute("INSERT INTO feedback (note) VALUES ('b4-placeholder')")
        conn.commit()
    finally:
        conn.close()

    test_file = write_sample_file(flow_dir / "arena_test.txt")

    # 空库榜单：无投票时 5 个模型均为 warming_up、rank=null、win_rate=null、
    # Wilson 区间退化为 (0,1)，不暗示任何倾向。
    fresh = make_service(flow_dir, "fresh.db", test_file=test_file)
    fresh_board = asyncio.run(fresh.leaderboard("req-fresh"))
    check(
        "leaderboard fresh db: all warming_up, rank null, win_rate null, wilson (0,1)",
        fresh_board.status == "warming_up"
        and len(fresh_board.entries) == 5
        and all(entry.votes == 0 for entry in fresh_board.entries)
        and all(entry.rank is None and entry.warming_up for entry in fresh_board.entries)
        and all(entry.win_rate is None for entry in fresh_board.entries)
        and all(
            entry.wilson_lower == 0.0 and entry.wilson_upper == 1.0
            for entry in fresh_board.entries
        ),
        str(
            [
                (entry.model_id, entry.votes, entry.rank, entry.win_rate,
                 entry.wilson_lower, entry.wilson_upper)
                for entry in fresh_board.entries
            ]
        ),
    )
    fresh.close()

    service = make_service(flow_dir, "tmf.db", test_file=test_file)
    outcomes = _async_vote_flow(service, 10, picker=random.Random(7))

    check(
        "flow: 10 votes all applied 4 games",
        len(outcomes) == 10 and all(outcome.games_applied == 4 for outcome in outcomes),
        str([outcome.games_applied for outcome in outcomes]),
    )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        vote_count = conn.execute("SELECT COUNT(*) FROM vote").fetchone()[0]
        vote_item_count = conn.execute("SELECT COUNT(*) FROM vote_item").fetchone()[0]
        per_vote = conn.execute(
            "SELECT vote_id, COUNT(*) AS n FROM vote_item GROUP BY vote_id ORDER BY vote_id"
        ).fetchall()
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        feedback_count = conn.execute("SELECT COUNT(*) FROM feedback").fetchone()[0]
        feedback_note = conn.execute("SELECT note FROM feedback").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        # 每票的对手集合：应为其他 4 个模型，且顺序 = model_id 升序（确定性）。
        order_ok = True
        order_detail = ""
        for vote_row in conn.execute(
            "SELECT vote_id, model_id FROM vote ORDER BY vote_id"
        ).fetchall():
            rows = conn.execute(
                "SELECT winner_model_id, opponent_model_id FROM vote_item"
                " WHERE vote_id = ? ORDER BY vote_item_id",
                (vote_row["vote_id"],),
            ).fetchall()
            winner = rows[0]["winner_model_id"] if rows else None
            opponents = [row["opponent_model_id"] for row in rows]
            expected_opponents = sorted(
                model_id for model_id in MODEL_IDS if model_id != winner
            )
            if (
                winner != vote_row["model_id"]
                or opponents != expected_opponents
                or len(rows) != 4
            ):
                order_ok = False
                order_detail = f"vote={vote_row['vote_id']} winner={winner} opponents={opponents}"
                break
        stored_ranks = {
            row["model_id"]: float(row["rating"])
            for row in conn.execute("SELECT model_id, rating FROM arena_model")
        }
    finally:
        conn.close()

    check("flow: vote rows == 10", vote_count == 10, str(vote_count))
    check("flow: vote_item rows == 40", vote_item_count == 40, str(vote_item_count))
    check(
        "flow: each vote has exactly 4 games",
        len(per_vote) == 10 and all(row["n"] == 4 for row in per_vote),
        str([row["n"] for row in per_vote]),
    )
    check("flow: journal_mode is wal", str(journal_mode).lower() == "wal", journal_mode)
    check(
        "flow: other tables preserved (feedback row)",
        feedback_count == 1 and feedback_note == "b4-placeholder",
        f"count={feedback_count} note={feedback_note}",
    )
    check(
        "flow: arena tables created",
        {"arena_item", "arena_model", "vote", "vote_item"} <= tables,
        str(sorted(tables)),
    )
    check(
        "flow: elo update order deterministic (opponents sorted)",
        order_ok,
        order_detail,
    )

    mismatches, final_ok, replay_ratings = _replay_elo(db_path)
    check(
        "flow: elo recomputed from vote_item matches every before/after",
        not mismatches,
        "; ".join(mismatches[:3]),
    )
    check(
        "flow: final arena_model elo matches full replay",
        final_ok,
        str({model_id: round(replay_ratings[model_id], 6) for model_id in replay_ratings}),
    )

    # 关闭后新建 store/service：vote/vote_item/Elo 仍在。
    service.close()
    reopened = make_service(flow_dir, "tmf.db", test_file=test_file)
    leaderboard = asyncio.run(reopened.leaderboard("req-reopen"))
    reloaded = {
        entry.model_id: entry.elo for entry in leaderboard.entries
    }
    check(
        "persistence: vote/vote_item still present after reopen",
        _count_rows(db_path, "vote") == 10 and _count_rows(db_path, "vote_item") == 40,
        f"vote={_count_rows(db_path, 'vote')} vote_item={_count_rows(db_path, 'vote_item')}",
    )
    check(
        "persistence: elo still present after reopen",
        all(
            abs(reloaded[model_id] - round(stored_ranks[model_id], 2)) < 1e-9
            for model_id in MODEL_IDS
        ),
        str(reloaded),
    )
    check(
        "leaderboard after 10 votes: warming_up, no rank",
        leaderboard.status == "warming_up"
        and all(entry.warming_up for entry in leaderboard.entries)
        and all(entry.rank is None for entry in leaderboard.entries)
        and all(entry.sample_size == 10 for entry in leaderboard.entries)
        and sum(entry.votes for entry in leaderboard.entries) == 10,
        str([(entry.model_id, entry.votes, entry.rank) for entry in leaderboard.entries]),
    )
    bounds_ok = all(
        0.0 <= entry.wilson_lower <= entry.wilson_upper <= 1.0
        for entry in leaderboard.entries
    )
    check("leaderboard after 10 votes: wilson bounds valid", bounds_ok)
    check(
        "leaderboard data_kind / note / min_votes contract",
        leaderboard.min_votes_for_ranking == WARMUP_MIN_VOTES,
    )
    reopened.close()


def _count_rows(db_path: Path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 4. TMF_DB_PATH 环境变量覆盖
# ---------------------------------------------------------------------------
def env_override_checks(tmp: Path) -> None:
    env_dir = tmp / "env"
    env_dir.mkdir(parents=True, exist_ok=True)
    db_file = env_dir / "custom.db"
    test_file = write_sample_file(env_dir / "arena_test.txt")
    os.environ["TMF_DB_PATH"] = str(db_file)
    try:
        service = ArenaService(
            registry=REGISTRY,
            adapters=make_fake_adapters(),
            classes=CLASSES,
            logger=LOGGER,
            test_file=test_file,
            rng=random.Random(99),
        )
    finally:
        os.environ.pop("TMF_DB_PATH", None)
    check("env: TMF_DB_PATH override creates db at that path", db_file.is_file(), str(db_file))
    conn = sqlite3.connect(str(db_file))
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    check("env: overridden db uses WAL", str(mode).lower() == "wal", str(mode))
    service.close()


# ---------------------------------------------------------------------------
# 5. HTTP 契约（TestClient，不监听真实端口）
# ---------------------------------------------------------------------------
class BrokenStore:
    """用于验证 SQLite 异常 -> 503 统一错误（不泄露堆栈）。"""

    def ensure_models(self, model_ids: object) -> None:
        pass

    def save_item(self, **kwargs: object) -> None:
        raise ArenaStoreError("simulated db failure")

    def get_item(self, item_id: str) -> None:
        raise ArenaStoreError("simulated db failure")

    def has_vote(self, client_id: str, item_id: str) -> bool:
        raise ArenaStoreError("simulated db failure")

    def record_vote(self, **kwargs: object) -> dict[str, object]:
        raise ArenaStoreError("simulated db failure")

    def leaderboard_stats(self, model_ids: object) -> dict[str, object]:
        raise ArenaStoreError("simulated db failure")

    def close(self) -> None:
        pass


class CaptureHandler(logging.Handler):
    """捕获业务日志，用于断言日志里没有标题 / 模型身份。"""

    def __init__(self) -> None:
        super().__init__()
        self.entries: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        payload = {
            key: getattr(record, key, None)
            for key in ("event", "request_id", "error_code")
        }
        payload["message"] = record.getMessage()
        self.entries.append(json.dumps(payload, ensure_ascii=False, default=str))



def _leaked_identities(serialized: str) -> list[str]:
    leaked = [pattern for pattern in IDENTITY_PATTERNS if pattern in serialized]
    for key in ("model_id", "display_name", "model_version", "true_label"):
        if f'"{key}"' in serialized:
            leaked.append(f"key:{key}")
    return leaked


def _error_detail(body: dict) -> tuple[str | None, dict | None]:
    error = body.get("error")
    if not isinstance(error, dict):
        return None, None
    details = error.get("details")
    return error.get("code"), details if isinstance(details, dict) else None


def http_checks(tmp: Path) -> None:
    http_dir = tmp / "http"
    http_dir.mkdir(parents=True, exist_ok=True)
    test_file = write_sample_file(http_dir / "arena_test.txt")

    from serving.gateway.main import create_app

    # 让 lifespan 的默认 ArenaService 也使用临时库，避免污染 serving/run/tmf.db。
    os.environ["TMF_DB_PATH"] = str(http_dir / "lifespan.db")
    capture = CaptureHandler()
    LOGGER.addHandler(capture)
    try:
        app = create_app()
        with TestClient(app) as client:
            failures_at_start = len(FAILURES)
            _http_contract_checks(client, app, http_dir, test_file, capture)
            if len(FAILURES) > failures_at_start:
                print("   (http contract 失败明细见上方 FAIL 行)")
    finally:
        LOGGER.removeHandler(capture)
        os.environ.pop("TMF_DB_PATH", None)


def _http_contract_checks(
    client: TestClient,
    app: object,
    http_dir: Path,
    test_file: Path,
    capture: CaptureHandler,
) -> None:
    service = make_service(http_dir, "http.db", test_file=test_file)
    app.state.arena = service  # type: ignore[attr-defined]

    # --- /arena/next：匿名、结构、标签不泄露 ---------------------------------
    response = client.get("/api/v1/arena/next")
    check("http next: status 200", response.status_code == 200, str(response.status_code))
    body = response.json()
    check(
        "http next: top-level fields exactly contract",
        isinstance(body, dict)
        and set(body) == {"request_id", "item_id", "title", "candidates", "partial"},
        str(sorted(body) if isinstance(body, dict) else body),
    )
    check(
        "http next: request_id matches header",
        response.headers.get("X-Request-ID") == body.get("request_id"),
        str(response.headers.get("X-Request-ID")),
    )
    candidates = body.get("candidates") or []
    check(
        "http next: 5 anonymous candidates with only anon_id/pred_class/latency_ms",
        len(candidates) == 5
        and all(
            set(candidate) == {"anon_id", "pred_class", "latency_ms"}
            for candidate in candidates
        ),
        str(candidates)[:300],
    )
    anon_ids = [candidate["anon_id"] for candidate in candidates]
    check(
        "http next: anon_id distinct and not model ids",
        len(set(anon_ids)) == len(anon_ids)
        and not (set(anon_ids) & set(MODEL_IDS)),
        str(anon_ids),
    )
    check(
        "http next: partial=false when all models ok",
        body.get("partial") is False,
        str(body.get("partial")),
    )
    serialized = json.dumps(body, ensure_ascii=False)
    leaked = _leaked_identities(serialized)
    check(
        "http next: serialized body grep finds no model identity / true label",
        not leaked,
        ",".join(leaked),
    )
    header_text = json.dumps(dict(response.headers), ensure_ascii=False)
    header_leaked = _leaked_identities(header_text)
    check(
        "http next: headers grep finds no model identity",
        not header_leaked,
        ",".join(header_leaked),
    )
    check(
        "http next: vote not revealed before voting",
        '"selected"' not in serialized and "true_label" not in serialized,
    )

    # 匿名映射只存在于服务端 SQLite（arena_item.candidates_json）。
    conn = sqlite3.connect(str(http_dir / "http.db"))
    try:
        stored_row = conn.execute(
            "SELECT candidates_json FROM arena_item WHERE item_id = ?",
            (body["item_id"],),
        ).fetchone()
    finally:
        conn.close()
    stored_mapping = json.loads(stored_row[0]) if stored_row else []
    check(
        "http next: anon->model mapping only in server-side sqlite",
        stored_row is not None
        and {entry["model_id"] for entry in stored_mapping} == set(MODEL_IDS)
        and {entry["anon_id"] for entry in stored_mapping} == set(anon_ids),
        str(stored_mapping)[:300],
    )

    # 服务端打乱：同一 fake 映射下连续 6 次 next 的候选类别顺序不总相同。
    orders = set()
    for _ in range(6):
        sample = client.get("/api/v1/arena/next")
        if sample.status_code == 200:
            orders.add(tuple(c["pred_class"] for c in sample.json()["candidates"]))
    check(
        "http next: candidate order is shuffled server-side",
        len(orders) > 1,
        str(orders),
    )

    # --- /arena/vote：投票后只揭晓被选中模型 --------------------------------
    item_id = body["item_id"]
    chosen = candidates[0]
    vote_response = client.post(
        "/api/v1/arena/vote",
        json={
            "client_id": "http-client-1",
            "item_id": item_id,
            "anon_id": chosen["anon_id"],
        },
    )
    check("http vote: status 200", vote_response.status_code == 200, str(vote_response.status_code))
    vote_body = vote_response.json()
    check(
        "http vote: response fields contract",
        set(vote_body) == {"request_id", "selected", "elo_after", "games_applied"},
        str(sorted(vote_body)),
    )
    selected = vote_body.get("selected") or {}
    selected_model = selected.get("model_id")
    check(
        "http vote: selected model revealed with display_name/pred_class",
        selected_model in MODEL_IDS
        and selected.get("display_name") == DISPLAY_NAMES.get(selected_model)
        and selected.get("pred_class") == chosen["pred_class"],
        str(selected),
    )
    check(
        "http vote: games_applied=4 for 5 candidates",
        vote_body.get("games_applied") == 4,
        str(vote_body.get("games_applied")),
    )
    check(
        "http vote: selected winner elo moves above 1200",
        isinstance(vote_body.get("elo_after"), (int, float))
        and vote_body["elo_after"] > ELO_INITIAL,
        str(vote_body.get("elo_after")),
    )
    vote_serialized = json.dumps(vote_body, ensure_ascii=False)
    other_models = [model_id for model_id in MODEL_IDS if model_id != selected_model]
    leaked_others = [f'"{model_id}"' for model_id in other_models if f'"{model_id}"' in vote_serialized]
    check(
        "http vote: other candidate identities not revealed",
        not leaked_others
        and all(f'"{anon}"' not in vote_serialized for anon in anon_ids if anon != chosen["anon_id"])
        and "model_version" not in vote_serialized,
        ",".join(leaked_others),
    )

    # --- 匿名性：日志不记标题、不记模型身份 ---------------------------------
    log_text = "\n".join(capture.entries)
    log_title = body.get("title") or ""
    log_leaked = _leaked_identities(log_text)
    check(
        "http logs: no news title in arena logs",
        bool(capture.entries) and bool(log_title) and log_title not in log_text,
        f"entries={len(capture.entries)} title_in_log={log_title in log_text}",
    )
    check(
        "http logs: no model identity in arena logs",
        not log_leaked,
        ",".join(log_leaked),
    )

    # --- 去重 409 ------------------------------------------------------------
    duplicate = client.post(
        "/api/v1/arena/vote",
        json={
            "client_id": "http-client-1",
            "item_id": item_id,
            "anon_id": chosen["anon_id"],
        },
    )
    code, _ = _error_detail(duplicate.json())
    check(
        "http vote: duplicate client+item -> 409 ARENA_DUPLICATE_VOTE",
        duplicate.status_code == 409 and code == "ARENA_DUPLICATE_VOTE",
        f"{duplicate.status_code}/{code}",
    )

    # --- 无效 item / anon / client ------------------------------------------
    missing_item = client.post(
        "/api/v1/arena/vote",
        json={
            "client_id": "http-client-1",
            "item_id": "no-such-item",
            "anon_id": "no-such-anon",
        },
    )
    code, _ = _error_detail(missing_item.json())
    check(
        "http vote: unknown item -> 404 ARENA_ITEM_NOT_FOUND",
        missing_item.status_code == 404 and code == "ARENA_ITEM_NOT_FOUND",
        f"{missing_item.status_code}/{code}",
    )

    second_next = client.get("/api/v1/arena/next").json()
    invalid_anon = client.post(
        "/api/v1/arena/vote",
        json={
            "client_id": "http-client-2",
            "item_id": second_next["item_id"],
            "anon_id": "definitely-not-a-candidate",
        },
    )
    code, _ = _error_detail(invalid_anon.json())
    check(
        "http vote: invalid anon -> 400 ARENA_ANON_INVALID",
        invalid_anon.status_code == 400 and code == "ARENA_ANON_INVALID",
        f"{invalid_anon.status_code}/{code}",
    )

    invalid_client = client.post(
        "/api/v1/arena/vote",
        json={
            "client_id": "",
            "item_id": second_next["item_id"],
            "anon_id": second_next["candidates"][0]["anon_id"],
        },
    )
    code, _ = _error_detail(invalid_client.json())
    check(
        "http vote: invalid client_id -> 400 INVALID_REQUEST",
        invalid_client.status_code == 400 and code == "INVALID_REQUEST",
        f"{invalid_client.status_code}/{code}",
    )

    # --- leaderboard：低票数不排序 ------------------------------------------
    leaderboard = client.get("/api/v1/arena/leaderboard")
    check(
        "http leaderboard: status 200",
        leaderboard.status_code == 200,
        str(leaderboard.status_code),
    )
    board = leaderboard.json()
    entries = board.get("entries") or []
    check(
        "http leaderboard: contract fields",
        set(board) == {
            "request_id",
            "status",
            "data_kind",
            "note",
            "min_votes_for_ranking",
            "entries",
        }
        and board.get("data_kind") == "vote"
        and board.get("min_votes_for_ranking") == WARMUP_MIN_VOTES,
        str(sorted(board)),
    )
    note = str(board.get("note") or "")
    check(
        "http leaderboard: note has no forbidden wording",
        "准确率" not in note and "客观排名" not in note and bool(note),
        note,
    )
    check(
        "http leaderboard: entries fixed config order, 5 models",
        [entry["model_id"] for entry in entries] == MODEL_ORDER,
        str([entry["model_id"] for entry in entries]),
    )
    check(
        "http leaderboard: warming_up, rank null, sample_size == 1",
        board.get("status") == "warming_up"
        and all(entry["warming_up"] for entry in entries)
        and all(entry["rank"] is None for entry in entries)
        and all(entry["sample_size"] == 1 for entry in entries)
        and sum(entry["votes"] for entry in entries) == 1,
        str([(entry["model_id"], entry["votes"], entry["rank"]) for entry in entries]),
    )
    wilson_ok = all(
        0.0 <= entry["wilson_lower"] <= entry["wilson_upper"] <= 1.0
        for entry in entries
    )
    check("http leaderboard: wilson bounds valid", wilson_ok)
    check(
        "http leaderboard: selection votes / sample_size consistent",
        all(
            entry["votes"] == entry["wins"]
            and entry["sample_size"] == entry["wins"] + entry["losses"]
            and (
                entry["win_rate"] is None
                or abs(entry["win_rate"] - entry["votes"] / entry["sample_size"]) < 1e-9
            )
            for entry in entries
        ),
    )

    # --- 部分模型失败：partial + 可投子集 -----------------------------------
    # 混合两种失败形态：一个抛异常（llm），一个返回 status=error（distill）。
    partial_service = make_service(
        http_dir,
        "http-partial.db",
        adapters=make_fake_adapters(raising=("llm",), erroring=("distill",)),
        test_file=test_file,
    )
    app.state.arena = partial_service  # type: ignore[attr-defined]
    partial_response = client.get("/api/v1/arena/next")
    partial_body = partial_response.json()
    partial_candidates = partial_body.get("candidates") or []
    check(
        "http partial: 3 candidates and partial=true",
        partial_response.status_code == 200
        and partial_body.get("partial") is True
        and len(partial_candidates) == 3,
        str(partial_body)[:300],
    )
    partial_vote = client.post(
        "/api/v1/arena/vote",
        json={
            "client_id": "http-partial-client",
            "item_id": partial_body["item_id"],
            "anon_id": partial_candidates[0]["anon_id"],
        },
    )
    check(
        "http partial: vote applied against available opponents only (2 games)",
        partial_vote.status_code == 200
        and partial_vote.json().get("games_applied") == 2,
        str(partial_vote.text)[:300],
    )

    # --- 不足 2 个候选 / 全部失败 -> 503 ------------------------------------
    one_service = make_service(
        http_dir,
        "http-one.db",
        adapters=make_fake_adapters(raising=("rf", "fasttext", "bert", "llm")),
        test_file=test_file,
    )
    app.state.arena = one_service  # type: ignore[attr-defined]
    one_response = client.get("/api/v1/arena/next")
    code, details = _error_detail(one_response.json())
    check(
        "http next: 1 available candidate -> 503 ARENA_UNAVAILABLE",
        one_response.status_code == 503
        and code == "ARENA_UNAVAILABLE"
        and (details or {}).get("available") == 1,
        f"{one_response.status_code}/{code}/{details}",
    )

    all_fail_service = make_service(
        http_dir,
        "http-allfail.db",
        adapters=make_fake_adapters(erroring=MODEL_IDS),
        test_file=test_file,
    )
    app.state.arena = all_fail_service  # type: ignore[attr-defined]
    all_fail = client.get("/api/v1/arena/next")
    code, details = _error_detail(all_fail.json())
    check(
        "http next: all models fail -> 503 ARENA_UNAVAILABLE",
        all_fail.status_code == 503
        and code == "ARENA_UNAVAILABLE"
        and (details or {}).get("available") == 0,
        f"{all_fail.status_code}/{code}/{details}",
    )

    # --- 空测试集 -> 503 ARENA_NO_ITEMS -------------------------------------
    empty_file = http_dir / "empty_test.txt"
    empty_file.write_text("", encoding="utf-8")
    empty_service = make_service(
        http_dir, "http-empty.db", test_file=empty_file
    )
    app.state.arena = empty_service  # type: ignore[attr-defined]
    empty_response = client.get("/api/v1/arena/next")
    code, _ = _error_detail(empty_response.json())
    check(
        "http next: empty test set -> 503 ARENA_NO_ITEMS",
        empty_response.status_code == 503 and code == "ARENA_NO_ITEMS",
        f"{empty_response.status_code}/{code}",
    )

    # --- DB 异常 -> 503 ARENA_STORE_ERROR（不泄露堆栈） ---------------------
    broken_service = make_service(
        http_dir, "http-broken.db", test_file=test_file, store=BrokenStore()
    )
    app.state.arena = broken_service  # type: ignore[attr-defined]
    broken_next = client.get("/api/v1/arena/next")
    code, _ = _error_detail(broken_next.json())
    broken_text = broken_next.text
    check(
        "http next: db failure -> 503 ARENA_STORE_ERROR without stack",
        broken_next.status_code == 503
        and code == "ARENA_STORE_ERROR"
        and "Traceback" not in broken_text,
        f"{broken_next.status_code}/{code}",
    )
    broken_vote = client.post(
        "/api/v1/arena/vote",
        json={
            "client_id": "http-broken-client",
            "item_id": "any-item",
            "anon_id": "any-anon",
        },
    )
    code, _ = _error_detail(broken_vote.json())
    check(
        "http vote: db failure -> 503 ARENA_STORE_ERROR",
        broken_vote.status_code == 503 and code == "ARENA_STORE_ERROR",
        f"{broken_vote.status_code}/{code}",
    )

    # --- 限速：同一 client 21 票/分钟 ---------------------------------------
    rate_service = make_service(http_dir, "http-rate.db", test_file=test_file)
    app.state.arena = rate_service  # type: ignore[attr-defined]
    statuses: list[int] = []
    last_body: dict = {}
    for index in range(21):
        next_response = client.get("/api/v1/arena/next")
        if next_response.status_code != 200:
            statuses.append(next_response.status_code)
            break
        candidate = next_response.json()["candidates"][index % 3]
        vote_response = client.post(
            "/api/v1/arena/vote",
            json={
                "client_id": "rate-client-1",
                "item_id": next_response.json()["item_id"],
                "anon_id": candidate["anon_id"],
            },
        )
        statuses.append(vote_response.status_code)
        if index == 20:
            last_body = vote_response.json()
    rate_code, rate_details = _error_detail(last_body)
    check(
        "http rate limit: first 20 votes ok, 21st -> 429 ARENA_RATE_LIMITED",
        statuses[:20] == [200] * 20
        and statuses[20] == 429
        and rate_code == "ARENA_RATE_LIMITED"
        and (rate_details or {}).get("limit") == 20,
        f"statuses={statuses} code={rate_code} details={rate_details}",
    )
    check(
        "http rate limit: unified error body with request_id",
        isinstance(last_body.get("request_id"), str)
        and isinstance(last_body.get("error"), dict),
        str(last_body)[:200],
    )

    # 限速只影响该 client，其他 client 仍可投票。
    other_client_next = client.get("/api/v1/arena/next").json()
    other_client_vote = client.post(
        "/api/v1/arena/vote",
        json={
            "client_id": "rate-client-2",
            "item_id": other_client_next["item_id"],
            "anon_id": other_client_next["candidates"][0]["anon_id"],
        },
    )
    check(
        "http rate limit: other client not affected",
        other_client_vote.status_code == 200,
        str(other_client_vote.status_code),
    )

    rate_service.close()


# ---------------------------------------------------------------------------
# 5. >=50 票才 ready 并按 Elo 给 rank
# ---------------------------------------------------------------------------
def ranking_checks(tmp: Path) -> None:
    rank_dir = tmp / "ranking"
    rank_dir.mkdir(parents=True, exist_ok=True)
    test_file = write_sample_file(rank_dir / "arena_test.txt")
    service = make_service(rank_dir, "ranking.db", test_file=test_file)

    async def run() -> None:
        picker = random.Random(11)
        for index in range(WARMUP_MIN_VOTES):
            next_item = await service.next_item(f"req-rank-{index}")
            candidate = picker.choice(next_item.candidates)
            await service.vote(
                client_id=f"rank-client-{index}",
                item_id=next_item.item_id,
                anon_id=candidate.anon_id,
                request_id=f"req-rank-{index}",
            )

    asyncio.run(run())
    board = asyncio.run(service.leaderboard("req-rank-board"))
    entries = list(board.entries)
    check(
        "ranking: status ready after all models >= 50 comparison samples",
        board.status == "ready" and all(entry.sample_size >= 50 for entry in entries),
        str([(entry.model_id, entry.votes) for entry in entries]),
    )
    check(
        "ranking: entries keep config order",
        [entry.model_id for entry in entries] == MODEL_ORDER,
        str([entry.model_id for entry in entries]),
    )
    ranks = sorted(entry.rank for entry in entries)
    check(
        "ranking: rank assigned as 1..5",
        ranks == [1, 2, 3, 4, 5],
        str([(entry.model_id, entry.elo, entry.rank) for entry in entries]),
    )
    ordered = sorted(entries, key=lambda entry: entry.rank)
    check(
        "ranking: rank follows elo desc",
        all(
            ordered[index].elo >= ordered[index + 1].elo
            for index in range(len(ordered) - 1)
        ),
        str([(entry.model_id, entry.elo, entry.rank) for entry in ordered]),
    )
    check(
        "ranking: all entries warming_up=false, win_rate in [0,1]",
        all(not entry.warming_up for entry in entries)
        and all(
            entry.win_rate is not None and 0.0 <= entry.win_rate <= 1.0
            for entry in entries
        ),
    )
    print(f"   ranking: {[(entry.model_id, entry.elo, entry.rank) for entry in entries]}")
    service.close()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> int:
    for env_key in ("TMF_DB_PATH", "TMF_ARENA_TEST_FILE"):
        os.environ.pop(env_key, None)

    elo_checks()
    wilson_checks()

    with tempfile.TemporaryDirectory(prefix="tmf-arena-verify-") as raw_tmp:
        tmp = Path(raw_tmp)
        sections = (
            ("flow", flow_checks),
            ("env", env_override_checks),
            ("http", http_checks),
            ("ranking", ranking_checks),
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

    if FAILURES:
        print(f"RESULT FAIL ({len(FAILURES)} failed / {TOTAL} checks)")
        for name in FAILURES:
            print(f"  FAILED: {name}")
        return 1
    print(f"RESULT PASS ({TOTAL} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
