#!/usr/bin/env python
"""B4 反馈数据显式清除工具。

清单 §4.4 / B4：反馈保留期 30 天，惰性清理由网关启动与 ``/feedback/stats``
完成；本工具用于演示前显式清除 ``feedback`` 表（删除后不可恢复）。

安全边界
--------
- **只对 ``TMF_DB_PATH`` 指向的库操作**；未设置该环境变量直接拒绝运行
  （避免误删默认 ``serving/run/tmf.db`` 之外/之内的数据）；
- 只 ``DELETE FROM feedback``，不 DROP 表，不触碰 B7 的 ``vote`` /
  ``vote_item`` / ``arena_*`` 数据；
- 无 ``--purge`` 时只打印 dry-run 数量，不做任何写操作。

CLI：
    $env:TMF_DB_PATH='<db 路径>'
    python tools/purge_feedback.py            # dry-run：只打印数量
    python tools/purge_feedback.py --purge    # 显式清除 feedback 表全部行

退出码：0 = 成功；2 = 配置/用法错误或数据库不可读。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path

try:  # 让 Windows 控制台也按 UTF-8 输出中文
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover - 非交互环境可能不支持
    pass

# 与 serving/services/feedback.py 的 FEEDBACK_RETENTION_DAYS 保持一致（30 天）。
# 本工具保持纯标准库，便于在网关依赖未注入的环境直接运行。
RETENTION_DAYS = 30


def _resolve_db_path() -> Path:
    raw = (os.environ.get("TMF_DB_PATH") or "").strip()
    if not raw:
        print(
            "错误：必须显式设置 TMF_DB_PATH 才能运行本工具（不允许操作默认库）。",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return Path(raw)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table,),
    ).fetchone()
    return row is not None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="B4 反馈数据显式清除（--purge 才写库；默认 dry-run）。"
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="显式删除 feedback 表全部行（不可恢复）；不加则只打印数量。",
    )
    args = parser.parse_args(argv)

    db_path = _resolve_db_path()
    if not db_path.is_file():
        print(f"数据库不存在，无需清理：{db_path}")
        return 0

    try:
        conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    except sqlite3.Error as exc:
        print(f"错误：无法打开数据库（{type(exc).__name__}）", file=sys.stderr)
        return 2

    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        if not _table_exists(conn, "feedback"):
            print("feedback 表不存在（该库尚未记录反馈），无需清理。")
            return 0

        total = int(
            conn.execute("SELECT COUNT(*) AS n FROM feedback").fetchone()["n"]
        )
        cutoff = time.time() - RETENTION_DAYS * 86400
        expired = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM feedback WHERE created_at_epoch < ?",
                (cutoff,),
            ).fetchone()["n"]
        )

        if not args.purge:
            print(
                f"[dry-run] 反馈总数={total}，其中超过 {RETENTION_DAYS} 天={expired}；"
                "未做任何修改（加 --purge 显式清除全部反馈）。"
            )
            return 0

        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("DELETE FROM feedback")
            deleted = int(cursor.rowcount) if cursor.rowcount is not None else 0
            conn.execute("COMMIT")
        except sqlite3.Error as exc:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            print(f"错误：清除失败（{type(exc).__name__}）", file=sys.stderr)
            return 2

        remaining = int(
            conn.execute("SELECT COUNT(*) AS n FROM feedback").fetchone()["n"]
        )
        print(
            f"已清除 feedback {max(0, deleted)} 行（清除前 {total}，剩余 {remaining}）；"
            "vote / arena 数据未删除。"
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
