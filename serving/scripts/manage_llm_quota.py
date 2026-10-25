#!/usr/bin/env python
"""TMF LLM 每 IP 配额的离线管理员 CLI（阶段 4C）。

只做两件事：
- status <ip>：按现有 client_ip.py + HMAC 规则查询该 IP 的 used/limit/blocked；
- unblock <ip> --reason ... --confirm RESET-LLM-QUOTA：原子解除封禁并把当前额度
  重置为 0，同时写入 admin_unblock 审计事件。

安全边界：
- 不提供 HTTP 管理 API、不启动网络服务、不调用网关/worker/Provider；
- HMAC key 只从环境变量 TMF_LLM_QUOTA_HMAC_KEY 读取，不接受命令行参数；
- 不把明文 IP、HMAC key、标题或 Provider payload 写入输出/日志/数据库；
- 正式 serving/run/tmf.db 必须显式 --db 并加 --allow-production-db；
- 不删除/清空任何配额或审计记录；不支持批量解除、reset-all 或模糊匹配。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from serving.gateway.quota_config import (  # noqa: E402
    DEFAULT_LLM_QUOTA_DB_PATH,
    HMAC_KEY_ENV,
    MIN_HMAC_KEY_BYTES,
    LlmQuotaConfigError,
)
from serving.services.llm_quota import (  # noqa: E402
    LlmQuotaAdminError,
    LlmQuotaIdentityError,
    LlmQuotaStore,
    LlmQuotaStoreError,
    QuotaNotFoundError,
)

PRODUCTION_DB_PATH = (PROJECT_ROOT / "serving" / "run" / "tmf.db").resolve()
UNBLOCK_CONFIRMATION = "RESET-LLM-QUOTA"


class CliError(RuntimeError):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="manage_llm_quota.py",
        description=(
            "离线查看/解除 TMF LLM per-IP 配额。"
            " HMAC key 必须由环境变量提供；本工具不启动网关。"
        ),
        epilog=(
            "示例：\n"
            "  python serving/scripts/manage_llm_quota.py --db D:\\tmp\\quota.db status 203.0.113.10\n"
            "  python serving/scripts/manage_llm_quota.py --db D:\\tmp\\quota.db unblock 203.0.113.10 "
            "--reason \"客服确认误触发\" --confirm RESET-LLM-QUOTA\n\n"
            "正式 DB 需要显式 --db 并以 --allow-production-db 确认。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=None,
        help="quota SQLite 路径；省略时用 TMF_DB_PATH，再回退 serving/run/tmf.db",
    )
    parser.add_argument(
        "--allow-production-db",
        action="store_true",
        help="显式确认操作正式 serving/run/tmf.db（必须同时显式传 --db）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="查询指定 IP 的配额状态")
    status.add_argument("ip", help="客户端 IP；由 client_ip.py 规范化")

    unblock = sub.add_parser("unblock", help="解除指定 IP 的封禁并重置计数")
    unblock.add_argument("ip", help="客户端 IP；由 client_ip.py 规范化")
    unblock.add_argument(
        "--reason",
        required=True,
        help="管理员备注/理由，写入审计事件 admin_note",
    )
    unblock.add_argument(
        "--confirm",
        required=True,
        help=f"必须精确输入 {UNBLOCK_CONFIRMATION} 才会解除",
    )
    return parser


def _resolve_db_path(args: argparse.Namespace) -> Path:
    raw = (
        args.db
        if args.db is not None
        else os.environ.get("TMF_DB_PATH") or str(DEFAULT_LLM_QUOTA_DB_PATH)
    )
    try:
        db_path = Path(raw).expanduser().resolve()
    except OSError as exc:
        raise CliError("无法解析 quota DB 路径") from exc

    if db_path == PRODUCTION_DB_PATH:
        if args.db is None or not args.allow_production_db:
            raise CliError(
                "拒绝操作正式 quota DB：请显式传 --db <正式路径> "
                "并加 --allow-production-db；本工具不会自动创建/修改正式库。",
                exit_code=2,
            )
        print(
            f"警告：正在操作正式 quota DB：{db_path}",
            file=sys.stderr,
        )

    if not db_path.is_file():
        raise CliError(
            f"quota DB 不存在：{db_path}（不自动创建；请检查 --db 或 TMF_DB_PATH）"
        )
    # 本工具允许查询也遵循“不可写即拒绝”，避免只读/受保护库被误认为可管理。
    if not os.access(db_path, os.W_OK):
        raise CliError(f"quota DB 不可写：{db_path}；拒绝操作")
    return db_path


def _load_hmac_key() -> bytes:
    raw = os.environ.get(HMAC_KEY_ENV, "")
    key_text = raw.strip() if isinstance(raw, str) else ""
    if not key_text:
        raise CliError(
            f"环境变量 {HMAC_KEY_ENV} 未配置；拒绝操作（不会输出 key）",
            exit_code=2,
        )
    key = key_text.encode("utf-8")
    if len(key) < MIN_HMAC_KEY_BYTES:
        raise CliError(
            f"环境变量 {HMAC_KEY_ENV} 至少需要 {MIN_HMAC_KEY_BYTES} 字节",
            exit_code=2,
        )
    return key


def _open_store(db_path: Path, key: bytes) -> LlmQuotaStore:
    try:
        return LlmQuotaStore(db_path, key)
    except LlmQuotaConfigError as exc:
        raise CliError("quota 配置无效；拒绝操作") from exc
    except LlmQuotaStoreError as exc:
        raise CliError("quota DB 无法打开；拒绝操作") from exc


def _print_status(store: LlmQuotaStore, ip: str) -> int:
    try:
        usage = store.get_usage(ip)
    except LlmQuotaIdentityError as exc:
        raise CliError("IP 无效；未查询 quota") from exc
    except LlmQuotaStoreError as exc:
        raise CliError("quota 查询失败；拒绝操作") from exc

    if usage.first_used_at is None and usage.last_used_at is None:
        print("该 IP 尚无配额记录")
        return 0

    print("LLM 配额状态")
    print(f"已使用: {usage.used}/{usage.limit}")
    print(f"封禁: {'是' if usage.blocked else '否'}")
    print(f"首次使用: {usage.first_used_at or '-'}")
    print(f"最近使用: {usage.last_used_at or '-'}")
    print(f"封禁时间: {usage.blocked_at or '-'}")
    return 0


def _unblock(store: LlmQuotaStore, ip: str, reason: str) -> int:
    note = reason.strip()
    if not note:
        raise CliError("--reason 不能为空；未解除", exit_code=2)
    try:
        result = store.unblock(
            ip,
            admin_note=note,
            request_id="offline-cli",
        )
    except QuotaNotFoundError as exc:
        raise CliError("该 IP 尚无配额记录，未执行解除") from exc
    except LlmQuotaIdentityError as exc:
        raise CliError("IP 无效；未执行解除") from exc
    except LlmQuotaAdminError as exc:
        raise CliError("解除参数无效；未执行解除", exit_code=2) from exc
    except LlmQuotaStoreError as exc:
        raise CliError("quota 解除失败并已回滚；请检查 DB 状态") from exc

    print("解除完成")
    print(f"使用次数: {result.used_before} -> {result.used_after}")
    print(f"封禁: {'是' if result.blocked_before else '否'} -> {'是' if result.blocked_after else '否'}")
    print(f"审计事件 ID: {result.event_id if result.event_id is not None else '-'}")
    print("后续该 IP 重新拥有最多 3 次 LLM 调用额度。")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        db_path = _resolve_db_path(args)
        key = _load_hmac_key()

        if args.command == "unblock" and args.confirm != UNBLOCK_CONFIRMATION:
            raise CliError(
                f"确认短语不匹配；未解除。必须精确输入 {UNBLOCK_CONFIRMATION}",
                exit_code=2,
            )
        if args.command == "unblock":
            reason = args.reason.strip()
            if not reason:
                raise CliError("--reason 不能为空；未解除", exit_code=2)
        else:
            reason = ""

        store = _open_store(db_path, key)
        try:
            if args.command == "status":
                return _print_status(store, args.ip)
            return _unblock(store, args.ip, reason)
        finally:
            store.close()
    except CliError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return exc.exit_code
    except Exception:
        # 不输出堆栈/SQL/路径之外敏感内容；所有正常失败都应是 CliError。
        print("错误: 操作失败；未完成", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
