/** F4：全局 toast 存储（最多同时 3 条，相同错误去重）。 */
import { readonly, ref } from 'vue'

export type ToastKind = 'info' | 'success' | 'warning' | 'error'

export interface ToastInput {
  kind: ToastKind
  message: string
  detail?: string
  requestId?: string
  /** 默认 6000ms；0 表示不自动关闭（仍可手动关闭）。 */
  durationMs?: number
}

export interface ToastItem extends ToastInput {
  id: number
}

export const MAX_TOASTS = 3
const DEFAULT_DURATION_MS = 6000
const items = ref<ToastItem[]>([])
const timers = new Map<number, ReturnType<typeof setTimeout>>()
let nextId = 1

function clearTimer(id: number): void {
  const timer = timers.get(id)
  if (timer !== undefined) {
    clearTimeout(timer)
    timers.delete(id)
  }
}

function scheduleDismiss(id: number, durationMs: number | undefined): void {
  clearTimer(id)
  const duration = durationMs ?? DEFAULT_DURATION_MS
  if (duration <= 0) return
  timers.set(
    id,
    setTimeout(() => dismissToast(id), duration),
  )
}

export function dismissToast(id: number): void {
  clearTimer(id)
  items.value = items.value.filter((item) => item.id !== id)
}

export function clearToasts(): void {
  items.value.forEach((item) => clearTimer(item.id))
  items.value = []
}

/** 添加一条 toast；相同 message+requestId 视为重复，只刷新生命周期。 */
export function pushToast(input: ToastInput): number {
  const duplicate = items.value.find(
    (item) => item.message === input.message && item.requestId === input.requestId,
  )
  if (duplicate) {
    scheduleDismiss(duplicate.id, input.durationMs)
    return duplicate.id
  }

  const item: ToastItem = { ...input, id: nextId++ }
  items.value = [...items.value, item]
  while (items.value.length > MAX_TOASTS) {
    const oldest = items.value[0]
    if (!oldest || oldest.id === item.id) break
    dismissToast(oldest.id)
  }
  scheduleDismiss(item.id, item.durationMs)
  return item.id
}

export function useToasts() {
  return {
    toasts: readonly(items),
    pushToast,
    dismissToast,
    clearToasts,
    MAX_TOASTS,
  }
}
