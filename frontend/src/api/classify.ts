/** F3：分类/模型相关 API（保持既有 HTTP 契约）。 */
import type {
  ClassifyResponse,
  CompareResponse,
  CompareStreamEventName,
  ModelsResponse,
} from '../types'
import { API_BASE, API_PREFIX, ApiClientError, request } from './client'

export function getModels(signal?: AbortSignal): Promise<ModelsResponse> {
  return request<ModelsResponse>(
    '/models',
    { method: 'GET' },
    { timeoutProfile: 'classify', signal },
  )
}

/** 单模型分类：单模型 20s 超时。 */
export function classify(
  title: string,
  model: string,
  signal?: AbortSignal,
): Promise<ClassifyResponse> {
  return request<ClassifyResponse>(
    '/classify',
    { method: 'POST', body: JSON.stringify({ title, model }) },
    { timeoutProfile: 'classify', signal },
  )
}

/** F5 预留：实时输入使用 10s 超时；当前页面未启用实时输入。 */
export function classifyRealtime(
  title: string,
  model: string,
  signal?: AbortSignal,
): Promise<ClassifyResponse> {
  return request<ClassifyResponse>(
    '/classify',
    { method: 'POST', body: JSON.stringify({ title, model }) },
    { timeoutProfile: 'realtime', signal },
  )
}

/**
 * 多模型对比（POST，非流式）。F9 的三条路径仍依赖它：
 * 1) SSE 不可用 / 3s 内无有效首事件时的回退；
 * 2) `COMPARE_STREAM_READY=false` 或浏览器无 EventSource 时的兼容路径。
 */
export function compare(
  title: string,
  models: string[],
  signal?: AbortSignal,
): Promise<CompareResponse> {
  return request<CompareResponse>(
    '/compare',
    { method: 'POST', body: JSON.stringify({ title, models }) },
    { timeoutProfile: 'compare', signal },
  )
}

/** B6 已落地：`/compare/stream`（SSE）可用。 */
export const COMPARE_STREAM_READY = true

/** 浏览器是否具备 EventSource（SSE 能力检测；不伪造流式结果）。 */
export function supportsCompareStream(): boolean {
  return COMPARE_STREAM_READY && typeof globalThis.EventSource !== 'undefined'
}

/** SSE 连接 URL：必须 GET query，title/models 均做 encodeURIComponent。 */
export function compareStreamUrl(title: string, models: string[]): string {
  const modelParam = models.map((model) => encodeURIComponent(model)).join(',')
  return `${API_BASE}${API_PREFIX}/compare/stream?title=${encodeURIComponent(title)}&models=${modelParam}`
}

export interface CompareStreamEvent {
  event: CompareStreamEventName
  /** 已通过 JSON.parse 的原始 data；字段校验交给下游状态机。 */
  data: unknown
  /** 原始 data 文本（仅用于调试/测试，不参与渲染）。 */
  raw: string
  /** 客户端收到事件的时刻。 */
  receivedAt: number
}

export type CompareStreamEventHandler = (event: CompareStreamEvent) => void
export type CompareStreamErrorHandler = (error: ApiClientError) => void
export type CompareStreamDoneHandler = () => void

export interface CompareStreamOptions {
  onEvent?: CompareStreamEventHandler
  onError?: CompareStreamErrorHandler
  onDone?: CompareStreamDoneHandler
  onOpen?: () => void
}

export interface CompareStreamConnection {
  /** 实际使用的 SSE URL（测试/网络排查用）。 */
  readonly url: string
  /** EventSource readyState；连接已关闭时为 CLOSED(2)。 */
  readonly readyState: number
  readonly closed: boolean
  onEvent(handler: CompareStreamEventHandler): () => void
  onError(handler: CompareStreamErrorHandler): () => void
  onDone(handler: CompareStreamDoneHandler): () => void
  onOpen(handler: () => void): () => void
  close(): void
}

/**
 * F9：封装 EventSource，只做「连接 + 解析命名事件」，不做业务状态机。
 *
 * - 仅 GET query（浏览器 EventSource 不支持 POST body）；
 * - `model`/`summary`/`done`/`error` 四个命名事件逐一解析；JSON 无效时丢弃该条；
 * - 心跳是 `:` 注释行，浏览器不会派发事件，天然忽略；
 * - 连接错误（EventSource 内置 error，无 data）→ onError(NETWORK_ERROR)；
 * - `event: error` 是服务端事件（MessageEvent.data 为 JSON）→ 作为普通事件交给状态机，
 *   避免和连接错误混淆；
 * - `done` 后立即 close，防止浏览器按 SSE 规范自动重连造成第二条连接/重复事件。
 */
export function openCompareStream(
  title: string,
  models: string[],
  options: CompareStreamOptions = {},
): CompareStreamConnection {
  if (!supportsCompareStream()) {
    throw new ApiClientError(0, 'NOT_IMPLEMENTED', '当前环境不支持流式对比（EventSource）。')
  }

  const url = compareStreamUrl(title, models)
  const source = new EventSource(url)
  const eventHandlers = new Set<CompareStreamEventHandler>(
    options.onEvent ? [options.onEvent] : [],
  )
  const errorHandlers = new Set<CompareStreamErrorHandler>(
    options.onError ? [options.onError] : [],
  )
  const doneHandlers = new Set<CompareStreamDoneHandler>(
    options.onDone ? [options.onDone] : [],
  )
  const openHandlers = new Set<() => void>(options.onOpen ? [options.onOpen] : [])

  let closed = false
  /** 防止 onError 被重复派发（CLOSED 与网络错误可能先后到达）。 */
  let transportErrorReported = false
  let doneReported = false

  function dispatchEvent(
    event: CompareStreamEventName,
    message: MessageEvent<string>,
  ): void {
    if (closed) return
    const raw = typeof message.data === 'string' ? message.data : ''
    let data: unknown
    try {
      data = JSON.parse(raw)
    } catch {
      // 无效消息直接丢弃：不覆盖已有有效结果，也不触发回退。
      return
    }
    const payload: CompareStreamEvent = {
      event,
      data,
      raw,
      receivedAt: Date.now(),
    }
    for (const handler of [...eventHandlers]) {
      if (closed) return
      handler(payload)
    }
  }

  const onModel = (message: MessageEvent<string>): void => dispatchEvent('model', message)
  const onSummary = (message: MessageEvent<string>): void => dispatchEvent('summary', message)
  const onStreamErrorEvent = (message: MessageEvent<string>): void => {
    dispatchEvent('error', message)
  }
  const onDone = (message: MessageEvent<string>): void => {
    dispatchEvent('done', message)
    if (closed || doneReported) return
    doneReported = true
    for (const handler of [...doneHandlers]) handler()
    // 服务端在 done 后关闭流；主动 close 以免浏览器自动重连。
    close()
  }

  const onOpen = (): void => {
    if (closed) return
    for (const handler of [...openHandlers]) handler()
  }

  /**
   * EventSource 内置 error 有两种：
   * - 服务端 `event: error` 是 MessageEvent（带 string data），已在命名监听里处理；
   * - 连接错误是 Event（无 data）。CONNECTING 表示浏览器正在自动重连，
   *   这里不交给状态机重连（只允许一条稳定连接），直接报错由上层回退。
   */
  const onTransportError = (event: Event): void => {
    if (closed || transportErrorReported || doneReported) return
    const maybeMessage = event as MessageEvent<unknown>
    if (typeof maybeMessage.data === 'string') return
    transportErrorReported = true
    const error = new ApiClientError(
      0,
      'NETWORK_ERROR',
      '无法连接对比流服务，请确认后端已启动。',
    )
    for (const handler of [...errorHandlers]) handler(error)
  }

  source.addEventListener('model', onModel as EventListener)
  source.addEventListener('summary', onSummary as EventListener)
  source.addEventListener('done', onDone as EventListener)
  source.addEventListener('error', onTransportError as EventListener)
  // 服务端 `event: error` 与内置 error 同名，用带 data 的 MessageEvent 区分。
  source.addEventListener('error', onStreamErrorEvent as EventListener)
  source.addEventListener('open', onOpen)

  function close(): void {
    if (closed) return
    closed = true
    source.removeEventListener('model', onModel as EventListener)
    source.removeEventListener('summary', onSummary as EventListener)
    source.removeEventListener('done', onDone as EventListener)
    source.removeEventListener('error', onTransportError as EventListener)
    source.removeEventListener('error', onStreamErrorEvent as EventListener)
    source.removeEventListener('open', onOpen)
    source.close()
    eventHandlers.clear()
    errorHandlers.clear()
    doneHandlers.clear()
    openHandlers.clear()
  }

  return {
    url,
    get readyState() {
      return source.readyState
    },
    get closed() {
      return closed
    },
    onEvent(handler) {
      eventHandlers.add(handler)
      return () => {
        eventHandlers.delete(handler)
      }
    },
    onError(handler) {
      errorHandlers.add(handler)
      return () => {
        errorHandlers.delete(handler)
      }
    },
    onDone(handler) {
      doneHandlers.add(handler)
      return () => {
        doneHandlers.delete(handler)
      }
    },
    onOpen(handler) {
      openHandlers.add(handler)
      return () => {
        openHandlers.delete(handler)
      }
    },
    close,
  }
}
