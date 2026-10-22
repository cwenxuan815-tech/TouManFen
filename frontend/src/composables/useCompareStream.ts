/**
 * F9：`/compare/stream`（SSE）流式对比状态机。
 *
 * 职责：
 * - 解析/校验 `model` / `summary` / `done` / `error` 事件；无效 JSON 或字段不合法一律丢弃，
 *   不覆盖已有有效结果；
 * - 按 model_id 去重（每个模型最多一条，先到先得）；
 * - 3s 首事件回退：连接建立后 3s 内没有任何有效事件 → 关闭 EventSource，
 *   调用既有 `compare()`（POST /compare）拿完整结果，标记 fallback；
 * - 任意时刻只维持一条 EventSource；`stop()` 关闭连接、清理监听与计时器；
 * - 组件卸载（onScopeDispose）自动 stop。
 *
 * 不做的事：不用前端定时器伪造事件/动画结果；不在 SSE 与 POST 之间混合数据。
 */
import { computed, onScopeDispose, ref } from 'vue'
import {
  ERROR_MESSAGES,
  compare,
  friendlyErrorMessage,
  isAbortError,
  openCompareStream,
  supportsCompareStream,
  type CompareStreamConnection,
} from '../api'
import type {
  CompareResponse,
  CompareStreamDoneEvent,
  CompareStreamErrorEvent,
  CompareStreamModelEvent,
  CompareStreamSummaryEvent,
} from '../types'

/** 清单 F9：3 秒内没有有效首事件即回退。 */
export const COMPARE_FIRST_EVENT_TIMEOUT_MS = 3_000

export type CompareStreamState =
  | 'idle'
  | 'starting'
  | 'streaming'
  | 'fallback'
  | 'done'
  | 'error'

/** 交给 CompareBoard 的纯快照（避免把 EventSource/handler 暴露给视图层）。 */
export interface CompareStreamSnapshot {
  status: CompareStreamState
  requestId: string
  /** 本次请求的模型顺序（用于渲染未到达模型的「等待中」占位）。 */
  requestedModelIds: string[]
  arrived: Partial<Record<string, CompareStreamModelEvent>>
  /** 到达顺序（服务端完成顺序）。 */
  arrivalOrder: string[]
  summary: CompareStreamSummaryEvent | null
  done: CompareStreamDoneEvent | null
  streamError: CompareStreamErrorEvent | null
  /** 面向用户的错误文案（流级错误、回退 POST 失败）。 */
  error: string
  fallback: boolean
  firstEvent: boolean
  firstEventAt: number | null
  /** 从发起到首个有效事件的耗时（毫秒）；未收到为 null。 */
  firstEventLatencyMs: number | null
  startedAt: number | null
  finishedAt: number | null
}

export interface UseCompareStreamOptions {
  /** 首事件回退窗口；默认 3000ms，仅测试可覆盖。 */
  firstEventTimeoutMs?: number
  /** 回退用的 POST 实现；默认 api.compare。仅用于测试注入。 */
  comparePost?: typeof compare
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value)
}

function asNullableString(value: unknown): string | null | undefined {
  if (value === null) return null
  if (typeof value === 'string') return value
  return undefined
}

/** 校验 `event: model`。字段不合法返回 null（该条丢弃）。 */
export function parseCompareStreamModelEvent(
  data: unknown,
): CompareStreamModelEvent | null {
  if (!isRecord(data)) return null
  if (typeof data.request_id !== 'string' || !data.request_id) return null
  if (typeof data.model_id !== 'string' || !data.model_id) return null
  if (data.status !== 'ok' && data.status !== 'error') return null
  if (!isFiniteNumber(data.latency_ms) || data.latency_ms < 0) return null
  const modelVersion = asNullableString(data.model_version)
  if (modelVersion === undefined) return null
  const predClass = asNullableString(data.pred_class)
  if (predClass === undefined) return null

  let error: CompareStreamModelEvent['error'] = null
  if (data.error !== null && data.error !== undefined) {
    if (!isRecord(data.error)) return null
    if (typeof data.error.code !== 'string' || !data.error.code) return null
    if (typeof data.error.message !== 'string') return null
    error = { code: data.error.code, message: data.error.message }
  }
  // 契约：ok 必有 pred_class 且无 error；error 必有 error 且无 pred_class。
  if (data.status === 'ok' && (!predClass || error)) return null
  if (data.status === 'error' && !error) return null

  return {
    request_id: data.request_id,
    model_id: data.model_id,
    status: data.status,
    latency_ms: data.latency_ms,
    model_version: modelVersion,
    pred_class: predClass,
    error,
    data_source:
      data.data_source === 'live' ||
      data.data_source === 'mock' ||
      data.data_source === 'cached' ||
      data.data_source === 'unknown'
        ? data.data_source
        : null,
  }
}

export function parseCompareStreamSummaryEvent(
  data: unknown,
): CompareStreamSummaryEvent | null {
  if (!isRecord(data)) return null
  if (typeof data.request_id !== 'string' || !data.request_id) return null
  if (data.status !== 'ok' && data.status !== 'partial' && data.status !== 'all_failed') {
    return null
  }
  const numericKeys = ['total', 'ok', 'failed', 'elapsed_ms', 'total_timeout_ms'] as const
  for (const key of numericKeys) {
    if (!isFiniteNumber(data[key])) return null
  }
  if (typeof data.partial !== 'boolean') return null
  return {
    request_id: data.request_id,
    status: data.status,
    total: data.total as number,
    ok: data.ok as number,
    failed: data.failed as number,
    elapsed_ms: data.elapsed_ms as number,
    total_timeout_ms: data.total_timeout_ms as number,
    partial: data.partial,
  }
}

export function parseCompareStreamDoneEvent(
  data: unknown,
): CompareStreamDoneEvent | null {
  if (!isRecord(data)) return null
  if (typeof data.request_id !== 'string' || !data.request_id) return null
  if (data.status !== 'ok' && data.status !== 'partial' && data.status !== 'all_failed') {
    return null
  }
  for (const key of ['total', 'ok', 'failed'] as const) {
    if (!isFiniteNumber(data[key])) return null
  }
  return {
    request_id: data.request_id,
    status: data.status,
    total: data.total as number,
    ok: data.ok as number,
    failed: data.failed as number,
  }
}

export function parseCompareStreamErrorEvent(
  data: unknown,
): CompareStreamErrorEvent | null {
  if (!isRecord(data)) return null
  if (typeof data.code !== 'string' || !data.code) return null
  if (typeof data.message !== 'string') return null
  return {
    request_id: typeof data.request_id === 'string' ? data.request_id : '',
    code: data.code,
    message: data.message,
  }
}

export function useCompareStream(options: UseCompareStreamOptions = {}) {
  const firstEventTimeoutMs = options.firstEventTimeoutMs ?? COMPARE_FIRST_EVENT_TIMEOUT_MS
  const comparePost = options.comparePost ?? compare

  const status = ref<CompareStreamState>('idle')
  const requestId = ref('')
  const requestedModelIds = ref<string[]>([])
  const arrived = ref<Partial<Record<string, CompareStreamModelEvent>>>({})
  const arrivalOrder = ref<string[]>([])
  const summary = ref<CompareStreamSummaryEvent | null>(null)
  const done = ref<CompareStreamDoneEvent | null>(null)
  const streamError = ref<CompareStreamErrorEvent | null>(null)
  const error = ref('')
  const fallback = ref(false)
  const firstEvent = ref(false)
  const firstEventAt = ref<number | null>(null)
  const firstEventLatencyMs = ref<number | null>(null)
  const startedAt = ref<number | null>(null)
  const finishedAt = ref<number | null>(null)

  let connection: CompareStreamConnection | null = null
  let fallbackController: AbortController | null = null
  let firstEventTimer: ReturnType<typeof setTimeout> | null = null
  let activeTitle = ''
  let activeModels: string[] = []
  /** 每次 start/stop 自增；异步回调只有在 token 匹配时才允许写状态。 */
  let attempt = 0
  let fallbackStarted = false

  function clearFirstEventTimer(): void {
    if (firstEventTimer !== null) {
      clearTimeout(firstEventTimer)
      firstEventTimer = null
    }
  }

  function closeConnection(): void {
    connection?.close()
    connection = null
  }

  function beginFirstEventTimer(): void {
    clearFirstEventTimer()
    firstEventTimer = setTimeout(() => {
      firstEventTimer = null
      void triggerFallback()
    }, firstEventTimeoutMs)
  }

  function markFirstEvent(): void {
    if (firstEvent.value) return
    firstEvent.value = true
    firstEventAt.value = Date.now()
    firstEventLatencyMs.value =
      startedAt.value === null ? null : Math.max(0, Date.now() - startedAt.value)
    clearFirstEventTimer()
  }

  function resetCollected(): void {
    requestId.value = ''
    requestedModelIds.value = []
    arrived.value = {}
    arrivalOrder.value = []
    summary.value = null
    done.value = null
    streamError.value = null
    error.value = ''
    fallback.value = false
    firstEvent.value = false
    firstEventAt.value = null
    firstEventLatencyMs.value = null
    startedAt.value = null
    finishedAt.value = null
  }

  function applyCompareResponse(response: CompareResponse): void {
    requestId.value = response.request_id
    const nextArrived: Partial<Record<string, CompareStreamModelEvent>> = {}
    const order: string[] = []
    for (const item of response.results ?? []) {
      if (!item || typeof item.model_id !== 'string' || !item.model_id) continue
      if (nextArrived[item.model_id]) continue
      nextArrived[item.model_id] = {
        request_id: response.request_id,
        model_id: item.model_id,
        status: item.status === 'ok' ? 'ok' : 'error',
        latency_ms: isFiniteNumber(item.latency_ms) ? item.latency_ms : 0,
        model_version: item.model_version ?? null,
        pred_class: item.pred_class ?? null,
        error: item.error ?? null,
      }
      order.push(item.model_id)
    }
    arrived.value = nextArrived
    arrivalOrder.value = order

    const postSummary = response.summary
    summary.value = {
      request_id: response.request_id,
      status: postSummary.status,
      total: postSummary.total,
      ok: postSummary.ok,
      failed: postSummary.failed,
      elapsed_ms:
        startedAt.value === null ? 0 : Math.max(0, Date.now() - startedAt.value),
      // POST /compare 不返回总截止；0 表示「非流式/未知」，UI 不展示该行。
      total_timeout_ms: 0,
      partial: postSummary.status === 'partial',
    }
    done.value = {
      request_id: response.request_id,
      status: postSummary.status,
      total: postSummary.total,
      ok: postSummary.ok,
      failed: postSummary.failed,
    }
    firstEvent.value = order.length > 0
    firstEventAt.value = firstEvent.value ? Date.now() : null
    firstEventLatencyMs.value =
      firstEvent.value && startedAt.value !== null
        ? Math.max(0, Date.now() - startedAt.value)
        : null
  }

  /**
   * 3s 首事件回退 / 连接失败回退。
   * 幂等：同一次 start 只回退一次；回退后清空 SSE 已收结果，避免与 POST 结果混合。
   */
  async function triggerFallback(): Promise<void> {
    if (fallbackStarted) return
    if (status.value !== 'starting' && status.value !== 'streaming') return
    fallbackStarted = true
    const token = attempt
    closeConnection()
    clearFirstEventTimer()

    // 丢弃可能已到达的 SSE 结果：SSE 与 POST 结果不得互相覆盖/混合。
    arrived.value = {}
    arrivalOrder.value = []
    summary.value = null
    done.value = null
    streamError.value = null
    fallback.value = true
    status.value = 'fallback'

    const controller = new AbortController()
    fallbackController = controller
    try {
      const response = await comparePost(activeTitle, [...activeModels], controller.signal)
      if (token !== attempt || controller.signal.aborted) return
      applyCompareResponse(response)
      finishedAt.value = Date.now()
      status.value = 'fallback'
    } catch (reason) {
      if (token !== attempt || controller.signal.aborted || isAbortError(reason)) return
      error.value = friendlyErrorMessage(reason)
      streamError.value = null
      status.value = 'error'
      finishedAt.value = Date.now()
    } finally {
      if (fallbackController === controller) fallbackController = null
    }
  }

  function handleStreamErrorEvent(event: CompareStreamErrorEvent, token: number): void {
    if (token !== attempt) return
    markFirstEvent()
    streamError.value = event
    error.value = ERROR_MESSAGES[event.code] || event.message || '对比流返回错误，请稍后重试。'
    status.value = 'error'
    finishedAt.value = Date.now()
    clearFirstEventTimer()
    closeConnection()
  }

  function handleEvent(
    eventName: string,
    data: unknown,
    token: number,
  ): void {
    if (token !== attempt) return
    if (eventName === 'model') {
      const modelEvent = parseCompareStreamModelEvent(data)
      if (!modelEvent) return
      // 按 model_id 去重：重复事件不覆盖先到结果。
      if (arrived.value[modelEvent.model_id]) return
      markFirstEvent()
      if (!requestId.value && modelEvent.request_id) requestId.value = modelEvent.request_id
      arrived.value = { ...arrived.value, [modelEvent.model_id]: modelEvent }
      arrivalOrder.value = [...arrivalOrder.value, modelEvent.model_id]
      if (status.value === 'starting') status.value = 'streaming'
      return
    }
    if (eventName === 'summary') {
      const summaryEvent = parseCompareStreamSummaryEvent(data)
      if (!summaryEvent) return
      markFirstEvent()
      summary.value = summaryEvent
      if (!requestId.value && summaryEvent.request_id) requestId.value = summaryEvent.request_id
      return
    }
    if (eventName === 'done') {
      const doneEvent = parseCompareStreamDoneEvent(data)
      if (!doneEvent) return
      markFirstEvent()
      done.value = doneEvent
      if (!requestId.value && doneEvent.request_id) requestId.value = doneEvent.request_id
      if (!summary.value) {
        // 契约中 summary 总在 done 前；若缺失则用 done 兜底（不编造耗时）。
        summary.value = {
          request_id: doneEvent.request_id,
          status: doneEvent.status,
          total: doneEvent.total,
          ok: doneEvent.ok,
          failed: doneEvent.failed,
          elapsed_ms: startedAt.value === null ? 0 : Math.max(0, Date.now() - startedAt.value),
          total_timeout_ms: 0,
          partial: doneEvent.status === 'partial',
        }
      }
      clearFirstEventTimer()
      closeConnection()
      finishedAt.value = Date.now()
      status.value = 'done'
      return
    }
    if (eventName === 'error') {
      const errorEvent = parseCompareStreamErrorEvent(data)
      if (!errorEvent) return
      handleStreamErrorEvent(errorEvent, token)
    }
  }

  /** 发起一次流式对比；会先静默关闭上一条连接（任意时刻只有一条 EventSource）。 */
  function start(title: string, models: string[]): void {
    const trimmed = title.trim()
    // 静默关闭：旧连接的迟到回调由 attempt token 丢弃，不报错。
    stop()

    const token = ++attempt
    activeTitle = trimmed
    activeModels = [...models]
    fallbackStarted = false
    resetCollected()
    requestedModelIds.value = [...activeModels]
    startedAt.value = Date.now()
    status.value = 'starting'

    if (!trimmed || activeModels.length === 0) {
      error.value = '请先输入新闻标题并选择至少一个模型。'
      status.value = 'error'
      finishedAt.value = Date.now()
      return
    }

    if (!supportsCompareStream()) {
      // 浏览器无 EventSource / 能力关闭：直接走旧 POST 路径。
      void triggerFallback()
      return
    }

    // 覆盖「连接迟迟不 open」的情况；open 后再重置为完整 3s 窗口。
    beginFirstEventTimer()
    try {
      connection = openCompareStream(trimmed, [...activeModels], {
        onOpen: () => {
          if (token !== attempt) return
          if (status.value === 'starting') status.value = 'streaming'
          if (!firstEvent.value) beginFirstEventTimer()
        },
        onEvent: (event) => {
          handleEvent(event.event, event.data, token)
        },
        onError: () => {
          if (token !== attempt) return
          void triggerFallback()
        },
      })
    } catch {
      void triggerFallback()
    }
  }

  /**
   * 关闭当前连接并清理计时器；不产生用户可见错误。
   * 进行中的 SSE 会回落到 idle（页面重新进入等待态），已收到的部分结果保留在快照中
   * 但不再更新；`reset()` 才会清空。
   */
  function stop(): void {
    attempt += 1
    closeConnection()
    clearFirstEventTimer()
    fallbackController?.abort()
    fallbackController = null
    if (status.value === 'starting' || status.value === 'streaming' || status.value === 'fallback') {
      status.value = 'idle'
      finishedAt.value = Date.now()
    }
  }

  function reset(): void {
    stop()
    resetCollected()
    status.value = 'idle'
  }

  const snapshot = computed<CompareStreamSnapshot>(() => ({
    status: status.value,
    requestId: requestId.value,
    requestedModelIds: requestedModelIds.value,
    arrived: arrived.value,
    arrivalOrder: arrivalOrder.value,
    summary: summary.value,
    done: done.value,
    streamError: streamError.value,
    error: error.value,
    fallback: fallback.value,
    firstEvent: firstEvent.value,
    firstEventAt: firstEventAt.value,
    firstEventLatencyMs: firstEventLatencyMs.value,
    startedAt: startedAt.value,
    finishedAt: finishedAt.value,
  }))

  onScopeDispose(() => {
    stop()
  })

  return {
    // 细粒度状态（供 UI/测试直接读取）
    status,
    requestId,
    requestedModelIds,
    arrived,
    arrivalOrder,
    summary,
    done,
    streamError,
    error,
    fallback,
    firstEvent,
    firstEventAt,
    firstEventLatencyMs,
    startedAt,
    finishedAt,
    snapshot,
    // 操作
    start,
    stop,
    reset,
  }
}

export type UseCompareStreamReturn = ReturnType<typeof useCompareStream>

