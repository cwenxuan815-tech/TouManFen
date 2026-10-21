"""只读基础统计路由：/stats。"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ..schemas import StatsResponse

router = APIRouter(prefix="/api/v1", tags=["system"])


@router.get("/stats", response_model=StatsResponse)
async def get_stats(request: Request) -> StatsResponse:
    """进程内内存统计；重启后清零。

    只包含 HTTP 请求计数、状态码分类、路由耗时；
    不包含预测准确率、LLM 成本或预测成功率（本阶段不执行预测）。
    """
    snapshot = request.app.state.stats.snapshot()
    return StatsResponse(
        request_id=request.state.request_id,
        process_local=True,
        note="进程内内存统计，重启后清零；不包含预测/准确率/LLM 成本",
        **snapshot,
    )
