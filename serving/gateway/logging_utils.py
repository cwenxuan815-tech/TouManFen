"""结构化 JSON 日志（阶段 2）。

只写服务端文件；不记录新闻标题、密钥、完整环境变量或请求体。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "event": getattr(record, "event", "log"),
            "message": record.getMessage(),
        }
        for key in (
            "request_id",
            "method",
            "path",
            "route",
            "status",
            "duration_ms",
            "model_id",
            "worker_id",
            "model_status",
            "error_code",
            "data_source",
            "stats_counted",
            # 阶段 2A：批量分类关联/汇总字段（不记录标题）。
            "batch_id",
            "batch_total",
            "batch_ok",
            "batch_failed",
            "batch_status",
            "item_id",
        ):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            # 堆栈仅写服务端日志，不返回给调用方
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("tmf.gateway")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger
    handler = RotatingFileHandler(
        str(log_file), maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)
    return logger


def log_startup_error(message: str) -> None:
    """配置错误等启动期错误输出到 stderr，由启动脚本捕获并定位。"""
    sys.stderr.write(message.rstrip() + "\n")
    sys.stderr.flush()
