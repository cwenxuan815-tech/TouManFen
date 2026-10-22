/** F3：竞技场 API 边界（后端 B7 契约；F10 才接入页面）。 */
import type {
  ArenaLeaderboardResponse,
  ArenaNextResponse,
  ArenaVoteRequest,
  ArenaVoteResponse,
} from '../types'
import { request } from './client'

export function arenaNext(signal?: AbortSignal): Promise<ArenaNextResponse> {
  return request<ArenaNextResponse>(
    '/arena/next',
    { method: 'GET' },
    { timeoutProfile: 'arena', signal },
  )
}

export function arenaVote(
  payload: ArenaVoteRequest,
  signal?: AbortSignal,
): Promise<ArenaVoteResponse> {
  return request<ArenaVoteResponse>(
    '/arena/vote',
    { method: 'POST', body: JSON.stringify(payload) },
    { timeoutProfile: 'arena', signal },
  )
}

export function arenaLeaderboard(signal?: AbortSignal): Promise<ArenaLeaderboardResponse> {
  return request<ArenaLeaderboardResponse>(
    '/arena/leaderboard',
    { method: 'GET' },
    { timeoutProfile: 'arena', signal },
  )
}
