#!/usr/bin/env python
"""阶段 4A 隔离验收：可信客户端 IP 解析 + SQLite LLM 配额基础存储。

约束：
- 只使用临时目录/临时 SQLite，不触碰 serving/run/tmf.db；
- 不启动 gateway/worker/真实服务，不调用 Provider；
- 不接入路由/分类/arena；不实现管理员命令/接口。

运行：
    $env:PYTHONPATH='E:\\AIModels\\test\\TouManFen\\venv-gateway\\Lib\\site-packages'
    D:\\Miniconda3\\envs\\py3109\\python.exe serving/scripts/verify_llm_quota_infra.py
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from serving.gateway.client_ip import (  # noqa: E402
    ClientIpError,
    normalize_ip,
    parse_trusted_proxy_cidrs,
    resolve_client_ip,
)
from serving.gateway.quota_config import (  # noqa: E402
    DEFAULT_LLM_QUOTA_DB_PATH,
    LlmQuotaConfigError,
    load_llm_quota_settings,
)
from serving.services.llm_quota import (  # noqa: E402
    LlmQuotaIdentityError,
    LlmQuotaStore,
    LlmQuotaStoreError,
)

FAILURES: list[str] = []
# 测试运行时生成的临时 key，不是生产配置，也不写入仓库。
TEST_KEY = secrets.token_bytes(32)
TEST_KEY_TEXT = TEST_KEY.hex()
ROTATED_KEY = secrets.token_bytes(32)


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


def expect_raises(name: str, exc_type: type[BaseException], fn) -> None:
    try:
        fn()
    except exc_type:
        check(name, True)
    except Exception as exc:  # noqa: BLE001
        check(name, False, f"unexpected {type(exc).__name__}")
    else:
        check(name, False, "did not raise")


def test_client_ip() -> None:
    print("\n=== 1. 可信客户端 IP 解析 ===")
    trusted = parse_trusted_proxy_cidrs("10.0.0.0/8, 192.0.2.0/24")

    check(
        "direct peer used without forwarding",
        resolve_client_ip("203.0.113.10") == "203.0.113.10",
    )
    check(
        "untrusted peer forged XFF ignored",
        resolve_client_ip(
            "203.0.113.10",
            {"X-Forwarded-For": "198.51.100.99"},
            trusted,
        )
        == "203.0.113.10",
    )
    check(
        "trusted proxy extracts client",
        resolve_client_ip(
            "10.0.0.5",
            {"X-Forwarded-For": "198.51.100.7"},
            trusted,
        )
        == "198.51.100.7",
    )
    check(
        "trusted chain chooses rightmost untrusted, not left spoof prefix",
        resolve_client_ip(
            "10.0.0.5",
            {"X-Forwarded-For": "1.2.3.4, 198.51.100.7, 10.0.0.8"},
            trusted,
        )
        == "198.51.100.7",
    )
    check(
        "Forwarded for= chain parsed",
        resolve_client_ip(
            "10.0.0.5",
            {"Forwarded": "for=198.51.100.7;proto=https, for=10.0.0.8"},
            trusted,
        )
        == "198.51.100.7",
    )
    check(
        "header names case-insensitive",
        resolve_client_ip(
            "10.0.0.5",
            {"x-forwarded-for": "198.51.100.7"},
            trusted,
        )
        == "198.51.100.7",
    )
    check(
        "X-Real-IP alone cannot establish identity",
        _raises(
            ClientIpError,
            lambda: resolve_client_ip(
                "10.0.0.5",
                {"X-Real-IP": "198.51.100.7"},
                trusted,
            ),
        ),
    )
    check(
        "trusted proxy must provide forwarding identity",
        _raises(ClientIpError, lambda: resolve_client_ip("10.0.0.5", {}, trusted)),
    )
    check(
        "all-trusted chain cannot identify client",
        _raises(
            ClientIpError,
            lambda: resolve_client_ip(
                "10.0.0.5",
                {"X-Forwarded-For": "10.0.0.8"},
                trusted,
            ),
        ),
    )
    check(
        "malformed forwarding chain fails",
        _raises(
            ClientIpError,
            lambda: resolve_client_ip(
                "10.0.0.5",
                {"X-Forwarded-For": "not-an-ip"},
                trusted,
            ),
        ),
    )
    check(
        "empty peer fails",
        _raises(ClientIpError, lambda: resolve_client_ip("")),
    )
    check(
        "invalid peer fails",
        _raises(ClientIpError, lambda: resolve_client_ip("not-an-ip")),
    )
    check(
        "empty trusted proxy list is default",
        parse_trusted_proxy_cidrs(None) == (),
    )
    check(
        "invalid trusted CIDR fails",
        _raises(ClientIpError, lambda: parse_trusted_proxy_cidrs("not-a-cidr")),
    )
    check(
        "broad /0 trusted CIDR rejected",
        _raises(ClientIpError, lambda: parse_trusted_proxy_cidrs("0.0.0.0/0")),
    )

    check("IPv4 normalization", normalize_ip("198.51.100.7") == "198.51.100.7")
    check("IPv6 normalization", normalize_ip("2001:db8::1") == "2001:db8::1")
    check(
        "IPv4-mapped IPv6 normalization",
        normalize_ip("::ffff:198.51.100.7") == "198.51.100.7",
    )
    check(
        "IPv6 zone id stripped",
        normalize_ip("fe80::1%eth0") == "fe80::1",
    )
    check(
        "IPv4 port stripped in forwarded token",
        normalize_ip("198.51.100.7:443") == "198.51.100.7",
    )
    check(
        "bracketed IPv6 port stripped",
        normalize_ip("[2001:db8::1]:443") == "2001:db8::1",
    )
    check(
        "IPv6 variants remain distinct",
        normalize_ip("2001:db8::1") != normalize_ip("2001:db8::2"),
    )


def _raises(exc_type: type[BaseException], fn) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:  # noqa: BLE001
        return False
    return False


def test_settings() -> None:
    print("\n=== 2. 配置（HMAC key / DB / 可信代理） ===")
    settings = load_llm_quota_settings({"TMF_LLM_QUOTA_HMAC_KEY": TEST_KEY_TEXT})
    check("default DB path is serving/run/tmf.db", settings.db_path == DEFAULT_LLM_QUOTA_DB_PATH)
    check("default trusted proxies empty", settings.trusted_proxies == ())
    check("HMAC key not shown in settings repr", TEST_KEY_TEXT not in repr(settings))
    check(
        "custom DB path honored",
        load_llm_quota_settings(
            {
                "TMF_LLM_QUOTA_HMAC_KEY": TEST_KEY_TEXT,
                "TMF_DB_PATH": "relative-test.db",
            }
        ).db_path == Path("relative-test.db"),
    )
    check(
        "trusted proxy CIDRs loaded",
        len(
            load_llm_quota_settings(
                {
                    "TMF_LLM_QUOTA_HMAC_KEY": TEST_KEY_TEXT,
                    "TMF_TRUSTED_PROXY_CIDRS": "10.0.0.0/8, 192.0.2.0/24",
                }
            ).trusted_proxies
        )
        == 2,
    )
    expect_raises(
        "missing HMAC key fails closed",
        LlmQuotaConfigError,
        lambda: load_llm_quota_settings({}),
    )
    expect_raises(
        "short HMAC key rejected",
        LlmQuotaConfigError,
        lambda: load_llm_quota_settings({"TMF_LLM_QUOTA_HMAC_KEY": "short"}),
    )
    expect_raises(
        "invalid trusted CIDR rejected by config",
        LlmQuotaConfigError,
        lambda: load_llm_quota_settings(
            {
                "TMF_LLM_QUOTA_HMAC_KEY": TEST_KEY_TEXT,
                "TMF_TRUSTED_PROXY_CIDRS": "bad-cidr",
            }
        ),
    )
    expect_raises(
        "broad /0 trusted CIDR rejected by config",
        LlmQuotaConfigError,
        lambda: load_llm_quota_settings(
            {
                "TMF_LLM_QUOTA_HMAC_KEY": TEST_KEY_TEXT,
                "TMF_TRUSTED_PROXY_CIDRS": "0.0.0.0/0",
            }
        ),
    )


def test_store() -> None:
    print("\n=== 3. SQLite 配额存储 ===")
    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        db_path = root / "quota.db"
        ip1 = "203.0.113.10"
        ip2 = "203.0.113.11"
        store = LlmQuotaStore(db_path, TEST_KEY)

        results = [
            store.reserve(ip1, request_id=f"req-{index}", route="/classify")
            for index in range(1, 4)
        ]
        check(
            "first 3 reserves allowed",
            all(result.allowed for result in results)
            and [result.used_after for result in results] == [1, 2, 3],
            str(results),
        )
        fourth = store.reserve(ip1, request_id="req-4", route="/classify")
        check(
            "4th reserve denied, used stays 3",
            not fourth.allowed and fourth.used_before == 3 and fourth.used_after == 3 and fourth.blocked,
            str(fourth),
        )
        usage = store.get_usage(ip1)
        check("usage reports 3 and blocked", usage.used == 3 and usage.blocked, str(usage))

        check(
            "different IP independent",
            store.reserve(ip2, request_id="req-ip2", route="/compare").used_after == 1,
        )
        ipv6_a = "2001:db8::10"
        ipv6_b = "2001:db8::11"
        check(
            "IPv6 address is a distinct quota identity",
            store.reserve(ipv6_a, request_id="ipv6-a", route="/classify").allowed
            and store.reserve(ipv6_b, request_id="ipv6-b", route="/classify").allowed
            and store.get_usage(ipv6_a).ip_key != store.get_usage(ipv6_b).ip_key,
        )

        # 第二个 store 连接同一 DB，计数共享。
        store2 = LlmQuotaStore(db_path, TEST_KEY)
        check(
            "second store shares count",
            not store2.reserve(ip1, request_id="store2", route="/compare").allowed,
        )
        check(
            "second store sees used=3",
            store2.get_usage(ip1).used == 3,
        )

        # 关闭并重新打开，状态保留。
        store.close()
        store2.close()
        reopened = LlmQuotaStore(db_path, TEST_KEY)
        check(
            "reopened store persists block",
            not reopened.reserve(ip1, request_id="reopened", route="/classify").allowed
            and reopened.get_usage(ip1).used == 3,
        )

        # schema 幂等：再次打开不丢计数。
        reopened.close()
        reopened_again = LlmQuotaStore(db_path, TEST_KEY)
        check(
            "schema init is idempotent",
            reopened_again.get_usage(ip1).used == 3,
        )

        # 独立 Python 进程重新打开同一 DB，验证进程重启持久性。
        child_code = (
            "import os\n"
            "from serving.services.llm_quota import LlmQuotaStore\n"
            "store = LlmQuotaStore("
            "os.environ['VERIFY_LLM_QUOTA_DB'], "
            "bytes.fromhex(os.environ['VERIFY_LLM_QUOTA_KEY']))\n"
            "result = store.reserve('203.0.113.10', request_id='child', route='/classify')\n"
            "print('allowed' if result.allowed else 'denied')\n"
        )
        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = os.pathsep.join(
            [str(PROJECT_ROOT), child_env.get("PYTHONPATH", "")]
        )
        child_env["VERIFY_LLM_QUOTA_DB"] = str(db_path)
        child_env["VERIFY_LLM_QUOTA_KEY"] = TEST_KEY.hex()
        child = subprocess.run(
            [sys.executable, "-c", child_code],
            cwd=str(PROJECT_ROOT),
            env=child_env,
            capture_output=True,
            text=True,
            timeout=20,
        )
        check(
            "separate process sees persisted block",
            child.returncode == 0 and child.stdout.strip() == "denied",
            f"rc={child.returncode} out={child.stdout!r} err={child.stderr[:200]!r}",
        )

        # 5 并发只有一个 IP，只能 3 个成功。
        concurrent_ip = "203.0.113.20"
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [
                executor.submit(
                    reopened_again.reserve,
                    concurrent_ip,
                    request_id=f"concurrent-{index}",
                    route="/classify",
                )
                for index in range(5)
            ]
            concurrent_results = [future.result() for future in futures]
        check(
            "5 concurrent reserves allow exactly 3",
            sum(1 for result in concurrent_results if result.allowed) == 3,
            str(concurrent_results),
        )
        check(
            "concurrent usage ends at 3",
            reopened_again.get_usage(concurrent_ip).used == 3,
        )

        # reserve 即消耗；没有退款接口，后续失败不改变 used。
        no_refund_ip = "203.0.113.30"
        before = reopened_again.reserve(no_refund_ip, request_id="x", route="/classify")
        after = reopened_again.get_usage(no_refund_ip)
        check(
            "reserve is consumed before later failure; no refund",
            before.allowed and after.used == 1,
            str(after),
        )

        # 审计表结构/记录：含未来解除所需字段，但不提供解除能力。
        audit_columns = {
            row[1]
            for row in sqlite3.connect(str(db_path)).execute(
                "PRAGMA table_info(llm_quota_event)"
            ).fetchall()
        }
        check(
            "audit table has admin/unblock fields",
            {"ip_key", "operation", "used_before", "used_after", "admin_note", "request_id", "route"}
            <= audit_columns,
            str(audit_columns),
        )
        event_row = sqlite3.connect(str(db_path)).execute(
            "SELECT operation, used_before, used_after, request_id, route, admin_note"
            " FROM llm_quota_event ORDER BY event_id LIMIT 1"
        ).fetchone()
        check(
            "audit event records metadata and null admin note",
            event_row is not None
            and event_row[0] == "reserve_allowed"
            and event_row[1] == 0
            and event_row[2] == 1
            and event_row[3] == "req-1"
            and event_row[4] == "/classify"
            and event_row[5] is None,
            str(event_row),
        )

        # 不同 HMAC key 产生不同索引；旧 key 数据保留，不做自动迁移。
        rotated = LlmQuotaStore(db_path, ROTATED_KEY)
        check(
            "different HMAC key maps to new quota identity",
            rotated.get_usage(ip1).used == 0 and rotated.reserve(ip1, request_id="rotated", route="/classify").allowed,
        )
        old_key_rows = sqlite3.connect(str(db_path)).execute(
            "SELECT COUNT(*) FROM llm_ip_quota"
        ).fetchone()[0]
        check(
            "key rotation does not delete old HMAC rows",
            old_key_rows >= 2,
            str(old_key_rows),
        )
        rotated.close()

        # 数据库中无明文 IP/标题/Provider payload。
        all_values: list[str] = []
        conn = sqlite3.connect(str(db_path))
        for table in ("llm_ip_quota", "llm_quota_event"):
            for row in conn.execute(f"SELECT * FROM {table}").fetchall():
                all_values.extend(str(value) for value in row if value is not None)
        conn.close()
        check(
            "database has no plaintext IP",
            not any(ip in value for value in all_values for ip in (ip1, ip2)),
        )
        check(
            "database has no title/provider columns",
            "title" not in audit_columns and "provider" not in audit_columns,
        )

        # HMAC key 缺失/长度不足：初始化失败，不创建可用配额。
        expect_raises(
            "store rejects missing HMAC key",
            LlmQuotaConfigError,
            lambda: LlmQuotaStore(root / "missing-key.db", b""),
        )
        expect_raises(
            "store rejects short HMAC key",
            LlmQuotaConfigError,
            lambda: LlmQuotaStore(root / "short-key.db", b"short"),
        )
        expect_raises(
            "store rejects limit above 3",
            LlmQuotaConfigError,
            lambda: LlmQuotaStore(root / "bad-limit.db", TEST_KEY, limit=4),
        )
        expect_raises(
            "invalid client IP fails identity",
            LlmQuotaIdentityError,
            lambda: reopened_again.reserve("not-an-ip", request_id="bad", route="/classify"),
        )

        # 损坏 DB 初始化失败，不得 fail-open。
        corrupt_path = root / "corrupt.db"
        corrupt_path.write_bytes(b"not a sqlite database")
        expect_raises(
            "corrupt DB initialization fails closed",
            LlmQuotaStoreError,
            lambda: LlmQuotaStore(corrupt_path, TEST_KEY),
        )

        # 只读连接模拟：连接可打开但 BEGIN IMMEDIATE 写失败，必须返回 store error。
        read_only_path = root / "readonly.db"
        read_only_store = LlmQuotaStore(read_only_path, TEST_KEY)

        def read_only_connect() -> sqlite3.Connection:
            uri = read_only_path.resolve().as_uri() + "?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=2.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=2000")
            return conn

        read_only_store._connect = read_only_connect  # type: ignore[method-assign]
        expect_raises(
            "read-only quota DB fails closed",
            LlmQuotaStoreError,
            lambda: read_only_store.reserve("203.0.113.40", request_id="ro", route="/classify"),
        )
        read_only_store.close()

        # 事务失败模拟：触发器强制 INSERT 失败，必须回滚并抛 store error。
        trigger_path = root / "trigger.db"
        trigger_store = LlmQuotaStore(trigger_path, TEST_KEY)
        trigger_conn = sqlite3.connect(str(trigger_path))
        trigger_conn.executescript(
            "CREATE TRIGGER fail_llm_quota_insert BEFORE INSERT ON llm_ip_quota"
            " BEGIN SELECT RAISE(ABORT, 'forced failure'); END;"
        )
        trigger_conn.commit()
        trigger_conn.close()
        expect_raises(
            "transaction failure returns store error",
            LlmQuotaStoreError,
            lambda: trigger_store.reserve("203.0.113.50", request_id="tx", route="/classify"),
        )
        trigger_store.close()

        # 已关闭 store 不得退回内存/放行。
        reopened_again.close()
        expect_raises(
            "closed store fails closed",
            LlmQuotaStoreError,
            lambda: reopened_again.reserve("203.0.113.60", request_id="closed", route="/classify"),
        )


def main() -> int:
    print("=== 阶段 4A：可信 IP + SQLite LLM quota isolated verification ===")
    test_client_ip()
    test_settings()
    test_store()
    if FAILURES:
        print(f"\nRESULT FAIL ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("\nRESULT PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
