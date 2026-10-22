/**
 * F3/F10：竞技场 API 状态（B7 契约）。
 *
 * 边界：
 * - 本 composable 不渲染 UI，也不自动请求；
 * - `client_id` 固定持久化在 `localStorage` 的 `tmf.arena.client_id`：刷新后保持，
 *   仍是随机 UUID（保留 `crypto.randomUUID` / `getRandomValues` / 时间戳回退链）；
 *   `localStorage` 不可用（隐私模式、配额）时退回「仅本次内存的 UUID」，不阻断投票；
 * - 投票前不接触任何 `model_id` / `display_name`：`/arena/next` 只存匿名候选；
 *   只有 `/arena/vote` 的成功响应才带被选中模型的真实身份；
 * - 拉票 / 投票 / 榜单的错误与加载状态分列，交由页面分别复用 AsyncState / Toaster；
 * - 票数与 Elo 全部来自服务端，本地不伪造任何投票或票数。
 */
import { computed, ref } from 'vue'
import {
  ApiClientError,
  arenaLeaderboard,
  arenaNext,
  arenaVote,
  friendlyErrorMessage,
  isAbortError,
} from '../api'
import type {
  ArenaLeaderboardResponse,
  ArenaNextResponse,
  ArenaVoteResponse,
} from '../types'

/** 竞技场客户端标识的固定存储键（刷新后保持，见 §4.3 防刷边界）。 */
export const ARENA_CLIENT_ID_KEY = 'tmf.arena.client_id'
/** 与网关 `^[A-Za-z0-9._-]{1,64}$` 的校验保持一致；不合法值直接忽略并重新生成。 */
const CLIENT_ID_PATTERN = /^[A-Za-z0-9._-]{1,64}$/

/** 揭晓动画时长；F10 要求约 200ms，reduced-motion 下由 CSS 跳过动画。 */
export const ARENA_REVEAL_MS = 200

interface FailureDetail {
  message: string
  code: string
  requestId: string
}

function createClientId(): string {
  const cryptoApi = typeof globalThis.crypto !== 'undefined' ? globalThis.crypto : undefined
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') return cryptoApi.randomUUID()
  if (cryptoApi && typeof cryptoApi.getRandomValues === 'function') {
    const bytes = cryptoApi.getRandomValues(new Uint8Array(16))
    bytes[6] = (bytes[6] & 0x0f) | 0x40
    bytes[8] = (bytes[8] & 0x3f) | 0x80
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
  }
  return `tmf-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

function arenaStorage(): Storage | null {
  try {
    // 部分隐私模式下访问 localStorage 会直接抛错，这里统一降级为 null。
    return typeof globalThis.localStorage === 'undefined' ? null : globalThis.localStorage
  } catch {
    return null
  }
}

/** 读取已持久化的 client_id；非法/缺失时返回 null（由调用方生成新值）。 */
export function readArenaClientId(): string | null {
  const storage = arenaStorage()
  if (!storage) return null
  try {
    const value = storage.getItem(ARENA_CLIENT_ID_KEY)
    if (!value) return null
    const trimmed = value.trim()
    return CLIENT_ID_PATTERN.test(trimmed) ? trimmed : null
  } catch {
    return null
  }
}

function persistArenaClientId(clientId: string): void {
  const storage = arenaStorage()
  if (!storage) return
  try {
    storage.setItem(ARENA_CLIENT_ID_KEY, clientId)
  } catch {
    // 配额不足/隐私模式：保留内存态 UUID，后续投票仍按同一 client_id 去重。
  }
}

/** 刷新后保持的 client_id：优先复用已存值，否则生成并立即写回。 */
export function resolveArenaClientId(): string {
  const stored = readArenaClientId()
  if (stored) return stored
  const created = createClientId()
  persistArenaClientId(created)
  return created
}

function describeFailure(reason: unknown): FailureDetail {
  const message = friendlyErrorMessage(reason)
  if (reason instanceof ApiClientError) {
    return {
      message,
      code: reason.code,
      requestId: reason.requestId ?? '',
    }
  }
  return { message, code: '', requestId: '' }
}

export function useArena() {
  const clientId = ref(resolveArenaClientId())

  const item = ref<ArenaNextResponse | null>(null)
  const leaderboard = ref<ArenaLeaderboardResponse | null>(null)
  const voteResult = ref<ArenaVoteResponse | null>(null)
  /** 用户点击过的候选 anon_id（本地锚点；投票失败时清空，不伪造选中）。 */
  const selectedAnonId = ref('')
  /** 翻牌状态：成功投票后为 true（动画时长由 CSS 控制）。 */
  const revealed = ref(false)

  const nextLoading = ref(false)
  const voteLoading = ref(false)
  const leaderboardLoading = ref(false)

  const nextError = ref('')
  const nextErrorCode = ref('')
  const nextRequestId = ref('')

  const voteError = ref('')
  const voteErrorCode = ref('')
  const voteRequestId = ref('')

  const leaderboardError = ref('')
  const leaderboardErrorCode = ref('')
  const leaderboardRequestId = ref('')

  /** 抽题成功、投票未成功前才允许投票；投票成功后不可重复投。 */
  const canVote = computed(
    () => Boolean(item.value) && !voteLoading.value && voteResult.value === null,
  )

  function clearVoteState(): void {
    selectedAnonId.value = ''
    voteResult.value = null
    revealed.value = false
    voteError.value = ''
    voteErrorCode.value = ''
    voteRequestId.value = ''
  }

  async function loadNext(signal?: AbortSignal): Promise<void> {
    nextLoading.value = true
    nextError.value = ''
    nextErrorCode.value = ''
    nextRequestId.value = ''
    clearVoteState()
    try {
      item.value = await arenaNext(signal)
    } catch (reason) {
      if (isAbortError(reason)) return
      item.value = null
      const failure = describeFailure(reason)
      nextError.value = failure.message
      nextErrorCode.value = failure.code
      nextRequestId.value = failure.requestId
    } finally {
      nextLoading.value = false
    }
  }

  async function vote(anonId: string, signal?: AbortSignal): Promise<void> {
    if (!item.value || !canVote.value || !anonId) return
    const target = item.value
    voteLoading.value = true
    voteError.value = ''
    voteErrorCode.value = ''
    voteRequestId.value = ''
    selectedAnonId.value = anonId
    try {
      voteResult.value = await arenaVote(
        { client_id: clientId.value, item_id: target.item_id, anon_id: anonId },
        signal,
      )
      revealed.value = true
      // 投票成功后刷新榜单（票数由服务端返回，不在本地自增）。
      void loadLeaderboard()
    } catch (reason) {
      if (isAbortError(reason)) return
      // 失败时清空选中锚点：卡片回到可选状态，不假装已投票。
      selectedAnonId.value = ''
      const failure = describeFailure(reason)
      voteError.value = failure.message
      voteErrorCode.value = failure.code
      voteRequestId.value = failure.requestId
    } finally {
      voteLoading.value = false
    }
  }

  async function loadLeaderboard(signal?: AbortSignal): Promise<void> {
    leaderboardLoading.value = true
    leaderboardError.value = ''
    leaderboardErrorCode.value = ''
    leaderboardRequestId.value = ''
    try {
      leaderboard.value = await arenaLeaderboard(signal)
    } catch (reason) {
      if (isAbortError(reason)) return
      const failure = describeFailure(reason)
      leaderboardError.value = failure.message
      leaderboardErrorCode.value = failure.code
      leaderboardRequestId.value = failure.requestId
    } finally {
      leaderboardLoading.value = false
    }
  }

  return {
    clientId,
    item,
    leaderboard,
    voteResult,
    selectedAnonId,
    revealed,
    nextLoading,
    voteLoading,
    leaderboardLoading,
    nextError,
    nextErrorCode,
    nextRequestId,
    voteError,
    voteErrorCode,
    voteRequestId,
    leaderboardError,
    leaderboardErrorCode,
    leaderboardRequestId,
    canVote,
    loadNext,
    vote,
    loadLeaderboard,
  }
}
