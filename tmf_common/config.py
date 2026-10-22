"""共享环境配置解析（网关 / 前端启动脚本 / 模型 worker）。

设计目标：
- 不再把某台机器的局域网 IP 写死在源码里；
- 默认值面向本地单机（``127.0.0.1``），局域网场景通过环境变量显式覆盖；
- 监听地址（bind）与客户端访问地址（connect/probe）区分：``0.0.0.0`` 只可用于监听；
- 所有错误消息只包含变量名与原因，不包含密钥或敏感值；
- 数值型运行时参数（如 ``TMF_TORCH_THREADS``）复用本模块的 ``env_int``，
  不另建一套配置框架。

环境变量一览（详见 README「环境配置」）：

======================  ==============================  ==================
变量                    作用                            默认
======================  ==============================  ==================
TMF_BIND_HOST           5 个 Flask worker 监听地址        127.0.0.1
TMF_GATEWAY_HOST        网关监听地址                      127.0.0.1
TMF_GATEWAY_PORT        网关监听端口                      8000
TMF_FRONTEND_HOST       前端 Vite 监听地址                127.0.0.1
TMF_FRONTEND_PORT       前端 Vite 端口                    5173
TMF_WORKER_HOST         网关调用 worker 时使用的主机      配置文件中的值
TMF_WORKER_URL_<ID>     单个 worker 的完整 base URL 覆盖  未设置
TMF_CORS_ORIGINS        追加允许的 CORS 来源（逗号分隔）  未设置
TMF_TORCH_THREADS       BERT/蒸馏 worker 推理线程数       4（范围 1~64）
VITE_API_BASE_URL       前端访问网关的 base URL           http://127.0.0.1:8000
======================  ==============================  ==================
"""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass
from urllib.parse import urlparse

DEFAULT_HOST = "127.0.0.1"
DEFAULT_GATEWAY_PORT = 8000
DEFAULT_FRONTEND_PORT = 5173

#: B12：BERT/蒸馏 worker 的推理线程数（``torch.set_num_threads``）。
#: 默认 4，可按机器核数用 ``TMF_TORCH_THREADS`` 覆盖（1~64）；线程数在模型
#: 加载前由各 worker 的预测服务应用，保证单机多模型并存时不互相抢满 CPU。
DEFAULT_TORCH_THREADS = 4
TORCH_THREADS_MIN = 1
TORCH_THREADS_MAX = 64

# 只能用于监听、不能作为客户端连接目标的地址
WILDCARD_HOSTS = frozenset({"0.0.0.0"})

_HOSTNAME_RE = re.compile(
    r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$"
)


class EnvConfigError(ValueError):
    """环境变量解析失败。消息只包含变量名与原因。"""


class NetworkConfigError(EnvConfigError):
    """网络配置非法。消息只包含变量名/字段与原因。"""


def _env(name: str) -> str | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip()
    return value or None


def validate_host(value: object, *, variable: str = "host") -> str:
    """校验纯主机名 / IPv4 地址（不含协议、端口、路径）。"""
    host = str(value).strip()
    if not host:
        raise NetworkConfigError(f"{variable} 不能为空")
    if "://" in host or any(ch in host for ch in ("/", ":", " ", "@", "?")):
        raise NetworkConfigError(
            f"{variable} 必须是纯主机名或 IPv4 地址，不能包含协议、端口或路径"
        )
    try:
        ipaddress.IPv4Address(host)
        return host
    except ValueError:
        pass
    if host.lower() == "localhost":
        return "localhost"
    if not _HOSTNAME_RE.match(host):
        raise NetworkConfigError(f"{variable} 不是合法的主机名或 IPv4 地址")
    return host


def validate_port(value: object, *, variable: str = "port") -> int:
    try:
        port = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise NetworkConfigError(f"{variable} 必须是 1~65535 的整数") from exc
    if not 1 <= port <= 65535:
        raise NetworkConfigError(f"{variable} 必须在 1~65535 之间")
    return port


def env_host(name: str, default: str) -> str:
    return validate_host(_env(name) or default, variable=name)


def env_port(name: str, default: int) -> int:
    raw = _env(name)
    return validate_port(default if raw is None else raw, variable=name)


def env_int(
    name: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """解析整数环境变量（复用本模块统一的取值/校验约定）。

    - 未设置或空串：返回 ``default``；
    - 非整数（含 ``"3.5"``）：抛 :class:`EnvConfigError`；
    - 超出 ``[minimum, maximum]``（未指定的边界不检查）：抛 :class:`EnvConfigError`。

    用于 worker 运行时参数（如 ``TMF_TORCH_THREADS``），避免再建一套解析框架。
    """
    raw = _env(name)
    if raw is None:
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise EnvConfigError(f"环境变量 {name} 必须是整数") from exc
    if minimum is not None and value < minimum:
        raise EnvConfigError(f"环境变量 {name} 必须不小于 {minimum}")
    if maximum is not None and value > maximum:
        raise EnvConfigError(f"环境变量 {name} 必须不大于 {maximum}")
    return value


def resolve_bind_host() -> str:
    """5 个 worker 的监听地址。"""
    return env_host("TMF_BIND_HOST", DEFAULT_HOST)


def resolve_gateway_host(default: str = DEFAULT_HOST) -> str:
    return env_host("TMF_GATEWAY_HOST", default)


def resolve_gateway_port(default: int = DEFAULT_GATEWAY_PORT) -> int:
    return env_port("TMF_GATEWAY_PORT", default)


def resolve_frontend_host() -> str:
    return env_host("TMF_FRONTEND_HOST", DEFAULT_HOST)


def resolve_frontend_port() -> int:
    return env_port("TMF_FRONTEND_PORT", DEFAULT_FRONTEND_PORT)


def probe_host(bind_host: str) -> str:
    """把监听地址转换成可连接的探测地址。"""
    return "127.0.0.1" if bind_host in WILDCARD_HOSTS else bind_host


def default_frontend_origin() -> str:
    """前端默认来源；监听 0.0.0.0 时使用回环地址作为默认 CORS 来源。"""
    host = probe_host(resolve_frontend_host())
    return f"http://{host}:{resolve_frontend_port()}"


def validate_http_url(
    value: object, *, variable: str = "url", allow_path: bool = False
) -> str:
    """校验 http(s) URL；默认只允许 scheme://host[:port]。"""
    text = str(value).strip()
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise NetworkConfigError(f"{variable} 必须形如 http(s)://host[:port]")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise NetworkConfigError(f"{variable} 不允许用户信息、查询串或片段")
    if not allow_path and parsed.path not in {"", "/"}:
        raise NetworkConfigError(f"{variable} 不应包含路径")
    validate_host(parsed.hostname, variable=f"{variable} 的主机部分")
    if parsed.port is not None:
        validate_port(parsed.port, variable=f"{variable} 的端口部分")
    return text.rstrip("/")


def validate_origin(value: object, *, variable: str = "CORS 来源") -> str:
    text = str(value).strip().rstrip("/")
    if not text or text == "*":
        raise NetworkConfigError(f"{variable} 不能为 *，必须显式声明 http(s) 来源")
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise NetworkConfigError(f"{variable} 必须形如 http(s)://host[:port]")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise NetworkConfigError(f"{variable} 不允许用户信息、查询串或片段")
    if parsed.path not in {"", "/"}:
        raise NetworkConfigError(f"{variable} 不应包含路径")
    validate_host(parsed.hostname, variable=f"{variable} 的主机部分")
    if parsed.port is not None:
        validate_port(parsed.port, variable=f"{variable} 的端口部分")
    return text


def resolve_worker_url(default_url: str, worker_id: str) -> str:
    """解析 worker base_url。

    优先级：
    1. ``TMF_WORKER_URL_<WORKER_ID>``（完整 URL 覆盖）；
    2. ``TMF_WORKER_HOST``（只替换主机，端口沿用配置文件）；
    3. 配置文件中的默认值。
    """
    override = _env(f"TMF_WORKER_URL_{worker_id.upper()}")
    if override:
        return validate_http_url(
            override, variable=f"TMF_WORKER_URL_{worker_id.upper()}"
        )

    default = validate_http_url(default_url, variable=f"worker {worker_id} base_url")
    host_override = _env("TMF_WORKER_HOST")
    if not host_override:
        return default

    host = validate_host(host_override, variable="TMF_WORKER_HOST")
    parsed = urlparse(default)
    port = parsed.port
    port_part = f":{port}" if port is not None else ""
    return f"{parsed.scheme}://{host}{port_part}"


def resolve_cors_origins(default_origin: str | None = None) -> tuple[str, ...]:
    """返回去重后的 CORS 来源；默认只放行配置的前端来源。"""
    origins = [validate_origin(default_origin or default_frontend_origin())]
    raw = _env("TMF_CORS_ORIGINS")
    if raw:
        for item in raw.split(","):
            item = item.strip()
            if item:
                origins.append(validate_origin(item))
    return tuple(dict.fromkeys(origins))


@dataclass(frozen=True)
class NetworkSettings:
    """网关启动时一次性解析出的网络配置。"""

    bind_host: str
    gateway_host: str
    gateway_port: int
    frontend_host: str
    frontend_port: int
    frontend_origin: str
    cors_origins: tuple[str, ...]


def load_network_settings() -> NetworkSettings:
    """解析并校验全部网络环境变量；任一非法即抛 NetworkConfigError。"""
    bind_host = resolve_bind_host()
    gateway_host = resolve_gateway_host()
    gateway_port = resolve_gateway_port()
    frontend_host = resolve_frontend_host()
    frontend_port = resolve_frontend_port()
    origin = default_frontend_origin()
    return NetworkSettings(
        bind_host=bind_host,
        gateway_host=gateway_host,
        gateway_port=gateway_port,
        frontend_host=frontend_host,
        frontend_port=frontend_port,
        frontend_origin=origin,
        cors_origins=resolve_cors_origins(origin),
    )
