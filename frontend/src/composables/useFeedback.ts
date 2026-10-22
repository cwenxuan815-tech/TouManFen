/**
 * F3/F8：纠错反馈状态。
 *
 * - B4 已实现真实 API；本 composable 负责 client_id、提交、去重提示与统计计数。
 * - 成功后仅表示“已记录，待审核”，不表示模型已变化。
 */
import { ref } from 'vue'
import {
  ApiClientError,
  friendlyErrorMessage,
  getFeedbackStats,
  isDuplicateFeedback,
  submitFeedback,
  type FeedbackStats,
} from '../api'

export interface FeedbackInput {
  title: string
  model_id: string
  pred_class: string
  corrected_class: string
  score?: number | null
}

export type FeedbackSubmitStatus = 'ok' | 'duplicate' | 'error'

const CLIENT_ID_KEY = 'tmf.feedback.client_id'

function generateClientId(): string {
  try {
    if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
      return crypto.randomUUID()
    }
    if (typeof crypto !== 'undefined' && typeof crypto.getRandomValues === 'function') {
      const bytes = crypto.getRandomValues(new Uint8Array(16))
      return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
    }
  } catch {
    // fall through
  }
  return `tmf-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

function ensureClientId(): string {
  try {
    const existing = window.localStorage.getItem(CLIENT_ID_KEY)
    if (existing) return existing
    const created = generateClientId()
    window.localStorage.setItem(CLIENT_ID_KEY, created)
    return created
  } catch {
    return generateClientId()
  }
}

export function useFeedback() {
  const supported = true
  const submitting = ref(false)
  const error = ref('')
  const duplicate = ref(false)
  const successMessage = ref('')
  const stats = ref<FeedbackStats | null>(null)
  const clientId = ref(ensureClientId())

  async function submit(input: FeedbackInput): Promise<FeedbackSubmitStatus> {
    if (submitting.value) return 'error'
    submitting.value = true
    error.value = ''
    duplicate.value = false
    successMessage.value = ''
    try {
      const result = await submitFeedback({
        client_id: clientId.value,
        title: input.title,
        model_id: input.model_id,
        pred_class: input.pred_class,
        corrected_class: input.corrected_class,
        score: input.score ?? null,
        source: 'web',
      })
      successMessage.value = result.message || '反馈已提交，待审核'
      if (stats.value) {
        stats.value = { ...stats.value, total: result.total, pending: result.total }
      } else {
        stats.value = {
          total: result.total,
          pending: result.total,
          by_class: {},
          by_model: {},
          similar_hits: 0,
          retention_days: 30,
        }
      }
      return 'ok'
    } catch (reason) {
      if (isDuplicateFeedback(reason)) {
        duplicate.value = true
        error.value = '已记录过，待审核'
        return 'duplicate'
      }
      if (reason instanceof ApiClientError && reason.code === 'CLIENT_ABORTED') {
        error.value = '请求已取消'
      } else {
        error.value = friendlyErrorMessage(reason)
      }
      return 'error'
    } finally {
      submitting.value = false
    }
  }

  async function loadStats(): Promise<void> {
    try {
      stats.value = await getFeedbackStats()
    } catch {
      // 计数是增强信息，失败时静默，不影响反馈提交。
    }
  }

  return {
    supported,
    submitting,
    error,
    duplicate,
    successMessage,
    stats,
    clientId,
    submit,
    loadStats,
  }
}
