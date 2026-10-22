"""反馈（B4）请求/响应 Pydantic 模型。

放在 ``gateway/routers/`` 下、与 ``gateway/schemas.py`` 分离，避免与 B1/F3
正在修改的公共 schema 冲突；``/classify`` 的 ``human_feedback_hint`` 可选字段
仍按清单要求追加在 ``gateway/schemas.py``。

契约要点：
- ``POST /feedback`` 成功只表示「已记录，待审核」，``status`` 固定 ``pending``；
- ``GET /feedback/stats`` 只返回聚合，不提供原始数据导出；
- 文案红线：禁止「已学会」「已重新训练」。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class FeedbackSubmitRequest(BaseModel):
    client_id: str
    title: str
    model_id: str
    pred_class: str
    corrected_class: str
    #: 可选；写入前校验为有限数值且位于 [0, 1]。
    score: float | None = None
    #: 可选短安全字符串，默认 "api"（校验 1~32 位字母/数字/._-）。
    source: str | None = None


class FeedbackSubmitResponse(BaseModel):
    request_id: str
    feedback_id: int
    status: str
    total: int
    message: str


class FeedbackStatsResponse(BaseModel):
    request_id: str
    total: int
    pending: int
    by_class: dict[str, int] = Field(default_factory=dict)
    by_model: dict[str, int] = Field(default_factory=dict)
    #: 至少存在一条相似 pending 反馈的 pending 行数（聚合，不含标题）。
    similar_hits: int = 0
    retention_days: int
