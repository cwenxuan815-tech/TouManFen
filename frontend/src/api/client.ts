/** F3：统一 HTTP 客户端（超时分级、AbortSignal、错误归一化、request id）。 */

export type TimeoutProfile = 'realtime' | 'classify' | 'compare' | 'arena'

/** 清单 F3 的超时口径（毫秒）。 */
export const TIMEOUT_MS: Record<TimeoutProfile, number> = {
  realtime: 10_000,
  classify: 20_000,
  compare: 25_000,
  arena: 25_000,
}

function resolveApiBase(): string {
  const configured = import.meta.env.VITE_API_BASE_URL?.trim()
  if (configured) return configured.replace(/\/$/, '')

  // In LAN mode, use the host that served this page so multiple NICs/subnets
  // each reach the gateway through the matching local address.
  if (typeof window !== 'undefined' && window.location.hostname) {
    const port = import.meta.env.VITE_API_PORT?.trim() || '8000'
    return `${window.location.protocol}//${window.location.hostname}:${port}`
  }
  return 'http://127.0.0.1:8000'
}

export const API_BASE = resolveApiBase()
export const API_PREFIX = '/api/v1'

export class ApiClientError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId?: string
  readonly isAbort: boolean

  constructor(status: number, code: string, message: string, requestId?: string) {
    super(message)
    this.name = 'ApiClientError'
    this.status = status
    this.code = code
    this.requestId = requestId
    this.isAbort = code === 'CLIENT_ABORTED'
  }
}

/** 用户主动取消（AbortSignal）不应被当成网络/服务故障展示。 */
export function isAbortError(error: unknown): boolean {
  return error instanceof ApiClientError && error.isAbort
}

export function isNotImplementedError(error: unknown): boolean {
  return error instanceof ApiClientError && error.code === 'NOT_IMPLEMENTED'
}

/** F3：后端错误码 → 中文用户提示的集中映射（未知错误保留兜底）。 */
export const ERROR_MESSAGES: Record<string, string> = {
  // 分类/标题
  REJECTED: '该标题可能不是新闻内容。',
  TITLE_TOO_LONG: '标题最多支持 200 个字符。',
  INVALID_TITLE: '请先输入新闻标题。',
  UNKNOWN_MODEL: '所选模型不存在，请刷新模型列表。',
  MODEL_DISABLED: '所选模型当前不可用。',
  INVALID_MODELS: '请选择至少一个可用模型。',
  INVALID_REQUEST: '请求参数不正确，请检查后重试。',
  INVALID_REQUEST_ID: '请求编号格式不正确。',
  // worker / 网关
  WORKER_TIMEOUT: '模型响应超时，可以稍后重试。',
  WORKER_UNAVAILABLE: '所选模型服务不可达，请确认后端 worker 已启动。',
  WORKER_HTTP_ERROR: '模型服务暂时不可用，请稍后重试。',
  WORKER_BAD_RESPONSE: '模型返回格式异常，请稍后重试。',
  MODEL_ERROR: '模型调用失败，请稍后重试。',
  INVALID_MODEL_OUTPUT: '模型返回了无法识别的类别，请稍后重试。',
  INTERNAL_ERROR: '服务暂时不可用，请稍后重试。',
  // 客户端
  CLIENT_TIMEOUT: '请求等待时间过长，请稍后重试。',
  CLIENT_ABORTED: '请求已取消。',
  NETWORK_ERROR: '无法连接分类服务，请确认后端已启动。',
  NOT_IMPLEMENTED: '该功能尚未就绪。',
  // 竞技场（B7）
  ARENA_NO_ITEMS: '当前没有可用的竞技场题目。',
  ARENA_UNAVAILABLE: '暂时没有足够的模型结果，请稍后重试。',
  ARENA_ITEM_NOT_FOUND: '该题目不存在或已失效，请刷新后重试。',
  ARENA_ANON_INVALID: '选项无效，请刷新题目后重试。',
  ARENA_DUPLICATE_VOTE: '你已经对这道题投过票了。',
  ARENA_RATE_LIMITED: '投票太快了，请稍后再试。',
  ARENA_STORE_ERROR: '竞技场存储暂时不可用，请稍后重试。',
  // 反馈（B4/F8）
  FEEDBACK_INVALID: '反馈内容不完整或格式不正确，请检查后重试。',
  FEEDBACK_DUPLICATE: '这条反馈已经记录过了，待审核。',
  FEEDBACK_STORE_ERROR: '反馈存储暂时不可用，请稍后重试。',
  FEEDBACK_UNAVAILABLE: '反馈服务暂时不可用，请稍后重试。',
  // 离线基准（B8/F11）
  BENCH_ARTIFACT_NOT_FOUND: '离线基准产物不存在，请先运行离线评估脚本生成。',
  BENCH_ARTIFACT_INVALID: '离线基准产物结构不合法，请重新生成后再试。',
  HTTP_ERROR: '服务暂时不可用，请稍后重试。',
}

/**
 * 将任意异常映射为可展示的中文提示。
 * - AbortSignal 取消 → “请求已取消”，不当作网络故障；
 * - 已登记错误码 → 固定文案；
 * - 未登记错误码 → 后端安全文案或最终兜底。
 */
export function friendlyErrorMessage(error: unknown): string {
  if (isAbortError(error)) return ERROR_MESSAGES.CLIENT_ABORTED
  if (error instanceof ApiClientError) {
    return ERROR_MESSAGES[error.code] || error.message || '请求没有完成，请稍后重试。'
  }
  return '请求没有完成，请检查服务状态后重试。'
}

function requestId(): string {
  // crypto.randomUUID() 只在“安全上下文”（HTTPS 或 localhost）可用。
  // 通过非 localhost 的 HTTP 地址（例如局域网 IP）访问时属于非安全上下文，
  // 直接调用会抛 TypeError 并让请求根本没发出去（表现为“无法连接后端”）。
  const cryptoApi = typeof globalThis.crypto !== 'undefined' ? globalThis.crypto : undefined
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
    return cryptoApi.randomUUID()
  }
  if (cryptoApi && typeof cryptoApi.getRandomValues === 'function') {
    const bytes = cryptoApi.getRandomValues(new Uint8Array(16))
    bytes[6] = (bytes[6] & 0x0f) | 0x40
    bytes[8] = (bytes[8] & 0x3f) | 0x80
    const hex = Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
  }
  // 极旧浏览器兜底：仍满足网关 ^[A-Za-z0-9._-]{1,64}$ 的 X-Request-ID 校验。
  return `tmf-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`
}

export interface RequestOptions {
  /** 外部取消信号；取消不会被映射为普通网络故障。 */
  signal?: AbortSignal
  /** 显式超时（毫秒），优先于 timeoutProfile；用于测试/特殊调用。 */
  timeoutMs?: number
  /** 按清单分级超时；默认 classify(20s)。 */
  timeoutProfile?: TimeoutProfile
}

/**
 * 统一请求封装：
 * - 手动组合 AbortSignal 与超时，覆盖 fetch + 响应体读取/解析的完整生命周期；
 * - 区分 CLIENT_ABORTED / CLIENT_TIMEOUT（取消不会被误报为网络故障）；
 * - 非 2xx 转成 ApiClientError(status, code, message, requestId)；
 * - 网络失败转 NETWORK_ERROR；不泄露响应堆栈；
 * - timeout/listener 在成功、HTTP 错误、JSON 解析失败、超时、取消时都清理。
 */
export async function request<T>(
  path: string,
  init: RequestInit = {},
  options: RequestOptions = {},
): Promise<T> {
  const timeoutMs = options.timeoutMs ?? TIMEOUT_MS[options.timeoutProfile ?? 'classify']
  const controller = new AbortController()
  let timedOut = false
  const externalSignal =
    options.signal ?? (init.signal as AbortSignal | null | undefined) ?? undefined
  let externalAborted = externalSignal?.aborted ?? false

  if (externalSignal?.aborted) {
    throw new ApiClientError(0, 'CLIENT_ABORTED', '请求已取消。')
  }

  const onExternalAbort = (): void => {
    externalAborted = true
    controller.abort()
  }
  externalSignal?.addEventListener('abort', onExternalAbort, { once: true })

  const timer = setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)

  try {
    const response = await fetch(`${API_BASE}${API_PREFIX}${path}`, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        'X-Request-ID': requestId(),
        ...init.headers,
      },
      signal: controller.signal,
    })

    // 注意：response.text()/json() 必须在同一个 try 内，超时与外部取消
    // 要继续覆盖 body 读取/解析阶段，不能只包住 fetch 的 headers 阶段。
    const text = await response.text()
    if (timedOut) {
      throw new ApiClientError(0, 'CLIENT_TIMEOUT', '请求等待时间过长，请稍后重试。')
    }
    if (externalAborted) {
      throw new ApiClientError(0, 'CLIENT_ABORTED', '请求已取消。')
    }

    let body: unknown = null
    if (text) {
      try {
        body = JSON.parse(text)
      } catch {
        // 保持既有行为：非 JSON 响应体按 null 处理，由 status 决定是否报错。
        body = null
      }
    }
    // JSON.parse 是同步操作；若极端情况下在解析期间超时/取消，也在返回前归一化。
    if (timedOut) {
      throw new ApiClientError(0, 'CLIENT_TIMEOUT', '请求等待时间过长，请稍后重试。')
    }
    if (externalAborted) {
      throw new ApiClientError(0, 'CLIENT_ABORTED', '请求已取消。')
    }

    if (!response.ok) {
      const payload = body as { request_id?: string; error?: { code?: string; message?: string } } | null
      throw new ApiClientError(
        response.status,
        payload?.error?.code || 'HTTP_ERROR',
        payload?.error?.message || '服务暂时不可用，请稍后重试。',
        payload?.request_id || response.headers.get('X-Request-ID') || undefined,
      )
    }
    return body as T
  } catch (error) {
    if (error instanceof ApiClientError) throw error
    if (timedOut) {
      throw new ApiClientError(0, 'CLIENT_TIMEOUT', '请求等待时间过长，请稍后重试。')
    }
    if (externalAborted || (error instanceof DOMException && error.name === 'AbortError')) {
      throw new ApiClientError(0, 'CLIENT_ABORTED', '请求已取消。')
    }
    throw new ApiClientError(0, 'NETWORK_ERROR', '无法连接分类服务，请确认后端已启动。')
  } finally {
    clearTimeout(timer)
    externalSignal?.removeEventListener('abort', onExternalAbort)
  }
}
