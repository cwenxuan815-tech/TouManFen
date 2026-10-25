"""可信客户端 IP 解析（阶段 4A）。

边界：
- 默认只信任 TCP socket peer；不读取任何转发头；
- 仅当 socket peer 属于显式配置的可信代理 CIDR 时，才解析
  ``X-Forwarded-For``（优先）或 RFC 7239 ``Forwarded``；
- 转发链从右向左跳过可信代理，取第一个非可信地址作为客户端；
  左侧（更早的）地址可能是客户端伪造的，不能优先采用；
- 只返回归一化 IP 字符串；不记录/打印原始 IP；
- 不依赖 FastAPI、Starlette、日志或数据库，便于单测和后续 middleware 使用。
"""

from __future__ import annotations

from ipaddress import (
    IPv4Address,
    IPv4Network,
    IPv6Address,
    IPv6Network,
    ip_address,
    ip_network,
)
from typing import Mapping, Sequence

IPAddress = IPv4Address | IPv6Address
IPNetwork = IPv4Network | IPv6Network


class ClientIpError(ValueError):
    """客户端 IP 无法安全确定；调用方应拒绝请求（不得猜测身份）。"""


def parse_trusted_proxy_cidrs(raw: str | None) -> tuple[IPNetwork, ...]:
    """解析 ``TMF_TRUSTED_PROXY_CIDRS``，默认空，拒绝非法 CIDR 和 /0。"""
    if raw is None or not str(raw).strip():
        return ()
    networks: list[IPNetwork] = []
    seen: set[str] = set()
    for token in str(raw).split(","):
        value = token.strip()
        if not value:
            raise ClientIpError("可信代理 CIDR 列表包含空项")
        try:
            network = ip_network(value, strict=True)
        except ValueError as exc:
            raise ClientIpError("可信代理 CIDR 配置非法") from exc
        if network.prefixlen == 0:
            # 显式拒绝全网信任，防止配置失误导致任意转发头被信任。
            raise ClientIpError("可信代理 CIDR 不允许使用 /0")
        canonical = str(network)
        if canonical in seen:
            continue
        seen.add(canonical)
        networks.append(network)
    return tuple(networks)


def normalize_ip(value: str) -> str:
    """归一化 IPv4/IPv6；IPv4-mapped IPv6 转成 IPv4；剥离 zone id。"""
    if not isinstance(value, str):
        raise ClientIpError("IP 必须是字符串")
    text = value.strip()
    if not text:
        raise ClientIpError("IP 不能为空")
    if text.startswith("["):
        end = text.find("]")
        if end < 0:
            raise ClientIpError("IPv6 方括号不完整")
        inner = text[1:end]
        suffix = text[end + 1 :]
        if suffix and not (suffix.startswith(":") and suffix[1:].isdigit()):
            raise ClientIpError("IP 地址端口格式非法")
        text = inner
    elif "%" in text:
        # 例如 fe80::1%eth0；只取地址部分。
        text = text.split("%", 1)[0]

    try:
        address = ip_address(text)
    except ValueError:
        # 兼容 IPv4:port（X-Forwarded-For 有时带端口）；IPv6 必须加方括号。
        if text.count(":") == 1:
            host, port = text.rsplit(":", 1)
            if port.isdigit():
                try:
                    address = ip_address(host)
                except ValueError as exc:
                    raise ClientIpError("IP 地址格式非法") from exc
            else:
                raise ClientIpError("IP 地址格式非法")
        else:
            raise ClientIpError("IP 地址格式非法")

    if isinstance(address, IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return str(address)


def _header_value(headers: Mapping[str, str] | None, name: str) -> str | None:
    if headers is None:
        return None
    for key, value in headers.items():
        if str(key).lower() == name.lower():
            return None if value is None else str(value)
    return None


def _normalize_forwarded_token(value: str) -> str:
    token = value.strip()
    if not token:
        raise ClientIpError("转发链包含空地址")
    if token.startswith('"'):
        if not token.endswith('"') or len(token) < 2:
            raise ClientIpError("转发链引号格式非法")
        token = token[1:-1].strip()
    if token.lower() in {"unknown", "_hidden"}:
        raise ClientIpError("转发链包含不可用地址")
    return normalize_ip(token)


def _parse_x_forwarded_for(value: str) -> list[str]:
    raw = value.strip()
    if not raw:
        raise ClientIpError("X-Forwarded-For 为空")
    parts = [part.strip() for part in raw.split(",")]
    if any(not part for part in parts):
        raise ClientIpError("X-Forwarded-For 包含空地址")
    return [_normalize_forwarded_token(part) for part in parts]


def _parse_forwarded(value: str) -> list[str]:
    """解析 RFC 7239 ``Forwarded`` 的 ``for=`` 值，按出现顺序返回。"""
    raw = value.strip()
    if not raw:
        raise ClientIpError("Forwarded 为空")
    result: list[str] = []
    for element in raw.split(","):
        params = [part.strip() for part in element.split(";") if part.strip()]
        for param in params:
            key, separator, raw_value = param.partition("=")
            if not separator or key.strip().lower() != "for":
                continue
            result.append(_normalize_forwarded_token(raw_value.strip()))
            break
        else:
            raise ClientIpError("Forwarded 缺少 for 参数")
    if not result:
        raise ClientIpError("Forwarded 缺少 for 参数")
    return result


def _is_trusted(address: IPAddress, trusted_proxies: Sequence[IPNetwork]) -> bool:
    return any(address in network for network in trusted_proxies)


def resolve_client_ip(
    peer: str | None,
    headers: Mapping[str, str] | None = None,
    trusted_proxies: Sequence[IPNetwork] = (),
) -> str:
    """解析客户端 IP。

    - 未配置可信代理时，永远返回归一化后的 socket peer；
    - peer 不在可信代理列表时，任何转发头都被忽略；
    - peer 是可信代理时，必须存在明确转发链；从右向左取第一个非可信地址；
    - peer/链非法或无可信客户端地址时抛 ``ClientIpError``，不猜测。
    """
    if peer is None or not str(peer).strip():
        raise ClientIpError("缺少 socket peer 地址")
    try:
        peer_text = normalize_ip(str(peer))
        peer_address = ip_address(peer_text)
    except ClientIpError:
        raise ClientIpError("socket peer 地址非法") from None
    except ValueError:
        raise ClientIpError("socket peer 地址非法") from None

    trusted = tuple(trusted_proxies)
    if not trusted or not _is_trusted(peer_address, trusted):
        return peer_text

    forwarded_for = _header_value(headers, "x-forwarded-for")
    forwarded = _header_value(headers, "forwarded")
    if forwarded_for is not None and forwarded_for.strip():
        chain = _parse_x_forwarded_for(forwarded_for)
    elif forwarded is not None and forwarded.strip():
        chain = _parse_forwarded(forwarded)
    else:
        raise ClientIpError("可信代理未提供客户端转发身份")

    current = peer_address
    for candidate_text in reversed(chain):
        if not _is_trusted(current, trusted):
            break
        try:
            current = ip_address(candidate_text)
        except ValueError as exc:
            raise ClientIpError("转发链包含非法地址") from exc
    if _is_trusted(current, trusted):
        raise ClientIpError("可信代理链没有非可信客户端地址")
    return normalize_ip(str(current))
