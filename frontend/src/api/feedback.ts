/** F3/F8：反馈 API（B4 已提供，F8 接入真实提交与统计）。 */
import { ApiClientError, request } from './client'

export interface FeedbackPayload {
  client_id: string
  title: string
  model_id: string
  pred_class: string
  corrected_class: string
  score?: number | null
  source?: string | null
}

export interface FeedbackSubmitResult {
  request_id: string
  feedback_id: number
  status: 'pending'
  total: number
  message: string
}

export interface FeedbackStats {
  request_id?: string
  total: number
  pending: number
  by_class: Record<string, number>
  by_model: Record<string, number>
  similar_hits: number
  retention_days: number
}

export const FEEDBACK_READY = true

export function submitFeedback(payload: FeedbackPayload): Promise<FeedbackSubmitResult> {
  return request<FeedbackSubmitResult>(
    '/feedback',
    {
      method: 'POST',
      body: JSON.stringify({
        client_id: payload.client_id,
        title: payload.title,
        model_id: payload.model_id,
        pred_class: payload.pred_class,
        corrected_class: payload.corrected_class,
        score: payload.score ?? null,
        source: payload.source ?? 'web',
      }),
    },
    { timeoutProfile: 'classify' },
  )
}

export function getFeedbackStats(): Promise<FeedbackStats> {
  return request<FeedbackStats>('/feedback/stats', { method: 'GET' }, { timeoutProfile: 'classify' })
}

export function isDuplicateFeedback(error: unknown): boolean {
  return error instanceof ApiClientError && error.code === 'FEEDBACK_DUPLICATE'
}
