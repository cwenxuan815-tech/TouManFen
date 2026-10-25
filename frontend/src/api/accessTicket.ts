/**
 * 阶段 5C：opaque access ticket 的前端存储与失效通知。
 *
 * 边界：
 * - 只保存 opaque ticket，不保存 IP；
 * - 不写入 URL、控制台、错误消息或遥测；
 * - localStorage 支持同源多标签共享；不可用时退化为 sessionStorage / 内存；
 * - 前端存储不是安全边界，服务端仍执行 ticket + IP 绑定。
 */

export const ACCESS_TICKET_HEADER = 'X-Access-Ticket'
export const ACCESS_TICKET_STORAGE_KEY = 'tmf.access.ticket'

let memoryTicket: string | null = null
let accessErrorHandler: ((code: string) => void) | null = null

function localStore(): Storage | null {
  try {
    return typeof globalThis.localStorage === 'undefined' ? null : globalThis.localStorage
  } catch {
    return null
  }
}

function sessionStore(): Storage | null {
  try {
    return typeof globalThis.sessionStorage === 'undefined' ? null : globalThis.sessionStorage
  } catch {
    return null
  }
}

function normalize(value: string | null): string | null {
  const text = (value || '').trim()
  return text || null
}

/** 读取已保存 ticket；优先 localStorage，再 sessionStorage，最后内存。 */
export function readAccessTicket(): string | null {
  if (memoryTicket) return memoryTicket
  for (const storage of [localStore(), sessionStore()]) {
    if (!storage) continue
    try {
      const value = normalize(storage.getItem(ACCESS_TICKET_STORAGE_KEY))
      if (value) {
        memoryTicket = value
        return value
      }
    } catch {
      // 隐私模式/配额：继续尝试下一个存储。
    }
  }
  return null
}

/** 保存 opaque ticket；尽量写 localStorage 以支持同源多标签。 */
export function saveAccessTicket(ticket: string): void {
  const value = normalize(ticket)
  if (!value) return
  memoryTicket = value
  for (const storage of [localStore(), sessionStore()]) {
    if (!storage) continue
    try {
      storage.setItem(ACCESS_TICKET_STORAGE_KEY, value)
    } catch {
      // storage 不可用时保留内存态；刷新会重新 enter。
    }
  }
}

/** 清理所有存储与内存 ticket。 */
export function clearAccessTicket(): void {
  memoryTicket = null
  for (const storage of [localStore(), sessionStore()]) {
    if (!storage) continue
    try {
      storage.removeItem(ACCESS_TICKET_STORAGE_KEY)
    } catch {
      // ignore
    }
  }
}

/** 供统一 HTTP client 注入业务请求头。 */
export function getAccessTicket(): string | null {
  return memoryTicket || readAccessTicket()
}

/** 业务 API 返回 ACCESS_* 时通知全局 useAccess（access API 自身不应触发）。 */
export function setAccessErrorHandler(handler: ((code: string) => void) | null): void {
  accessErrorHandler = handler
}

export function notifyAccessError(code: string): void {
  accessErrorHandler?.(code)
}
