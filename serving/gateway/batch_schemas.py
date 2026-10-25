"""阶段 2A：多标题批量分类（多条标题 × 单模型）请求/响应模型。

边界：
- 一个批次共用一个 ``model``；不支持每条各自指定模型；
- ``llm`` 由业务层在调用 worker 前拒绝，不能靠 schema/前端约束；
- 成功项与单条 ``ClassifyResponse`` 的核心分类语义对齐，但**不返回**
  ``human_feedback_hint``，避免 N 条触发 N 次相似反馈扫描；
- 失败项只带稳定错误结构，不伪造类别/分数。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from tmf_common.prediction import ScoreKind

from .schemas import ErrorInfo, TopKEntry


class BatchItemRequest(BaseModel):
    """批量请求中的一个标题条目。

    ``item_id`` 是客户端稳定映射键；``title`` 的 trim/长度/总字符校验由
    业务层完成，以便整批在调用任何 worker 前失败。
    """

    item_id: str
    title: str


class BatchClassifyRequest(BaseModel):
    """``POST /api/v1/classify/batch`` 请求体。"""

    #: 整批共用模型；是否允许 LLM 由业务层强制检查。
    model: str
    #: 可选客户端批次关联 ID；仅回显，不持久化。
    batch_id: str | None = None
    #: 条数/总字符/逐条合法性由业务层整批预检。
    items: list[BatchItemRequest]


class BatchSummary(BaseModel):
    total: int
    ok: int
    failed: int
    status: Literal["ok", "partial", "all_failed"]
    elapsed_ms: int
    total_timeout_ms: int


class BatchItemResult(BaseModel):
    """批量结果的一项：成功复用单条分类核心字段，失败只带安全错误。"""

    item_id: str
    index: int
    status: Literal["ok", "error"]
    pred_class: str | None = None
    latency_ms: int = 0
    model_version: str | None = None
    data_source: str | None = None
    scores: dict[str, float] | None = None
    score_kind: ScoreKind | None = None
    top_k: list[TopKEntry] | None = None
    rejected: bool = False
    reject_reason: str | None = None
    threshold_meta: dict[str, Any] | None = None
    #: 失败项：稳定错误码 + 安全 message + 可选结构化 details（不含标题）。
    error: ErrorInfo | None = None


class BatchClassifyResponse(BaseModel):
    """``POST /api/v1/classify/batch`` 成功/部分成功/全失败响应。"""

    request_id: str
    batch_id: str | None = None
    model_id: str
    summary: BatchSummary
    results: list[BatchItemResult] = Field(default_factory=list)
