#!/usr/bin/env python
"""阶段 4B 收口验收：stage2 的 Uvicorn 不提前信任代理头。

验证：
1. stage2.ps1 的 Start-Gateway 实际参数包含 --no-proxy-headers，且没有冲突的
   --proxy-headers / --forwarded-allow-ips；
2. 用临时 loopback 端口启动临时 Uvicorn app（不启动用户网关，不占用 8000/
   8101-8106），以与 stage2 相同的 Uvicorn 参数运行；
3. 无可信代理时，伪造 X-Forwarded-For / Forwarded 不改变客户端身份；
4. 显式、窄范围可信代理（127.0.0.1/32）时，client_ip.py 按既定链规则取客户端；
5. 全可信链/非法身份仍 fail-closed。

不读取/输出 HMAC key、Provider key；不调用真实 LLM。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STAGE2 = PROJECT_ROOT / "serving" / "scripts" / "stage2.ps1"
sys.path.insert(0, str(PROJECT_ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from serving.gateway.client_ip import (  # noqa: E402
    resolve_client_ip,
    parse_trusted_proxy_cidrs,
)

FAILURES: list[str] = []
TOTAL = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global TOTAL
    TOTAL += 1
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


def stage2_gateway_arguments() -> str:
    text = STAGE2.read_text(encoding="utf-8")
    match = re.search(
        r"\$arguments\s*=\s*@\((.*?)\n\s*\)",
        text,
        flags=re.DOTALL,
    )
    if not match:
        raise AssertionError("无法定位 stage2.ps1 的 Start-Gateway $arguments 块")
    return match.group(1)


def free_loopback_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


PROBE_APP = r'''
from __future__ import annotations

import json
import os

from serving.gateway.client_ip import ClientIpError, parse_trusted_proxy_cidrs, resolve_client_ip

TRUSTED = parse_trusted_proxy_cidrs(os.environ.get("PROBE_TRUSTED_CIDRS", ""))


async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return
        return

    if scope["type"] != "http":
        return

    headers = {}
    for key, value in scope.get("headers", []):
        name = key.decode("latin1").lower()
        text = value.decode("latin1")
        headers[name] = f"{headers[name]}, {text}" if name in headers else text

    client = scope.get("client")
    peer = client[0] if client else None
    resolved = None
    error = None
    try:
        resolved = resolve_client_ip(peer, headers, TRUSTED)
    except ClientIpError as exc:
        error = str(exc)

    body = json.dumps(
        {"peer": peer, "client_ip": resolved, "error": error},
        ensure_ascii=False,
    ).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"content-type", b"application/json; charset=utf-8")],
        }
    )
    await send({"type": "http.response.body", "body": body})
'''


def start_probe(root: Path, trusted_cidrs: str) -> tuple[subprocess.Popen, str]:
    app_path = root / "probe_app.py"
    if not app_path.exists():
        app_path.write_text(PROBE_APP, encoding="utf-8")
    port = free_loopback_port()
    if port in {8000, 8101, 8103, 8104, 8105, 8106}:
        raise AssertionError(f"临时端口意外命中保留端口: {port}")
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        [str(PROJECT_ROOT), env.get("PYTHONPATH", "")]
    )
    if trusted_cidrs:
        env["PROBE_TRUSTED_CIDRS"] = trusted_cidrs
    else:
        env.pop("PROBE_TRUSTED_CIDRS", None)
    # 与 stage2.ps1 的网关启动参数保持一致，只替换 app/host/port。
    command = [
        sys.executable,
        "-m",
        "uvicorn",
        "probe_app:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--no-access-log",
        "--no-proxy-headers",
        "--log-level",
        "info",
    ]
    process = subprocess.Popen(
        command,
        cwd=str(root),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 15.0
    with httpx.Client(trust_env=False, timeout=2.0) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError(f"临时 Uvicorn 提前退出 rc={process.returncode}")
            try:
                response = client.get(f"{base_url}/healthz")
                if response.status_code == 200:
                    return process, base_url
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    process.terminate()
    raise AssertionError("临时 Uvicorn 启动超时")


def stop_probe(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def get_probe(base_url: str, headers: dict[str, str]) -> dict:
    with httpx.Client(trust_env=False, timeout=5.0) as client:
        response = client.get(f"{base_url}/identity", headers=headers)
        response.raise_for_status()
        return response.json()


def test_stage2_arguments() -> None:
    print("\n=== 1. stage2.ps1 启动参数 ===")
    block = stage2_gateway_arguments()
    check("stage2 gateway includes --no-proxy-headers", "'--no-proxy-headers'" in block)
    check("stage2 gateway keeps --no-access-log", "'--no-access-log'" in block)
    check(
        "stage2 gateway has no conflicting --proxy-headers",
        "'--proxy-headers'" not in block,
    )
    check(
        "stage2 gateway has no conflicting --forwarded-allow-ips",
        "'--forwarded-allow-ips'" not in block,
    )
    print(f"   stage2 gateway args: {' '.join(block.split())}")


def run_default_probe() -> None:
    print("\n=== 2. 默认无可信代理：Uvicorn 不提前信任转发头 ===")
    with tempfile.TemporaryDirectory(prefix="tmf-uvicorn-default-") as raw:
        process, base_url = start_probe(Path(raw), trusted_cidrs="")
        try:
            direct = get_probe(base_url, {})
            spoofed_xff = get_probe(
                base_url, {"X-Forwarded-For": "203.0.113.99"}
            )
            spoofed_forwarded = get_probe(
                base_url, {"Forwarded": "for=203.0.113.99;proto=http"}
            )
            both = get_probe(
                base_url,
                {
                    "X-Forwarded-For": "203.0.113.99",
                    "Forwarded": "for=203.0.113.99;proto=http",
                },
            )
            check("direct socket peer is 127.0.0.1", direct.get("client_ip") == "127.0.0.1", str(direct))
            check("forged XFF does not change identity", spoofed_xff.get("client_ip") == "127.0.0.1", str(spoofed_xff))
            check(
                "forged Forwarded does not change identity",
                spoofed_forwarded.get("client_ip") == "127.0.0.1",
                str(spoofed_forwarded),
            )
            check("forged both headers do not change identity", both.get("client_ip") == "127.0.0.1", str(both))
            check(
                "peer returned by Uvicorn is real socket peer",
                direct.get("peer") == "127.0.0.1",
                str(direct.get("peer")),
            )
        finally:
            stop_probe(process)


def run_trusted_probe() -> None:
    print("\n=== 3. 显式可信代理 127.0.0.1/32：按应用层链规则解析 ===")
    with tempfile.TemporaryDirectory(prefix="tmf-uvicorn-trusted-") as raw:
        process, base_url = start_probe(
            Path(raw),
            trusted_cidrs="127.0.0.1/32",
        )
        try:
            xff = get_probe(
                base_url,
                {"X-Forwarded-For": "192.0.2.9, 203.0.113.10, 127.0.0.1"},
            )
            forwarded = get_probe(
                base_url,
                {"Forwarded": "for=192.0.2.9, for=203.0.113.10, for=127.0.0.1"},
            )
            all_trusted = get_probe(
                base_url,
                {"X-Forwarded-For": "127.0.0.1"},
            )
            check(
                "trusted proxy XFF picks rightmost untrusted client",
                xff.get("client_ip") == "203.0.113.10",
                str(xff),
            )
            check(
                "trusted proxy Forwarded picks rightmost untrusted client",
                forwarded.get("client_ip") == "203.0.113.10",
                str(forwarded),
            )
            check(
                "all-trusted chain fails closed",
                all_trusted.get("client_ip") is None and all_trusted.get("error"),
                str(all_trusted),
            )
        finally:
            stop_probe(process)


def test_resolver_unit() -> None:
    print("\n=== 4. 应用层解析器纯函数回归 ===")
    trusted = parse_trusted_proxy_cidrs("127.0.0.1/32")
    check(
        "trusted chain keeps rightmost untrusted",
        resolve_client_ip(
            "127.0.0.1",
            {"X-Forwarded-For": "192.0.2.9, 203.0.113.10, 127.0.0.1"},
            trusted,
        )
        == "203.0.113.10",
    )
    check(
        "untrusted peer ignores forged XFF",
        resolve_client_ip(
            "203.0.113.50",
            {"X-Forwarded-For": "203.0.113.99"},
            trusted,
        )
        == "203.0.113.50",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-probe", action="store_true")
    args = parser.parse_args()

    print(f"Uvicorn version: {uvicorn.__version__}")
    test_stage2_arguments()
    test_resolver_unit()
    if not args.skip_probe:
        run_default_probe()
        run_trusted_probe()
    if FAILURES:
        print(f"\nRESULT FAIL ({len(FAILURES)} failed / {TOTAL} checks)")
        for name in FAILURES:
            print(f"  FAILED: {name}")
        return 1
    print(f"\nRESULT PASS ({TOTAL} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
