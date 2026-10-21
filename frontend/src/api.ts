import type {
  ClassifyResponse,
  CompareResponse,
  ErrorResponse,
  ModelsResponse,
} from './types'

const API_BASE = (import.meta.env.VITE_API_BASE_URL || 'http://192.168.12.54:8000').replace(/\/$/, '')
const API_PREFIX = '/api/v1'

export class ApiClientError extends Error {
  readonly status: number
  readonly code: string
  readonly requestId?: string

  constructor(status: number, code: string, message: string, requestId?: string) {
    super(message)
    this.name = 'ApiClientError'
    this.status = status
    this.code = code
    this.requestId = requestId
  }
}

function requestId(): string {
  // crypto.randomUUID() 只在“安全上下文”（HTTPS 或 localhost）可用。
  // 局域网通常通过 http://192.168.12.54:5173 访问，属于非安全上下文，
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

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response
  try {
    response = await fetch(`${API_BASE}${API_PREFIX}${path}`, {
      ...init,
      headers: {
        'Content-Type': 'application/json',
        'X-Request-ID': requestId(),
        ...init?.headers,
      },
      signal: init?.signal ?? AbortSignal.timeout(45_000),
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === 'TimeoutError') {
      throw new ApiClientError(0, 'CLIENT_TIMEOUT', '请求等待时间过长，请稍后重试。')
    }
    throw new ApiClientError(0, 'NETWORK_ERROR', '无法连接分类服务，请确认后端已启动。')
  }

  const body = await response.json().catch(() => null) as unknown
  if (!response.ok) {
    const payload = body as ErrorResponse | null
    throw new ApiClientError(
      response.status,
      payload?.error?.code || 'HTTP_ERROR',
      payload?.error?.message || '服务暂时不可用，请稍后重试。',
      payload?.request_id || response.headers.get('X-Request-ID') || undefined,
    )
  }
  return body as T
}

export function getModels(signal?: AbortSignal): Promise<ModelsResponse> {
  return request<ModelsResponse>('/models', { method: 'GET', signal })
}

export function classify(title: string, model: string, signal?: AbortSignal): Promise<ClassifyResponse> {
  return request<ClassifyResponse>('/classify', {
    method: 'POST',
    body: JSON.stringify({ title, model }),
    signal,
  })
}

export function compare(title: string, models: string[], signal?: AbortSignal): Promise<CompareResponse> {
  return request<CompareResponse>('/compare', {
    method: 'POST',
    body: JSON.stringify({ title, models }),
    signal,
  })
}

export function getApiBase(): string {
  return API_BASE
}
