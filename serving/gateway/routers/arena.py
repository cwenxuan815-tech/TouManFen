"""竞技场路由：/arena/next、/arena/vote、/arena/leaderboard（B7）。

路由只通过 ``request.app.state.arena`` 调用业务层；业务层在 composition root
（``serving/gateway/main.py`` 的 lifespan）注入，便于用 fake adapter 验收。
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..errors import ApiError, ERROR_ARENA_UNAVAILABLE
from .arena_schemas import (
    ArenaLeaderboardEntry,
    ArenaLeaderboardResponse,
    ArenaNextCandidate,
    ArenaNextResponse,
    ArenaSelectedModel,
    ArenaVoteRequest,
    ArenaVoteResponse,
)
from ...services.arena import (
    LEADERBOARD_DATA_KIND,
    LEADERBOARD_NOTE,
    ArenaService,
)

router = APIRouter(prefix="/api/v1", tags=["arena"])


def _service(request: Request) -> ArenaService:
    arena = getattr(request.app.state, "arena", None)
    if arena is None:
        # 组合根未装配（例如未启动 lifespan 的裸单测）时明确失败，不伪造数据。
        raise ApiError(503, ERROR_ARENA_UNAVAILABLE, "竞技场服务未装配")
    return arena


@router.get("/arena/next", response_model=ArenaNextResponse)
async def arena_next(request: Request) -> ArenaNextResponse:
    outcome = await _service(request).next_item(request.state.request_id)
    return ArenaNextResponse(
        request_id=request.state.request_id,
        item_id=outcome.item_id,
        title=outcome.title,
        candidates=[
            ArenaNextCandidate(
                anon_id=candidate.anon_id,
                pred_class=candidate.pred_class,
                latency_ms=candidate.latency_ms,
            )
            for candidate in outcome.candidates
        ],
        partial=outcome.partial,
    )


@router.post("/arena/vote", response_model=ArenaVoteResponse)
async def arena_vote(
    request: Request, payload: ArenaVoteRequest
) -> ArenaVoteResponse:
    outcome = await _service(request).vote(
        client_id=payload.client_id,
        item_id=payload.item_id,
        anon_id=payload.anon_id,
        request_id=request.state.request_id,
    )
    return ArenaVoteResponse(
        request_id=request.state.request_id,
        selected=ArenaSelectedModel(
            model_id=outcome.model_id,
            display_name=outcome.display_name,
            pred_class=outcome.pred_class,
        ),
        elo_after=outcome.elo_after,
        games_applied=outcome.games_applied,
    )


@router.get("/arena/leaderboard", response_model=ArenaLeaderboardResponse)
async def arena_leaderboard(request: Request) -> ArenaLeaderboardResponse:
    outcome = await _service(request).leaderboard(request.state.request_id)
    return ArenaLeaderboardResponse(
        request_id=request.state.request_id,
        status=outcome.status,
        data_kind=LEADERBOARD_DATA_KIND,
        note=LEADERBOARD_NOTE,
        min_votes_for_ranking=outcome.min_votes_for_ranking,
        entries=[
            ArenaLeaderboardEntry(
                model_id=entry.model_id,
                display_name=entry.display_name,
                elo=entry.elo,
                votes=entry.votes,
                wins=entry.wins,
                losses=entry.losses,
                win_rate=entry.win_rate,
                wilson_lower=entry.wilson_lower,
                wilson_upper=entry.wilson_upper,
                warming_up=entry.warming_up,
                rank=entry.rank,
            )
            for entry in outcome.entries
        ],
    )
