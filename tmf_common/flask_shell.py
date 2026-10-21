"""worker 侧薄 Flask 入口。

五个 ``api_flask_server.py`` 共用本模块，route 中不再包含模型加载、
预处理、白名单校验或历史错误格式——这些由 ``PredictService`` 负责。

保留的兼容契约（不得随意改动）：

    POST /predict_api  {"title": "..."}
      -> 200 {"title": "...", "pred_class": "..."}
      -> 200 {"title": "...", "error": "..."}        # 缺 title / 预测失败
    GET /healthz    -> {"status": "alive", "service": "<model_id>"}
    GET /readyz     -> {"status": "ready", "model_id": "<model_id>"}
    GET /metadata   -> {"model_id", "display_name", "version"}
"""

from __future__ import annotations

from typing import Any

from flask import Flask, jsonify, request

from tmf_common.config import env_port, resolve_bind_host
from tmf_common.prediction import PredictService


def create_app(service: PredictService, *, import_name: str = __name__) -> Flask:
    """根据统一预测服务构造 Flask app（不加载模型，模型由调用方注入）。"""
    if not isinstance(service, PredictService):
        raise TypeError("service 必须是 PredictService 实例")

    app = Flask(import_name)

    @app.post("/predict_api")
    def predict_api():
        # get_json 行为保持原样（非法 JSON 仍由 Flask 返回 400）。
        payload: Any = request.get_json()
        return jsonify(service.handle_payload(payload))

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "alive", "service": service.model_id})

    @app.get("/readyz")
    def readyz():
        if not service.is_ready():
            return jsonify({"status": "not_ready", "model_id": service.model_id}), 503
        return jsonify(service.ready_payload())

    @app.get("/metadata")
    def metadata():
        return jsonify(service.metadata())

    return app


def run_worker(
    app: Flask,
    service: PredictService,
    *,
    default_port: int,
    host: str | None = None,
) -> None:
    """按环境变量启动 worker（监听地址与端口可覆盖）。"""
    bind_host = host or resolve_bind_host()
    port = env_port(f"TMF_WORKER_PORT_{service.model_id.upper()}", default_port)
    app.run(host=bind_host, port=port, debug=False)
