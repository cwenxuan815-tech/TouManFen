"""竞技场（B7）请求/响应 Pydantic 模型。

放在 ``gateway/routers/`` 下、与 ``gateway/schemas.py`` 分离，避免与 B1/F3
正在修改的公共 schema 冲突。

匿名性契约：``ArenaNextResponse`` 只包含 ``anon_id`` / ``pred_class`` /
``latency_ms``，不包含 model_id / display_name / model_version / 真实标签。
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ArenaNextCandidate(BaseModel):
    """匿名候选（投票前不可还原模型身份）。"""

    anon_id: str
    pred_class: str
    latency_ms: int


class ArenaNextResponse(BaseModel):
    request_id: str
    item_id: str
    title: str
    candidates: list[ArenaNextCandidate]
    partial: bool = False


class ArenaVoteRequest(BaseModel):
    client_id: str
    item_id: str
    anon_id: str


class ArenaSelectedModel(BaseModel):
    """投票后仅揭晓被选中的模型，不返回其他候选身份。"""

    model_id: str
    display_name: str
    pred_class: str


class ArenaVoteResponse(BaseModel):
    request_id: str
    selected: ArenaSelectedModel
    elo_after: float
    games_applied: int


class ArenaLeaderboardEntry(BaseModel):
    model_id: str
    display_name: str
    elo: float
    votes: int
    sample_size: int
    wins: int
    losses: int
    win_rate: float | None
    wilson_lower: float
    wilson_upper: float
    warming_up: bool
    rank: int | None


class ArenaLeaderboardResponse(BaseModel):
    request_id: str
    status: str
    data_kind: str
    note: str
    min_votes_for_ranking: int
    entries: list[ArenaLeaderboardEntry] = Field(default_factory=list)
