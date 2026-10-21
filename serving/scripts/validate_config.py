#!/usr/bin/env python
"""只校验网关配置与网络环境变量，不启动服务、不加载模型。

退出码：
- 0：配置合法（输出 CONFIG_OK ...）
- 2：配置非法（输出 CONFIG_ERROR: <可定位的文件/字段/原因>）

校验内容：
- workers.yaml / models.yaml / class.txt；
- worker base_url 与端口；
- TMF_BIND_HOST / TMF_GATEWAY_HOST / TMF_GATEWAY_PORT / TMF_FRONTEND_* /
  TMF_WORKER_HOST / TMF_WORKER_URL_<ID> / TMF_CORS_ORIGINS。
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

    network = settings.network
    worker_hosts = sorted({worker.base_url for worker in settings.workers.values()})
    print(
        "CONFIG_OK "
        f"workers={len(settings.workers)} "
        f"models={len(settings.models)} "
        f"classes={len(settings.classes)} "
        f"default={settings.default_model} "
        f"bind={network.bind_host} "
        f"gateway={network.gateway_host}:{network.gateway_port} "
        f"frontend={network.frontend_origin} "
        f"cors={','.join(network.cors_origins)} "
        f"worker_urls={','.join(worker_hosts)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
