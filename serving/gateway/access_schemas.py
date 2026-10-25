"""阶段 5B：访问管理 API 的响应模型。"""

from __future__ import annotations

from pydantic import BaseModel


class AccessStateResponse(BaseModel):
    request_id: str
    state: str
    # 只在 enter 响应中返回；status/claim/leave/cancel 不返回。
    ticket: str | None = None
    position: int | None = None
    ahead: int | None = None
    remaining_seconds: float | None = None
