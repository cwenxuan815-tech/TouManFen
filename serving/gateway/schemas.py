"""阶段 3 网关请求/响应模型。

阶段 2 只读接口保持不变；新增 /classify、/compare 请求响应模型。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from tmf_common.prediction import ScoreKind


class ErrorInfo(BaseModel):
    code: str
    message: str
    details: dict[str, Any] | None = None


class ErrorResponse(BaseModel):
    request_id: str
    error: ErrorInfo


class HealthResponse(BaseModel):
    request_id: str
    status: str
    service: str
    version: str


class WorkerState(BaseModel):
    worker_id: str
    status: str
    latency_ms: int | None = None
    error: ErrorInfo | None = None


class ReadyResponse(BaseModel):
    request_id: str
    status: str
    workers: list[WorkerState]


class ModelState(BaseModel):
    model_id: str
    display_name: str
    enabled: bool
    worker_id: str
    model_version: str | None = None
    status: str


class ModelsResponse(BaseModel):
    request_id: str
    default_model: str
    classes: list[str]
    models: list[ModelState]


class RouteStats(BaseModel):
    count: int
    avg_ms: float
    max_ms: float
    latency_buckets: list[dict[str, Any]]


class StatsResponse(BaseModel):
    request_id: str
    process_local: bool
    note: str
    since: str
    uptime_s: float
    total_requests: int
    by_status: dict[str, int]
    routes: dict[str, RouteStats]
    model_calls: dict[str, dict[str, int]] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 阶段 3：分类请求/响应
# ---------------------------------------------------------------------------
class ClassifyRequest(BaseModel):
    title: str
    model: str | None = None


class TopKEntry(BaseModel):
    """top-k 的一个类别/分数配对（分数降序，并列按类别名升序）。"""

    class_id: str
    score: float


class HumanFeedbackHint(BaseModel):
    """相似历史反馈的参考提示（B4，未审核）。

    仅用于并列展示「人工反馈参考（未审核）」；不得据此改写 ``pred_class``，
    不包含反馈原文、反馈 ID 或任何用户字段。
    """

    corrected_class: str
    status: str = "pending"


class ClassifyResponse(BaseModel):
    request_id: str
    model_id: str
    model_version: str | None = None
    pred_class: str
    latency_ms: int
    #: 未校准原始分数（按类别 ID 配对）；不提供或校验失败时为 null。
    scores: dict[str, float] | None = None
    #: 分数来源（raw_proba / raw_fasttext / raw_softmax）；无分数时为 null。
    score_kind: ScoreKind | None = None
    #: 前 k 类（默认 3）；无分数时为 null。
    top_k: list[TopKEntry] | None = None
    #: B3 软拒识：true 表示“可能是非新闻内容”，但**仍保留** pred_class/分数。
    rejected: bool = False
    #: 拒识原因：low_max_score / high_entropy；未拒识或无分数时为 null。
    reject_reason: str | None = None
    #: B3 阈值来源元数据；无分数/未启用拒识时为 null（不伪造）。
    #:
    #: 固定键（F6 契约）：source、calibrated、method、max_score_threshold、
    #: entropy_threshold、scope_note、sample_size；另有 dataset/seed/
    #: derived_for_model/applied_model_id/rule/entropy_base/observed_* 等辅助键。
    threshold_meta: dict[str, Any] | None = None
    #: B4：相似 pending 反馈的纠正类别参考；无命中/未装配时为 null。
    human_feedback_hint: HumanFeedbackHint | None = None
    #: B11 演示真实性：live / mock / unknown；null = 适配器未判定（不得推断为 live）。
    #: 约束：只有 worker ``/metadata.display_name`` 以 ``mock-`` 开头才是 mock；
    #: 取不到 metadata 时为 unknown，不猜测。cached 仅出现在离线快照产物。
    data_source: str | None = None


class CompareRequest(BaseModel):
    title: str
    models: list[str] | None = None


class CompareSummary(BaseModel):
    total: int
    ok: int
    failed: int
    status: str


class CompareResult(BaseModel):
    model_id: str
    status: str
    model_version: str | None = None
    pred_class: str | None = None
    latency_ms: int
    error: ErrorInfo | None = None
    #: B11 演示真实性：live / mock / unknown；null = 适配器未判定。
    data_source: str | None = None


class CompareResponse(BaseModel):
    request_id: str
    summary: CompareSummary
    results: list[CompareResult]
