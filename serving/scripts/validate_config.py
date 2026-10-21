#!/usr/bin/env python
"""只校验网关配置，不启动服务、不加载模型。

退出码：
- 0：配置合法（输出 CONFIG_OK ...）
- 2：配置非法（输出 CONFIG_ERROR: <可定位的文件/字段/原因>）
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from serving.gateway.errors import ConfigError  # noqa: E402
from serving.gateway.settings import load_settings  # noqa: E402


def main() -> int:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print(f"CONFIG_ERROR: {exc}")
        return 2
    print(
        "CONFIG_OK "
        f"workers={len(settings.workers)} "
        f"models={len(settings.models)} "
        f"classes={len(settings.classes)} "
        f"default={settings.default_model}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
