#!/usr/bin/env python
"""阶段 3 验证用 mock LLM worker（仅 stdlib）。

用途：在 8105 端口模拟 LLM worker，验证网关对 LLM 的成功/错误/超时映射。
不访问任何真实 LLM API，不读取密钥。

按标题前缀选择行为（标题仅用于模式选择，不写入日志）：
- MOCK_OK / 默认           -> 200 {"pred_class": "sports"}
- MOCK_ERROR               -> 200 {"pred_class": "", "error": "mock error"}
- MOCK_MISSING_CLASS       -> 200 {"title": "..."}
- MOCK_INVALID_CLASS       -> 200 {"pred_class": "not_a_class"}
- MOCK_EMPTY_CLASS         -> 200 {"pred_class": ""}
- MOCK_INVALID_JSON        -> 200 text/plain "not-json"
- MOCK_HTTP_500            -> 500 {"error": "mock 500"}
- MOCK_SLOW                -> sleep 16s 后返回成功（触发网关 15s 超时）
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


class MockHandler(BaseHTTPRequestHandler):
    server_version = "TMFMockLLM/1.0"

    def log_message(self, fmt: str, *args) -> None:
        # 只记录 HTTP 方法与路径，不记录请求体/标题
        sys.stdout.write(f"{self.command} {self.path}\n")
        sys.stdout.flush()

    def _send_json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_text(self, status: int, text: str) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            self._send_json(200, {"status": "alive", "service": "llm-mock"})
        elif path == "/readyz":
            self._send_json(200, {"status": "ready", "model_id": "llm"})
        elif path == "/metadata":
            self._send_json(
                200,
                {"model_id": "llm", "display_name": "mock-llm", "version": None},
            )
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/predict_api":
            self._send_json(404, {"error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            title = payload.get("title", "") if isinstance(payload, dict) else ""
        except Exception:
            title = ""

        if title == "MOCK_SLOW":
            time.sleep(16)
            self._send_json(200, {"pred_class": "sports"})
            return
        if title == "MOCK_ERROR":
            self._send_json(200, {"pred_class": "", "error": "mock error"})
            return
        if title == "MOCK_MISSING_CLASS":
            self._send_json(200, {"title": title})
            return
        if title == "MOCK_INVALID_CLASS":
            self._send_json(200, {"pred_class": "not_a_class"})
            return
        if title == "MOCK_EMPTY_CLASS":
            self._send_json(200, {"pred_class": ""})
            return
        if title == "MOCK_INVALID_JSON":
            self._send_text(200, "not-json")
            return
        if title == "MOCK_HTTP_500":
            self._send_json(500, {"error": "mock 500"})
            return
        self._send_json(200, {"pred_class": "sports"})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.12.54")
    parser.add_argument("--port", type=int, default=8105)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), MockHandler)
    print(f"mock llm worker listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
