#!/usr/bin/env python
"""通用 mock worker（仅 stdlib）。

用途：
1. LLM mock：替代 8105，验证网关对 LLM 的成功/错误/超时映射，不访问真实 Provider；
2. 其余模型的轻量 mock：在没有加载真实模型的环境里验证网关适配层与 API 契约。

按标题选择行为（仅在加 ``--special-titles`` 时启用；标题仅用于模式选择，不写入日志）：
- 默认 / MOCK_OK          -> 200 {"title": ..., "pred_class": <class>}
- MOCK_ERROR              -> 200 {"pred_class": "", "error": "mock error"}
- MOCK_MISSING_CLASS      -> 200 {"title": ...}
- MOCK_INVALID_CLASS      -> 200 {"pred_class": "not_a_class"}
- MOCK_EMPTY_CLASS        -> 200 {"pred_class": ""}
- MOCK_INVALID_JSON       -> 200 text/plain "not-json"
- MOCK_HTTP_500           -> 500 {"error": "mock 500"}
- MOCK_SLOW               -> sleep 16s 后返回成功（触发网关超时）

示例：
    python serving/scripts/mock_worker.py --port 8105 --model-id llm --special-titles
    python serving/scripts/mock_worker.py --port 8101 --model-id rf --display-name "mock-rf"
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from tmf_common.config import validate_host  # noqa: E402


class MockHandler(BaseHTTPRequestHandler):
    server_version = "TMFMock/1.0"
    model_id = "llm"
    display_name = "mock-llm"
    pred_class = "sports"
    special_titles = False
    #: 仅供验收断言「默认请求是否真的没有调用 LLM」；不参与业务逻辑。
    predict_requests = 0

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
            self._send_json(200, {"status": "alive", "service": self.model_id})
        elif path == "/readyz":
            self._send_json(200, {"status": "ready", "model_id": self.model_id})
        elif path == "/metadata":
            self._send_json(
                200,
                {
                    "model_id": self.model_id,
                    "display_name": self.display_name,
                    "version": None,
                },
            )
        elif path == "/mock/stats":
            # 验收辅助（只读）：统计 /predict_api 调用次数，用于断言默认 compare
            # 不触发 LLM；不返回标题/请求体，也不影响真实 worker 契约。
            self._send_json(
                200,
                {
                    "model_id": self.model_id,
                    "display_name": self.display_name,
                    "predict_requests": type(self).predict_requests,
                },
            )
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path != "/predict_api":
            self._send_json(404, {"error": "not found"})
            return

        type(self).predict_requests += 1

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            title = payload.get("title", "") if isinstance(payload, dict) else ""
        except Exception:
            title = ""

        if self.special_titles and title == "MOCK_SLOW":
            time.sleep(16)
            self._send_json(200, {"title": title, "pred_class": self.pred_class})
            return
        if self.special_titles and title == "MOCK_ERROR":
            self._send_json(200, {"pred_class": "", "error": "mock error"})
            return
        if self.special_titles and title == "MOCK_MISSING_CLASS":
            self._send_json(200, {"title": title})
            return
        if self.special_titles and title == "MOCK_INVALID_CLASS":
            self._send_json(200, {"title": title, "pred_class": "not_a_class"})
            return
        if self.special_titles and title == "MOCK_EMPTY_CLASS":
            self._send_json(200, {"title": title, "pred_class": ""})
            return
        if self.special_titles and title == "MOCK_INVALID_JSON":
            self._send_text(200, "not-json")
            return
        if self.special_titles and title == "MOCK_HTTP_500":
            self._send_json(500, {"error": "mock 500"})
            return
        self._send_json(200, {"title": title, "pred_class": self.pred_class})


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8105)
    parser.add_argument("--model-id", default="llm")
    parser.add_argument("--display-name", default="mock-llm")
    parser.add_argument("--class", dest="pred_class", default="sports")
    parser.add_argument(
        "--special-titles",
        action="store_true",
        help="启用 MOCK_* 特殊标题（LLM 错误/超时验证需要）",
    )
    args = parser.parse_args()

    try:
        host = validate_host(args.host, variable="--host")
    except ValueError as exc:
        parser.error(str(exc))
    if not 1 <= args.port <= 65535:
        parser.error("--port 必须在 1~65535 之间")

    MockHandler.model_id = args.model_id
    MockHandler.display_name = args.display_name
    MockHandler.pred_class = args.pred_class
    MockHandler.special_titles = args.special_titles

    server = ThreadingHTTPServer((host, args.port), MockHandler)
    print(
        f"mock worker listening on http://{host}:{args.port} "
        f"(model_id={args.model_id}, display_name={args.display_name})",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
