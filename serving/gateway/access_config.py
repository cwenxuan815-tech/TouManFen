"""阶段 5B：访问管理配置（默认关闭）。

只读取访问开关和复用既有 HMAC key；不创建 manager、不启动服务。
- ``TMF_ACCESS_CONTROL_ENABLED``：默认 false；只有显式 truthy 才启用；
- ``TMF_LLM_QUOTA_HMAC_KEY``：启用时必需，与 LLM quota 共用同一密钥，
  但 access IP key 使用不同的 HMAC domain separator；
- ``TMF_ACCESS_STATUS_MIN_INTERVAL_MS``：状态轮询最小间隔，默认 1000ms。

本模块不输出、不记录任何 key 实际值。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Mapping

from .quota_config import HMAC_KEY_ENV, MIN_HMAC_KEY_BYTES

ACCESS_ENABLED_ENV = "TMF_ACCESS_CONTROL_ENABLED"
ACCESS_STATUS_INTERVAL_ENV = "TMF_ACCESS_STATUS_MIN_INTERVAL_MS"
DEFAULT_STATUS_MIN_INTERVAL_MS = 1000
MAX_STATUS_MIN_INTERVAL_MS = 10_000

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


class AccessConfigError(ValueError):
    """访问管理配置缺失或非法；启用时调用方应 fail-closed。"""


@dataclass(frozen=True)
class AccessSettings:
    enabled: bool
    hmac_key: bytes = field(default=b"", repr=False)
    status_min_interval_seconds: float = DEFAULT_STATUS_MIN_INTERVAL_MS / 1000.0


def _parse_bool(raw: str) -> bool:
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise AccessConfigError(
        f"环境变量 {ACCESS_ENABLED_ENV} 必须是 true/false 之一"
    )


def _parse_interval_ms(raw: str) -> int:
    try:
        value = int(raw.strip())
    except (TypeError, ValueError) as exc:
        raise AccessConfigError(
            f"环境变量 {ACCESS_STATUS_INTERVAL_ENV} 必须是整数毫秒"
        ) from exc
    if not 0 <= value <= MAX_STATUS_MIN_INTERVAL_MS:
        raise AccessConfigError(
            f"环境变量 {ACCESS_STATUS_INTERVAL_ENV} 必须在 0~{MAX_STATUS_MIN_INTERVAL_MS} 之间"
        )
    return value


def load_access_settings(
    env: Mapping[str, str] | None = None,
) -> AccessSettings:
    values = os.environ if env is None else env
    raw_enabled = values.get(ACCESS_ENABLED_ENV, "")
    enabled = _parse_bool(raw_enabled) if raw_enabled is not None and raw_enabled.strip() else False
    if not enabled:
        # 关闭时不读取 key/轮询配置，保持旧部署行为不变。
        return AccessSettings(enabled=False)

    raw_interval = values.get(
        ACCESS_STATUS_INTERVAL_ENV,
        str(DEFAULT_STATUS_MIN_INTERVAL_MS),
    )
    interval_ms = _parse_interval_ms(raw_interval)

    raw_key = values.get(HMAC_KEY_ENV, "")
    key_text = raw_key.strip() if isinstance(raw_key, str) else ""
    if not key_text:
        raise AccessConfigError(
            f"访问控制已启用，但环境变量 {HMAC_KEY_ENV} 未配置"
        )
    key = key_text.encode("utf-8")
    if len(key) < MIN_HMAC_KEY_BYTES:
        raise AccessConfigError(
            f"环境变量 {HMAC_KEY_ENV} 长度必须不少于 {MIN_HMAC_KEY_BYTES} 字节"
        )
    return AccessSettings(
        enabled=True,
        hmac_key=key,
        status_min_interval_seconds=interval_ms / 1000.0,
    )
