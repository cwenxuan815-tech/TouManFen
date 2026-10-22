/**
 * F16：安全上下文检测。
 *
 * 只以浏览器官方 `window.isSecureContext` 为准，不自行按 hostname/端口推断。
 */
import { ref } from 'vue'

export function useSecureContext() {
  const isSecureContext = ref(
    typeof window !== 'undefined' && Boolean(window.isSecureContext),
  )

  function refresh(): boolean {
    isSecureContext.value =
      typeof window !== 'undefined' && Boolean(window.isSecureContext)
    return isSecureContext.value
  }

  return { isSecureContext, refresh }
}
