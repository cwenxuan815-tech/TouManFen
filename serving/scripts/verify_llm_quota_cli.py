#!/usr/bin/env python
"""阶段 4C 隔离验收：LLM 配额离线管理员 CLI。

使用临时目录/临时 SQLite/运行时随机 HMAC key：
- 不启动网关/worker，不调用 Provider；
- 不接触 serving/run/tmf.db；
- 不输出或写入实际 HMAC key。

覆盖：查询不存在/0/1/3 记录、缺失/短 key、错误 key、无效 IP、损坏/只读 DB、
确认短语、原子解除、审计事件、历史事件保留、双 IP 隔离、解除后 3 次再封禁、
正式 DB 防误操作、无批量/清库命令，以及输出隐私检查。
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CLI = PROJECT_ROOT / "serving" / "scripts" / "manage_llm_quota.py"
PRODUCTION_DB = (PROJECT_ROOT / "serving" / "run" / "tmf.db").resolve()
sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

from serving.services.llm_quota import LlmQuotaStore  # noqa: E402

FAILURES: list[str] = []
TOTAL = 0
TEST_KEY = secrets.token_hex(32)
OTHER_KEY = secrets.token_hex(32)
IP0 = "203.0.113.10"
IP1 = "203.0.113.11"
IP2 = "203.0.113.12"
IP_MISSING = "203.0.113.99"
TITLE = "不该进入CLI输出的标题"
REASON = "客服确认误触发，人工复核后解除"
OUTPUTS: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


def run_cli(
    args: list[str],
    *,
    key: str | None = TEST_KEY,
    db_env: str | None = None,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(PROJECT_ROOT), env.get("PYTHONPATH", "")]
    )
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("TMF_LLM_QUOTA_HMAC_KEY", None)
    if key is not None:
        env["TMF_LLM_QUOTA_HMAC_KEY"] = key
    env.pop("TMF_DB_PATH", None)
    if db_env is not None:
        env["TMF_DB_PATH"] = db_env
    result = subprocess.run(
        [sys.executable, str(CLI), *args],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    OUTPUTS.extend([result.stdout or "", result.stderr or ""])
    return result


def create_store(db_path: Path) -> LlmQuotaStore:
    return LlmQuotaStore(db_path, TEST_KEY.encode("utf-8"))


def seed(db_path: Path) -> None:
    store = create_store(db_path)
    try:
        store.reserve(IP1, request_id="seed-1", route="/classify")
        for _ in range(3):
            store.reserve(IP2, request_id="seed-2", route="/classify")
    finally:
        store.close()


def row_state(db_path: Path, ip: str) -> tuple[int, bool]:
    store = create_store(db_path)
    try:
        usage = store.get_usage(ip)
        return usage.used, usage.blocked
    finally:
        store.close()


def event_counts(db_path: Path) -> tuple[int, int]:
    conn = sqlite3.connect(str(db_path))
    try:
        reserved = conn.execute(
            "SELECT COUNT(*) FROM llm_quota_event WHERE operation = 'reserve_allowed'"
        ).fetchone()[0]
        admin = conn.execute(
            "SELECT COUNT(*) FROM llm_quota_event WHERE operation = 'admin_unblock'"
        ).fetchone()[0]
        return int(reserved), int(admin)
    finally:
        conn.close()


def test_status(root: Path) -> None:
    print("\n=== 1. 查询：不存在 / 0 / 1 / 3 次 ===")
    db = root / "status.db"
    seed(db)

    missing = run_cli(["--db", str(db), "status", IP_MISSING])
    check("missing record exit 0", missing.returncode == 0, str(missing.returncode))
    check("missing record message", "尚无配额记录" in missing.stdout, missing.stdout)

    one = run_cli(["--db", str(db), "status", IP1])
    check(
        "1-used status fields",
        one.returncode == 0
        and "已使用: 1/3" in one.stdout
        and "封禁: 否" in one.stdout,
        one.stdout,
    )

    three = run_cli(["--db", str(db), "status", IP2])
    check(
        "3-used blocked status",
        three.returncode == 0
        and "已使用: 3/3" in three.stdout
        and "封禁: 是" in three.stdout
        and "封禁时间:" in three.stdout,
        three.stdout,
    )

    # 默认路径：TMF_DB_PATH 指向临时库时，不显式 --db 也能查询。
    default_path = run_cli(["status", IP1], db_env=str(db))
    check(
        "default temp TMF_DB_PATH status works",
        default_path.returncode == 0 and "已使用: 1/3" in default_path.stdout,
        default_path.stdout,
    )


def test_key_and_input_failures(root: Path) -> None:
    print("\n=== 2. key / IP / DB 故障 fail-closed ===")
    db = root / "failures.db"
    seed(db)

    missing_key = run_cli(["--db", str(db), "status", IP1], key=None)
    check("missing HMAC key exits nonzero", missing_key.returncode != 0, str(missing_key.returncode))
    check(
        "missing HMAC key does not print key value",
        TEST_KEY not in missing_key.stdout + missing_key.stderr,
    )

    short_key = run_cli(["--db", str(db), "status", IP1], key="short")
    check("short HMAC key exits nonzero", short_key.returncode != 0, str(short_key.returncode))

    # 合法但错误的 key：HMAC 设计上无法区分“错 key”和“新 IP”，因此查询
    # 只能显示无记录；解除必须失败，且不能改变原记录。
    wrong_key = run_cli(["--db", str(db), "status", IP1], key=OTHER_KEY)
    check(
        "wrong key status reports no record (no key recovery by design)",
        wrong_key.returncode == 0 and "尚无配额记录" in wrong_key.stdout,
        wrong_key.stdout,
    )
    wrong_unblock = run_cli(
        [
            "--db",
            str(db),
            "unblock",
            IP2,
            "--reason",
            REASON,
            "--confirm",
            "RESET-LLM-QUOTA",
        ],
        key=OTHER_KEY,
    )
    check(
        "wrong key unblock exits nonzero without changing row",
        wrong_unblock.returncode != 0 and row_state(db, IP2) == (3, True),
        f"rc={wrong_unblock.returncode} state={row_state(db, IP2)}",
    )

    invalid_ip = run_cli(["--db", str(db), "status", "not-an-ip"])
    check("invalid IP exits nonzero", invalid_ip.returncode != 0, str(invalid_ip.returncode))

    corrupt = root / "corrupt.db"
    corrupt.write_bytes(b"not-a-sqlite-database")
    corrupt_result = run_cli(["--db", str(corrupt), "status", IP1])
    check("corrupt DB exits nonzero", corrupt_result.returncode != 0, str(corrupt_result.returncode))
    check(
        "corrupt DB output has no stack/SQL/key",
        "Traceback" not in corrupt_result.stdout + corrupt_result.stderr
        and "SELECT" not in corrupt_result.stdout + corrupt_result.stderr
        and TEST_KEY not in corrupt_result.stdout + corrupt_result.stderr,
    )

    missing_db = root / "missing.db"
    missing_db_result = run_cli(["--db", str(missing_db), "status", IP1])
    check("missing DB exits nonzero and is not created", missing_db_result.returncode != 0 and not missing_db.exists())

    readonly = root / "readonly.db"
    seed(readonly)
    old_mode = readonly.stat().st_mode
    try:
        os.chmod(readonly, stat.S_IREAD)
        readonly_status = run_cli(["--db", str(readonly), "status", IP1])
        readonly_unblock = run_cli(
            [
                "--db",
                str(readonly),
                "unblock",
                IP2,
                "--reason",
                REASON,
                "--confirm",
                "RESET-LLM-QUOTA",
            ]
        )
        check(
            "read-only DB status exits nonzero",
            readonly_status.returncode != 0,
            str(readonly_status.returncode),
        )
        check(
            "read-only DB unblock exits nonzero without changing row",
            readonly_unblock.returncode != 0,
            str(readonly_unblock.returncode),
        )
    finally:
        os.chmod(readonly, old_mode)
    check("read-only DB row unchanged", row_state(readonly, IP2) == (3, True))


def test_unblock_confirmation_and_audit(root: Path) -> None:
    print("\n=== 3. 确认、原子解除、审计与历史保留 ===")
    db = root / "unblock.db"
    seed(db)
    reserved_before, admin_before = event_counts(db)
    check("seed reserve events exist", reserved_before == 4, str(reserved_before))
    check("no admin event before unblock", admin_before == 0, str(admin_before))

    no_confirm = run_cli(
        ["--db", str(db), "unblock", IP2, "--reason", REASON]
    )
    check("missing confirmation exits nonzero", no_confirm.returncode != 0, str(no_confirm.returncode))
    check("missing confirmation does not reset", row_state(db, IP2) == (3, True))

    wrong_confirm = run_cli(
        [
            "--db",
            str(db),
            "unblock",
            IP2,
            "--reason",
            REASON,
            "--confirm",
            "WRONG",
        ]
    )
    check("wrong confirmation exits nonzero", wrong_confirm.returncode != 0, str(wrong_confirm.returncode))
    check("wrong confirmation does not reset", row_state(db, IP2) == (3, True))

    missing_reason = run_cli(
        [
            "--db",
            str(db),
            "unblock",
            IP2,
            "--reason",
            "   ",
            "--confirm",
            "RESET-LLM-QUOTA",
        ]
    )
    check("blank reason exits nonzero", missing_reason.returncode != 0, str(missing_reason.returncode))
    check("blank reason does not reset", row_state(db, IP2) == (3, True))

    missing_row = run_cli(
        [
            "--db",
            str(db),
            "unblock",
            IP_MISSING,
            "--reason",
            REASON,
            "--confirm",
            "RESET-LLM-QUOTA",
        ]
    )
    check("unblock missing record exits nonzero", missing_row.returncode != 0, str(missing_row.returncode))

    # 单独验证双 IP 隔离：解除 IP2 后 IP1 的 1 次记录不受影响。
    good = run_cli(
        [
            "--db",
            str(db),
            "unblock",
            IP2,
            "--reason",
            REASON,
            "--confirm",
            "RESET-LLM-QUOTA",
        ]
    )
    check("correct confirmation exits 0", good.returncode == 0, good.stdout + good.stderr)
    check("unblock output shows 3 -> 0", "使用次数: 3 -> 0" in good.stdout, good.stdout)
    check("unblock output does not print raw IP", IP2 not in good.stdout + good.stderr)

    after_state = row_state(db, IP2)
    check("unblock resets used/blocked", after_state == (0, False), str(after_state))
    zero_status = run_cli(["--db", str(db), "status", IP2])
    check(
        "status shows 0/3 after reset",
        zero_status.returncode == 0
        and "已使用: 0/3" in zero_status.stdout
        and "封禁: 否" in zero_status.stdout,
        zero_status.stdout,
    )
    check("other IP history unchanged", row_state(db, IP1) == (1, False))

    reserved_after, admin_after = event_counts(db)
    check("reserve events retained", reserved_after == reserved_before, f"{reserved_before}->{reserved_after}")
    check("one admin event appended", admin_after == 1, str(admin_after))

    conn = sqlite3.connect(str(db))
    try:
        event = conn.execute(
            "SELECT operation, used_before, used_after, admin_note, route"
            " FROM llm_quota_event WHERE operation = 'admin_unblock'"
            " ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    check(
        "admin audit event fields",
        event is not None
        and event[0] == "admin_unblock"
        and event[1] == 3
        and event[2] == 0
        and event[3] == REASON
        and event[4] == "offline-cli",
        str(event),
    )

    # 解除后重新拥有 3 次；第 4 次仍拒绝。
    store = create_store(db)
    try:
        allowed = [store.reserve(IP2, request_id=f"after-{i}", route="/classify").allowed for i in range(3)]
        fourth = store.reserve(IP2, request_id="after-4", route="/classify")
        check("reset IP can use 3 more calls", allowed == [True, True, True], str(allowed))
        check("reset IP 4th call denied", fourth.allowed is False, str(fourth))
    finally:
        store.close()


def test_atomic_rollback(root: Path) -> None:
    print("\n=== 4. 审计写失败时回滚 ===")
    db = root / "rollback.db"
    seed(db)
    reserved_before, admin_before = event_counts(db)
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "CREATE TRIGGER fail_admin_event BEFORE INSERT ON llm_quota_event"
            " BEGIN SELECT RAISE(ABORT, 'forced admin audit failure'); END;"
        )
        conn.commit()
    finally:
        conn.close()

    result = run_cli(
        [
            "--db",
            str(db),
            "unblock",
            IP2,
            "--reason",
            REASON,
            "--confirm",
            "RESET-LLM-QUOTA",
        ]
    )
    check("audit failure exits nonzero", result.returncode != 0, str(result.returncode))
    check("quota row unchanged after audit failure", row_state(db, IP2) == (3, True))
    reserved_after, admin_after = event_counts(db)
    check(
        "audit failure rolled back both updates",
        reserved_after == reserved_before and admin_after == admin_before,
    )


def test_production_protection(root: Path) -> None:
    print("\n=== 5. 正式 DB 防误操作 ===")
    explicit = run_cli(["--db", str(PRODUCTION_DB), "status", IP1])
    check(
        "explicit production DB without confirmation rejected",
        explicit.returncode != 0 and "正式" in explicit.stderr,
        explicit.stderr,
    )

    default_prod = run_cli(["status", IP1], db_env=str(PRODUCTION_DB))
    check(
        "default production DB rejected",
        default_prod.returncode != 0 and "正式" in default_prod.stderr,
        default_prod.stderr,
    )

    allow_without_db = run_cli(
        ["--allow-production-db", "status", IP1],
        db_env=str(PRODUCTION_DB),
    )
    check(
        "allow flag without explicit DB rejected for production default",
        allow_without_db.returncode != 0 and "正式" in allow_without_db.stderr,
        allow_without_db.stderr,
    )

    clear = run_cli(["--db", str(root / "status.db"), "clear"])
    reset_all = run_cli(["--db", str(root / "status.db"), "reset-all"])
    check("no clear command", clear.returncode != 0)
    check("no reset-all command", reset_all.returncode != 0)


def test_privacy(root: Path) -> None:
    print("\n=== 6. 输出/数据库隐私 ===")
    joined = "\n".join(OUTPUTS)
    for ip in (IP0, IP1, IP2, IP_MISSING):
        check(f"CLI output excludes raw IP {ip[-2:]}", ip not in joined)
    check("CLI output excludes HMAC key", TEST_KEY not in joined)
    check("CLI output excludes title", TITLE not in joined)
    check("CLI output has no traceback", "Traceback" not in joined)

    db = root / "unblock.db"
    conn = sqlite3.connect(str(db))
    try:
        values: list[str] = []
        for table in ("llm_ip_quota", "llm_quota_event"):
            for row in conn.execute(f"SELECT * FROM {table}").fetchall():
                values.extend(str(value) for value in row if value is not None)
    finally:
        conn.close()
    stored = "\n".join(values)
    for ip in (IP0, IP1, IP2, IP_MISSING):
        check(f"quota DB excludes raw IP {ip[-2:]}", ip not in stored)
    check("quota DB excludes HMAC key", TEST_KEY not in stored)
    check("quota DB excludes title", TITLE not in stored)
    check("quota DB has admin_unblock event", "admin_unblock" in stored)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="tmf-quota-cli-") as raw:
        root = Path(raw)
        test_status(root)
        test_key_and_input_failures(root)
        test_unblock_confirmation_and_audit(root)
        test_atomic_rollback(root)
        test_production_protection(root)
        test_privacy(root)

    if FAILURES:
        print(f"\nRESULT FAIL ({len(FAILURES)} failed / {TOTAL} checks)")
        for name in FAILURES:
            print(f"  FAILED: {name}")
        return 1
    print(f"\nRESULT PASS ({TOTAL} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
