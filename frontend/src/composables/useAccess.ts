/**
 * 阶段 5C：全局访问名额状态机（前端）。
 *
 * 与后端 5B 契约对齐：
 * enter -> ready -> claim -> active(180s)
 * queued(120s ticket) / ready(15s) / cooling(60s)
 *
 * 约束：
 * - 服务端是 TTL/位置的唯一权威；前端倒计时只用于显示；
 * - 轮询约 2s + jitter，页面隐藏降频，不依赖轮询续租；
 * - ticket 只保存在 opaque 存储，不进入 URL/日志/错误文案；
 * - EventSource 不能加 header：access 启用时本 composable 返回
 *   streamAllowed=false，compare 走受门禁的 POST fallback。
 */
import { computed, onMounted, onScopeDispose, ref } from 'vue'
import {
  ApiClientError,
  cancelAccess,
  claimAccess,
  enterAccess,
  getAccessStatus,
  leaveAccess,
  type AccessStateResponse,
} from '../api'
import {
  ACCESS_TICKET_STORAGE_KEY,
  clearAccessTicket,
  readAccessTicket,
  saveAccessTicket,
  setAccessErrorHandler,
} from '../api/accessTicket'

export type AccessMode =
  | 'checking'
  | 'disabled'
  | 'active'
  | 'ready'
  | 'queued'
  | 'cooling'
  | 'queue_full'
  | 'cancelled'
  | 'unavailable'
  | 'identity'
  | 'error'

export const ACCESS_ACTIVE_SECONDS = 180
export const ACCESS_POLL_BASE_MS = 2_000
export const ACCESS_POLL_HIDDEN_MS = 8_000
export const ACCESS_POLL_JITTER_MS = 500

function errorCode(reason: unknown): string | null {
  return reason instanceof ApiClientError ? reason.code : null
}

function errorMessage(reason: unknown): string {
  if (reason instanceof ApiClientError) return reason.message
  return '访问状态暂时不可用，请稍后重试。'
}



export function useAccess() {
  const mode = ref<AccessMode>('checking')
  const ticket = ref<string | null>(null)
  const position = ref<number | null>(null)
  const ahead = ref<number | null>(null)
  const remainingSeconds = ref<number | null>(null)
  const errorMessageText = ref('')
  const busy = ref(false)

  let started = false
  let disposed = false
  let deadlineMs: number | null = null
  let tickTimer: ReturnType<typeof setInterval> | null = null
  let pollTimer: ReturnType<typeof setTimeout> | null = null
  let pollGeneration = 0
  let pollInFlight = false
  let claimInFlight = false
  let recoverInFlight = false
  let refreshInFlight = false

  const readyForBusiness = computed(
    () => mode.value === 'disabled' || mode.value === 'active',
  )
  const overlayVisible = computed(
    () => mode.value !== 'disabled' && mode.value !== 'active',
  )
  // EventSource cannot attach X-Access-Ticket: never attempt SSE when access
  // control is enabled. The existing POST /compare fallback uses fetch + header.
  const streamAllowed = computed(() => mode.value === 'disabled')

  function clearDeadline(): void {
    deadlineMs = null
    remainingSeconds.value = null
  }

  function stopTicker(): void {
    if (tickTimer !== null) {
      clearInterval(tickTimer)
      tickTimer = null
    }
  }

  function setDeadline(seconds: number | null | undefined): void {
    if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) {
      clearDeadline()
      return
    }
    deadlineMs = Date.now() + Math.max(0, seconds) * 1000
    remainingSeconds.value = Math.max(0, Math.ceil(seconds))
    if (tickTimer === null) {
      tickTimer = setInterval(tick, 250)
    }
  }

  function tick(): void {
    if (disposed || deadlineMs === null) return
    const next = Math.max(0, Math.ceil((deadlineMs - Date.now()) / 1000))
    remainingSeconds.value = next
    if (next > 0) return
    deadlineMs = null
    remainingSeconds.value = 0
    if (mode.value === 'active') {
      mode.value = 'checking'
      void refreshStatus()
    } else if (mode.value === 'cooling') {
      // 不用 enter 在 0 秒反复重置 deadline；status 返回 ready 后立即 claim，
      // 避免 enter 每次续出新的 cooling 从而饿死轮询。
      void refreshStatus()
    } else if (mode.value === 'queued' || mode.value === 'ready') {
      void refreshStatus()
    }
  }

  function pollDelay(): number {
    const hidden = typeof document !== 'undefined' && document.visibilityState === 'hidden'
    const base = hidden ? ACCESS_POLL_HIDDEN_MS : ACCESS_POLL_BASE_MS
    return base + Math.floor(Math.random() * ACCESS_POLL_JITTER_MS)
  }

  function stopPoll(): void {
    pollGeneration += 1
    if (pollTimer !== null) {
      clearTimeout(pollTimer)
      pollTimer = null
    }
  }

  function schedulePoll(delay?: number): void {
    stopPoll()
    if (disposed) return
    const generation = pollGeneration
    pollTimer = setTimeout(() => {
      pollTimer = null
      void pollStatus(generation)
    }, delay ?? pollDelay())
  }

  function stopRouting(): void {
    stopPoll()
    stopTicker()
    clearDeadline()
  }

  function storeTicket(value: string | null | undefined): void {
    const text = (value || '').trim()
    if (!text) return
    ticket.value = text
    saveAccessTicket(text)
  }

  function clearLocalTicket(): void {
    ticket.value = null
    clearAccessTicket()
  }

  function setFailure(nextMode: AccessMode, message: string): void {
    stopRouting()
    mode.value = nextMode
    errorMessageText.value = message
  }

  function applyResponse(response: AccessStateResponse, fromEnter: boolean): void {
    errorMessageText.value = ''
    if (fromEnter && response.ticket) storeTicket(response.ticket)
    if (!ticket.value && response.ticket) storeTicket(response.ticket)
    position.value = response.position ?? null
    ahead.value = response.ahead ?? null
    if (response.state === 'expired') {
      void recover('访问票据已过期，正在重新进入…')
      return
    }
    mode.value = response.state
    if (response.remaining_seconds !== null && response.remaining_seconds !== undefined) {
      setDeadline(response.remaining_seconds)
    } else {
      clearDeadline()
    }

    if (response.state === 'active') {
      stopPoll()
      // keep ticker if deadline exists; active countdown is local display only
    } else if (response.state === 'ready') {
      stopPoll()
      void claimReady()
    } else if (response.state === 'queued' || response.state === 'cooling') {
      schedulePoll()
    } else if (response.state === 'cancelled') {
      stopRouting()
    }
  }

  async function enterFlow(candidate: string | null): Promise<void> {
    if (disposed) return
    const generation = ++pollGeneration
    busy.value = true
    try {
      const response = await enterAccess(candidate)
      if (disposed || generation !== pollGeneration) return
      applyResponse(response, true)
    } catch (reason) {
      if (disposed || generation !== pollGeneration) return
      const code = errorCode(reason)
      if (code === 'ACCESS_DISABLED') {
        clearLocalTicket()
        stopRouting()
        mode.value = 'disabled'
        errorMessageText.value = ''
      } else if (code === 'ACCESS_QUEUE_FULL') {
        clearDeadline()
        mode.value = 'queue_full'
        errorMessageText.value = '队列已满，请稍后再试。'
      } else if (code === 'ACCESS_UNAVAILABLE') {
        setFailure('unavailable', '访问管理暂时不可用，请稍后重试。')
      } else if (code === 'ACCESS_IDENTITY_INVALID') {
        setFailure('identity', '无法确认当前网络身份，暂时不能进入。')
      } else if (
        (code === 'ACCESS_TICKET_INVALID' || code === 'ACCESS_EXPIRED')
        && candidate
      ) {
        clearLocalTicket()
        await recover('访问票据已失效，正在重新进入…')
      } else {
        setFailure('error', errorMessage(reason))
      }
    } finally {
      busy.value = false
    }
  }

  async function claimReady(): Promise<void> {
    if (claimInFlight || mode.value !== 'ready' || !ticket.value) return
    claimInFlight = true
    busy.value = true
    try {
      const response = await claimAccess(ticket.value)
      if (disposed) return
      applyResponse(response, false)
    } catch (reason) {
      if (disposed) return
      const code = errorCode(reason)
      if (code === 'ACCESS_TICKET_INVALID' || code === 'ACCESS_EXPIRED') {
        await recover('轮到你的 15 秒已过，正在重新排队…')
      } else if (code === 'ACCESS_UNAVAILABLE') {
        setFailure('unavailable', '访问管理暂时不可用，请稍后重试。')
      } else if (code === 'ACCESS_POLL_THROTTLED') {
        schedulePoll(1_500 + Math.random() * 500)
      } else {
        setFailure('error', errorMessage(reason))
      }
    } finally {
      claimInFlight = false
      busy.value = false
    }
  }

  async function pollStatus(generation: number): Promise<void> {
    if (disposed || generation !== pollGeneration || pollInFlight) return
    if (mode.value !== 'queued' && mode.value !== 'ready' && mode.value !== 'cooling') return
    if (!ticket.value) {
      await recover('访问票据丢失，正在重新进入…')
      return
    }
    pollInFlight = true
    try {
      const response = await getAccessStatus(ticket.value)
      if (disposed || generation !== pollGeneration) return
      applyResponse(response, false)
    } catch (reason) {
      if (disposed || generation !== pollGeneration) return
      const code = errorCode(reason)
      if (code === 'ACCESS_POLL_THROTTLED') {
        schedulePoll(1_500 + Math.random() * 500)
      } else if (code === 'ACCESS_TICKET_INVALID' || code === 'ACCESS_EXPIRED') {
        await recover('访问票据已过期，正在重新进入…')
      } else if (code === 'ACCESS_UNAVAILABLE') {
        setFailure('unavailable', '访问管理暂时不可用，请稍后重试。')
      } else if (code === 'ACCESS_IDENTITY_INVALID') {
        setFailure('identity', '无法确认当前网络身份，暂时不能进入。')
      } else {
        // 临时网络错误不快速自旋；稍后重试，保持服务端权威。
        schedulePoll(5_000 + Math.random() * 500)
      }
    } finally {
      pollInFlight = false
    }
  }

  async function refreshStatus(): Promise<void> {
    if (refreshInFlight || !ticket.value) {
      if (!ticket.value) await recover('访问票据丢失，正在重新进入…')
      return
    }
    refreshInFlight = true
    try {
      const response = await getAccessStatus(ticket.value)
      if (disposed) return
      applyResponse(response, false)
    } catch (reason) {
      if (disposed) return
      const code = errorCode(reason)
      if (code === 'ACCESS_TICKET_INVALID' || code === 'ACCESS_EXPIRED') {
        await recover('访问票据已失效，正在重新进入…')
      } else if (code === 'ACCESS_UNAVAILABLE') {
        setFailure('unavailable', '访问管理暂时不可用，请稍后重试。')
      } else if (code === 'ACCESS_IDENTITY_INVALID') {
        setFailure('identity', '无法确认当前网络身份，暂时不能进入。')
      } else if (code === 'ACCESS_POLL_THROTTLED') {
        schedulePoll(1_500 + Math.random() * 500)
      } else {
        setFailure('error', errorMessage(reason))
      }
    } finally {
      refreshInFlight = false
    }
  }

  async function recover(message: string): Promise<void> {
    if (recoverInFlight || disposed) return
    recoverInFlight = true
    stopRouting()
    clearLocalTicket()
    position.value = null
    ahead.value = null
    mode.value = 'checking'
    errorMessageText.value = message
    try {
      await enterFlow(null)
    } finally {
      recoverInFlight = false
    }
  }

  async function retry(): Promise<void> {
    if (busy.value) return
    errorMessageText.value = ''
    if (mode.value === 'disabled') return
    if (mode.value === 'queue_full' || mode.value === 'cancelled') {
      clearLocalTicket()
      await enterFlow(null)
      return
    }
    await enterFlow(ticket.value)
  }

  async function cancelQueue(): Promise<void> {
    if (mode.value !== 'queued' || !ticket.value || busy.value) return
    busy.value = true
    try {
      await cancelAccess(ticket.value)
    } catch (reason) {
      const code = errorCode(reason)
      if (code === 'ACCESS_UNAVAILABLE') {
        setFailure('unavailable', '访问管理暂时不可用，请稍后重试。')
        return
      }
      // 已过期/被其他标签取消：清理后进入取消态。
    } finally {
      busy.value = false
    }
    stopRouting()
    clearLocalTicket()
    position.value = null
    ahead.value = null
    mode.value = 'cancelled'
    errorMessageText.value = '已取消排队。'
  }

  async function leaveActive(): Promise<void> {
    if ((mode.value !== 'active' && mode.value !== 'ready') || !ticket.value || busy.value) return
    busy.value = true
    try {
      await leaveAccess(ticket.value)
    } catch (reason) {
      const code = errorCode(reason)
      if (code === 'ACCESS_UNAVAILABLE') {
        setFailure('unavailable', '访问管理暂时不可用，请稍后重试。')
        return
      }
    } finally {
      busy.value = false
    }
    stopRouting()
    clearLocalTicket()
    position.value = null
    ahead.value = null
    mode.value = 'cancelled'
    errorMessageText.value = '已提前结束本次使用。'
  }

  function onStorage(event: StorageEvent): void {
    if (event.key !== ACCESS_TICKET_STORAGE_KEY) return
    // 不能先读 readAccessTicket()：本标签的内存缓存仍保存旧 ticket，
    // 会掩盖其他标签刚执行的清除操作。storage 事件以 newValue 为准。
    const next = (event.newValue || '').trim() || null
    if (next === ticket.value) return
    if (!next) {
      void recover('其他标签已结束访问，正在恢复访问状态…')
      return
    }
    storeTicket(next)
    void refreshStatus()
  }

  function onBusinessAccessError(code: string): void {
    if (mode.value === 'disabled' || mode.value === 'checking') return
    if (code === 'ACCESS_UNAVAILABLE') {
      setFailure('unavailable', '访问管理暂时不可用，请稍后重试。')
      return
    }
    if (code === 'ACCESS_IDENTITY_INVALID') {
      setFailure('identity', '无法确认当前网络身份，暂时不能进入。')
      return
    }
    if (
      code === 'ACCESS_REQUIRED'
      || code === 'ACCESS_QUEUED'
      || code === 'ACCESS_COOLING'
      || code === 'ACCESS_CLAIM_REQUIRED'
      || code === 'ACCESS_TICKET_INVALID'
      || code === 'ACCESS_EXPIRED'
    ) {
      void refreshStatus()
    }
  }

  function onVisibility(): void {
    if (disposed) return
    if (mode.value === 'active') {
      void refreshStatus()
    } else if (mode.value === 'queued' || mode.value === 'ready' || mode.value === 'cooling') {
      void pollStatus(pollGeneration)
    }
  }

  async function start(): Promise<void> {
    if (started) return
    started = true
    ticket.value = readAccessTicket()
    await enterFlow(ticket.value)
  }

  onMounted(() => {
    setAccessErrorHandler(onBusinessAccessError)
    if (typeof window !== 'undefined') {
      window.addEventListener('storage', onStorage)
      document.addEventListener('visibilitychange', onVisibility)
    }
    void start()
  })

  onScopeDispose(() => {
    disposed = true
    stopRouting()
    setAccessErrorHandler(null)
    if (typeof window !== 'undefined') {
      window.removeEventListener('storage', onStorage)
      document.removeEventListener('visibilitychange', onVisibility)
    }
  })

  return {
    mode,
    ticket,
    position,
    ahead,
    remainingSeconds,
    errorMessage: errorMessageText,
    busy,
    readyForBusiness,
    overlayVisible,
    streamAllowed,
    retry,
    cancelQueue,
    leaveActive,
    refreshStatus,
  }
}
