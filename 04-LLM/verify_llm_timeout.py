#!/usr/bin/env python
"""阶段 4A 离线验证：LLM worker 上游超时 / 有限重试 / 错误映射。

完全离线：
- 使用本地 mock OpenAI 兼容 HTTP 服务（127.0.0.1 随机端口）；
- 服务代码复制到临时目录运行，临时目录无 .env，避免读取真实密钥；
- 不访问 DeepSeek/Qwen/智谱，不验证真实密钥，不产生计费请求。

运行：
    D:\\Miniconda3\\envs\\py3109\\python.exe 04-LLM\\verify_llm_timeout.py
"""

from __future__ import annotations

import importlib
import inspect
import json
import logging
import os
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import openai

MODULE_DIR = Path(__file__).resolve().parent
SERVICE_FILE = MODULE_DIR / "llm_predict_service.py"
FLASK_FILE = MODULE_DIR / "api_flask_server.py"
WORKERS_YAML = MODULE_DIR.parent / "serving" / "config" / "workers.yaml"
FAKE_KEY = "local-test-key-not-real"
FAILURES: list[str] = []
CALLS: dict[str, int] = {}
CALLS_LOCK = threading.Lock()
LOG_MESSAGES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        print(f"FAIL {name} {detail}".rstrip())
        FAILURES.append(name)


class _CaptureHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        LOG_MESSAGES.append(record.getMessage())


class MockUpstreamHandler(BaseHTTPRequestHandler):
    server_version = "TMFMockUpstream/1.0"

    def log_message(self, fmt: str, *args) -> None:
        # 只记录路径，不记录请求体/标题/Authorization
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

    def _chat_completion(self, content: str) -> dict:
        return {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "test-model",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": "not found"}})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length else b"{}"
            payload = json.loads(raw.decode("utf-8"))
            messages = payload.get("messages", [])
            title = ""
            for message in reversed(messages):
                if message.get("role") == "user":
                    title = message.get("content", "")
                    break
        except Exception:
            title = ""

        with CALLS_LOCK:
            CALLS[title] = CALLS.get(title, 0) + 1
            count = CALLS[title]

        if title == "stage4a-http-500":
            self._send_json(500, {"error": {"message": "mock upstream error", "type": "server_error"}})
            return
        if title == "stage4a-slow":
            time.sleep(5)
            self._send_json(200, self._chat_completion("sports"))
            return
        if title == "stage4a-budget-exhaust":
            time.sleep(1.85)
            self._send_json(200, self._chat_completion("bogus"))
            return
        if title == "stage4a-retry":
            content = "bogus" if count == 1 else "sports"
            self._send_json(200, self._chat_completion(content))
            return
        if title == "stage4a-invalid-twice":
            self._send_json(200, self._chat_completion("bogus"))
            return
        self._send_json(200, self._chat_completion("sports"))


class MockUpstreamServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(
            exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)
        ):
            return
        super().handle_error(request, client_address)


def free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def start_abort_server() -> int:
    """接受一个连接后立即关闭，用于验证连接失败映射。"""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = int(listener.getsockname()[1])

    def accept_and_close() -> None:
        try:
            connection, _ = listener.accept()
            connection.close()
        finally:
            listener.close()

    threading.Thread(target=accept_and_close, daemon=True).start()
    return port


def start_mock_server() -> MockUpstreamServer:
    server = MockUpstreamServer(("127.0.0.1", 0), MockUpstreamHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def reset_env() -> None:
    for name in (
        "TMF_LLM_PROVIDER",
        "TMF_LLM_TOTAL_TIMEOUT_S",
        "DEEPSEEK_API_KEY",
        "DEEPSEEK_BASE_URL",
        "DEEPSEEK_MODEL",
        "QWEN_API_KEY",
        "QWEN_BASE_URL",
        "QWEN_MODEL",
        "ZHIPU_API_KEY",
        "ZHIPU_BASE_URL",
        "ZHIPU_MODEL",
    ):
        os.environ.pop(name, None)


def load_service(temp_dir: str, provider: str, base_url: str, total_timeout: str | None):
    os.environ["TMF_LLM_PROVIDER"] = provider
    key_name, base_name, model_name, _ = {
        "deepseek": ("DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "deepseek-chat"),
        "qwen": ("QWEN_API_KEY", "QWEN_BASE_URL", "QWEN_MODEL", "qwen-plus"),
        "zhipu": ("ZHIPU_API_KEY", "ZHIPU_BASE_URL", "ZHIPU_MODEL", "glm-4-flash"),
    }[provider]
    os.environ[key_name] = FAKE_KEY
    os.environ[base_name] = base_url
    os.environ[model_name] = "test-model"
    if total_timeout is None:
        os.environ.pop("TMF_LLM_TOTAL_TIMEOUT_S", None)
    else:
        os.environ["TMF_LLM_TOTAL_TIMEOUT_S"] = total_timeout
    return service_namespace(temp_dir)


def service_namespace(temp_dir: str):
    import runpy

    return runpy.run_path(str(Path(temp_dir) / "llm_predict_service.py"))


def gateway_llm_timeout_ms() -> int | None:
    if not WORKERS_YAML.is_file():
        return None
    in_llm = False
    for line in WORKERS_YAML.read_text(encoding="utf-8").splitlines():
        if line.strip() == "llm:":
            in_llm = True
            continue
        if in_llm:
            if line and not line.startswith("    "):
                break
            match = re.match(r"\s+timeout_ms:\s*(\d+)", line)
            if match:
                return int(match.group(1))
    return None


def main() -> int:
    # 仅测试进程：本地 mock 流量绕过系统代理；不修改生产/系统代理设置。
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"

    print("--- SDK static behavior ---")
    print("openai", openai.__version__)
    print("DEFAULT_TIMEOUT", openai.DEFAULT_TIMEOUT)
    print("DEFAULT_MAX_RETRIES", openai.DEFAULT_MAX_RETRIES)
    from openai.resources.chat.completions import Completions

    create_sig = inspect.signature(Completions.create)
    print("create accepts timeout", "timeout" in create_sig.parameters)
    print("create accepts max_retries", "max_retries" in create_sig.parameters)
    print("client supports with_options", hasattr(openai.OpenAI, "with_options"))

    logger = logging.getLogger("tmf.llm")
    logger.setLevel(logging.INFO)
    handler = _CaptureHandler()
    logger.addHandler(handler)

    temp_dir = tempfile.mkdtemp(prefix="tmf-stage4a-")
    shutil.copyfile(SERVICE_FILE, Path(temp_dir) / "llm_predict_service.py")
    shutil.copyfile(FLASK_FILE, Path(temp_dir) / "api_flask_server.py")
    server = start_mock_server()
    base_url = f"http://127.0.0.1:{server.server_address[1]}/v1"

    try:
        # 1) 默认配置
        reset_env()
        namespace = load_service(temp_dir, "deepseek", base_url, None)
        total_timeout = namespace["TOTAL_TIMEOUT_S"]
        check("default total timeout 12s", total_timeout == 12.0, str(total_timeout))
        check("default below gateway timeout", total_timeout * 1000 < (gateway_llm_timeout_ms() or 0))
        check("SDK retries disabled", namespace["llm_client"].max_retries == 0)

        # 2) 非法配置
        for invalid in ("0", "abc", "15.0"):
            reset_env()
            try:
                load_service(temp_dir, "deepseek", base_url, invalid)
                check(f"invalid timeout rejected ({invalid})", False)
            except Exception as exc:
                message = str(exc)
                check(
                    f"invalid timeout rejected ({invalid})",
                    "TMF_LLM_TOTAL_TIMEOUT_S" in message and FAKE_KEY not in message,
                )

        # 3) 缺少 key（临时目录无 .env）
        reset_env()
        os.environ["TMF_LLM_PROVIDER"] = "deepseek"
        try:
            service_namespace(temp_dir)
            check("missing key rejected", False)
        except Exception as exc:
            message = str(exc)
            check(
                "missing key rejected",
                "DEEPSEEK_API_KEY" in message and FAKE_KEY not in message,
            )

        # 4) 动态场景：统一总预算 2s
        reset_env()
        namespace = load_service(temp_dir, "deepseek", base_url, "2.0")
        predict = namespace["predict"]

        result = predict({"title": "stage4a-success"})
        check(
            "success returns legal class",
            result.get("pred_class") == "sports" and "error" not in result,
            str(result),
        )
        check("success one upstream call", CALLS.get("stage4a-success") == 1)

        result = predict({"title": "stage4a-retry"})
        check(
            "format retry succeeds",
            result.get("pred_class") == "sports" and CALLS.get("stage4a-retry") == 2,
            f"calls={CALLS.get('stage4a-retry')} result={result.get('pred_class')}",
        )

        result = predict({"title": "stage4a-invalid-twice"})
        check(
            "invalid output twice fails safely",
            result.get("pred_class") == ""
            and "LLM_INVALID_OUTPUT" in result.get("error", "")
            and CALLS.get("stage4a-invalid-twice") == 2
            and "bogus" not in result.get("error", ""),
            f"calls={CALLS.get('stage4a-invalid-twice')}",
        )

        result = predict({"title": "stage4a-http-500"})
        check(
            "HTTP 500 maps upstream error",
            result.get("pred_class") == ""
            and "LLM_UPSTREAM_ERROR" in result.get("error", "")
            and CALLS.get("stage4a-http-500") == 1,
            f"calls={CALLS.get('stage4a-http-500')}",
        )

        started = time.monotonic()
        result = predict({"title": "stage4a-slow"})
        elapsed = time.monotonic() - started
        check(
            "slow upstream bounded by total timeout",
            result.get("pred_class") == ""
            and "LLM_TIMEOUT" in result.get("error", "")
            and CALLS.get("stage4a-slow") == 1
            and elapsed < 4.0,
            f"calls={CALLS.get('stage4a-slow')} elapsed={elapsed:.2f}",
        )

        started = time.monotonic()
        result = predict({"title": "stage4a-budget-exhaust"})
        elapsed = time.monotonic() - started
        check(
            "exhausted budget skips format retry",
            result.get("pred_class") == ""
            and "LLM_TIMEOUT" in result.get("error", "")
            and CALLS.get("stage4a-budget-exhaust") == 1
            and elapsed < 3.0,
            f"calls={CALLS.get('stage4a-budget-exhaust')} elapsed={elapsed:.2f}",
        )

        # 5) 连接失败（本机接受连接后立即断开）
        reset_env()
        abort_port = start_abort_server()
        namespace = load_service(
            temp_dir, "deepseek", f"http://127.0.0.1:{abort_port}/v1", "2.0"
        )
        result = namespace["predict"]({"title": "stage4a-connection-fail"})
        check(
            "connection failure maps upstream error",
            result.get("pred_class") == ""
            and "LLM_UPSTREAM_ERROR" in result.get("error", ""),
            result.get("error", ""),
        )

        # 6) provider 选择仍可用（全部指向本地 mock）
        for provider in ("deepseek", "qwen", "zhipu"):
            reset_env()
            namespace = load_service(temp_dir, provider, base_url, "2.0")
            result = namespace["predict"]({"title": "stage4a-success"})
            check(
                f"provider {provider} path works",
                namespace["PROVIDER"] == provider
                and result.get("pred_class") == "sports",
            )

        # 7) /predict_api 兼容性（Flask test client；临时目录无 .env）
        reset_env()
        os.environ["TMF_LLM_PROVIDER"] = "deepseek"
        os.environ["DEEPSEEK_API_KEY"] = FAKE_KEY
        os.environ["DEEPSEEK_BASE_URL"] = base_url
        os.environ["DEEPSEEK_MODEL"] = "test-model"
        os.environ["TMF_LLM_TOTAL_TIMEOUT_S"] = "2.0"
        sys.path.insert(0, temp_dir)
        try:
            flask_module = importlib.import_module("api_flask_server")
            client = flask_module.app.test_client()
            response = client.post("/predict_api", json={"title": "stage4a-success"})
            body = response.get_json()
            check(
                "predict_api success shape",
                response.status_code == 200
                and body.get("pred_class") == "sports"
                and body.get("title") == "stage4a-success",
                str(body),
            )
            response = client.post("/predict_api", json={"title": "stage4a-http-500"})
            body = response.get_json()
            check(
                "predict_api error shape",
                response.status_code == 200
                and body.get("pred_class") == ""
                and "LLM_UPSTREAM_ERROR" in body.get("error", ""),
                str(body),
            )
        finally:
            sys.path.remove(temp_dir)
            for module_name in ("api_flask_server", "llm_predict_service"):
                sys.modules.pop(module_name, None)

        # 8) 日志与响应敏感信息检查
        joined_logs = "\n".join(LOG_MESSAGES)
        for forbidden in (FAKE_KEY, "stage4a-", "北京", "沪指", "Authorization", "Traceback"):
            check(f"logs exclude {forbidden}", forbidden not in joined_logs)

        all_results = [
            predict({"title": "stage4a-success"}),
            predict({"title": "stage4a-invalid-twice"}),
        ]
        joined_responses = json.dumps(all_results, ensure_ascii=False)
        for forbidden in (FAKE_KEY, "Traceback", "Authorization"):
            check(f"responses exclude {forbidden}", forbidden not in joined_responses)
        check(
            "invalid output raw text not exposed",
            "bogus" not in all_results[1].get("error", ""),
        )

        check(
            "no real provider base_url",
            all(
                not url.startswith(("https://api.deepseek", "https://dashscope", "https://open.bigmodel"))
                for url in [base_url]
            ),
        )
    finally:
        server.shutdown()
        server.server_close()
        logger.removeHandler(handler)
        shutil.rmtree(temp_dir, ignore_errors=True)

    if FAILURES:
        print(f"RESULT FAIL ({len(FAILURES)}): " + ", ".join(FAILURES))
        return 1
    print("RESULT PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
