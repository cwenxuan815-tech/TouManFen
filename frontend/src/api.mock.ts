/**
 * F3：显式 mock 数据（仅在测试/演示注入时使用）。
 *
 * 重要：生产代码不得静默导入本文件作为真实 API 的替代；所有 mock 数据都带
 * `mock: true` / `MOCK_SOURCE` 标记，调用方必须显式选择使用。
 */
import type {
  ArenaLeaderboardResponse,
  ArenaNextResponse,
  ArenaVoteRequest,
  ArenaVoteResponse,
  ClassifyResponse,
} from './types'
import type { FeedbackStats } from './api/feedback'

export const MOCK_SOURCE = 'mock' as const

export const mockClassifyResponse: ClassifyResponse & { mock: true } = {
  mock: true,
  request_id: 'mock-request',
  model_id: 'fasttext',
  model_version: null,
  pred_class: 'realty',
  latency_ms: 42,
  scores: null,
  score_kind: null,
  top_k: null,
}

export const mockArenaNext: ArenaNextResponse & { mock: true; data_source: 'mock' } = {
  mock: true,
  data_source: 'mock',
  request_id: 'mock-arena',
  item_id: 'mock-item-1',
  title: '演示标题（mock）',
  partial: false,
  candidates: [
    { anon_id: 'anon-a', pred_class: 'realty', latency_ms: 42 },
    { anon_id: 'anon-b', pred_class: 'finance', latency_ms: 61 },
    { anon_id: 'anon-c', pred_class: 'sports', latency_ms: 73 },
  ],
}

export function mockArenaVote(payload: ArenaVoteRequest): ArenaVoteResponse & { mock: true } {
  void payload
  return {
    mock: true,
    request_id: 'mock-arena',
    selected: { model_id: 'bert', display_name: 'BERT', pred_class: 'realty' },
    elo_after: 1224,
    games_applied: 4,
  }
}

export const mockArenaLeaderboard: ArenaLeaderboardResponse & { mock: true } = {
  mock: true,
  request_id: 'mock-arena',
  status: 'warming_up',
  data_kind: 'vote',
  note: '人类偏好投票强度，仅作演示参考。',
  min_votes_for_ranking: 50,
  entries: [],
}

export const mockFeedbackStats: FeedbackStats & { mock: true } = {
  mock: true,
  total: 0,
  pending: 0,
  by_class: {},
  by_model: {},
  similar_hits: 0,
  retention_days: 30,
}
