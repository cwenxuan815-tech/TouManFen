"""B8 离线基准（PK 榜）HTTP 响应模型。

契约（F11 消费）：

``GET /api/v1/bench/pk`` → ``BenchPkResponse``::

    {
      "request_id": "...",
      "artifact_path": "artifacts/bench/pk_2026-10-22.json",  # 工程根相对路径
      "artifact": { ...tools/bench_signature.py 产出的完整 JSON... }
    }

``artifact`` 结构见 ``tools/bench_signature.py``：``meta``（§6.8）、
``dataset``、``models``、``derived``、``missing_models``、``reproduction``、
``run`` 等。这里不把 artifact 拆成强类型字段，避免把离线产物演进绑死在
网关 schema 上；F11 只需按 ``meta`` / ``models`` / ``derived`` 读取。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class BenchPkResponse(BaseModel):
    """离线基准最新产物（只读）。"""

    request_id: str
    #: 工程根相对路径，例如 ``artifacts/bench/pk_2026-10-22.json``；
    #: 不含绝对路径，且只可能是 bench 目录下的 ``pk_*.json``。
    artifact_path: str = Field(
        description="工程根相对路径，只可能是 artifacts/bench/pk_*.json"
    )
    #: 产物完整内容（含 §6.8 meta / 模型指标 / 派生指标 / 复现信息）。
    artifact: dict[str, Any]
