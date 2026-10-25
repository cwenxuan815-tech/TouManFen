"""LLM 配额基础设施配置（阶段 4A）。

只读取必要配置，不创建 DB、不启动服务、不注册路由：
- ``TMF_DB_PATH``：与 arena/feedback 共用 SQLite 路径，默认 serving/run/tmf.db；
- ``TMF_LLM_QUOTA_HMAC_KEY``：HMAC-SHA256 key，必须显式配置；缺失/过短 fail-closed；
- ``TMF_TRUSTED_PROXY_CIDRS``：可信代理 CIDR 逗号列表，默认空。

本模块不输出、不记录 key 实际值；key 仅以 ``repr=False`` 字段保存在设置对象中。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .client_ip import (
    ClientIpError,
    IPNetwork,
    parse_trusted_proxy_cidrs,
)

_SERVING_DIR = Path(__file__).resolve().parents[1]
DEFAULT_LLM_QUOTA_DB_PATH = _SERVING_DIR / "run" / "tmf.db"
HMAC_KEY_ENV = "TMF_LLM_QUOTA_HMAC_KEY"
TRUSTED_PROXY_ENV = "TMF_TRUSTED_PROXY_CIDRS"
MIN_HMAC_KEY_BYTES = 32


class LlmQuotaConfigError(ValueError):
    """配额基础设施配置缺失或非法；调用方应 fail-closed。"""


@dataclass(frozen=True)
class LlmQuotaSettings:
    db_path: Path
    hmac_key: bytes = field(repr=False)
    trusted_proxies: tuple[IPNetwork, ...] = ()


def load_llm_quota_settings(
    env: Mapping[str, str] | None = None,
) -> LlmQuotaSettings:
    """解析环境变量；缺失 HMAC key 时明确失败，绝不回退明文/自动生成 key。"""
    values = os.environ if env is None else env
    raw_key = values.get(HMAC_KEY_ENV, "")
    key_text = raw_key.strip() if isinstance(raw_key, str) else ""
    if not key_text:
        raise LlmQuotaConfigError(
            f"环境变量 {HMAC_KEY_ENV} 未配置；配额基础设施 fail-closed"
        )
    key = key_text.encode("utf-8")
    if len(key) < MIN_HMAC_KEY_BYTES:
        raise LlmQuotaConfigError(
            f"环境变量 {HMAC_KEY_ENV} 长度必须不少于 {MIN_HMAC_KEY_BYTES} 字节"
        )

    raw_db_path = values.get("TMF_DB_PATH", "").strip()
    db_path = Path(raw_db_path) if raw_db_path else DEFAULT_LLM_QUOTA_DB_PATH
    try:
        trusted_proxies = parse_trusted_proxy_cidrs(
            values.get(TRUSTED_PROXY_ENV, "")
        )
    except ClientIpError as exc:
        raise LlmQuotaConfigError(str(exc)) from exc
    return LlmQuotaSettings(
        db_path=db_path,
        hmac_key=key,
        trusted_proxies=trusted_proxies,
    )
