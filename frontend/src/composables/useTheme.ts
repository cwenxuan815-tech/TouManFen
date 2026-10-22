/** F13：亮/暗主题。默认跟随 prefers-color-scheme；手动选择写入 localStorage。 */
import { computed, ref } from 'vue'

export type ThemeMode = 'light' | 'dark' | 'system'
export type ResolvedTheme = 'light' | 'dark'

const STORAGE_KEY = 'tmf.theme'
const mode = ref<ThemeMode>('system')
const systemDark = ref(false)
let mediaQuery: MediaQueryList | null = null
let initialized = false

function readStoredMode(): ThemeMode {
  try {
    const stored = window.localStorage.getItem(STORAGE_KEY)
    if (stored === 'light' || stored === 'dark') return stored
  } catch {
    // localStorage 不可用时仍使用系统默认
  }
  return 'system'
}

function applyTheme(): void {
  if (typeof document === 'undefined') return
  const dark = mode.value === 'dark' || (mode.value === 'system' && systemDark.value)
  document.documentElement.dataset.theme = dark ? 'dark' : 'light'
  document.documentElement.style.colorScheme = dark ? 'dark' : 'light'
}

export function initTheme(): void {
  if (initialized || typeof window === 'undefined') return
  initialized = true
  mode.value = readStoredMode()
  mediaQuery = window.matchMedia('(prefers-color-scheme: dark)')
  systemDark.value = mediaQuery.matches
  mediaQuery.addEventListener('change', (event) => {
    systemDark.value = event.matches
    if (mode.value === 'system') applyTheme()
  })
  applyTheme()
}

export function useTheme() {
  initTheme()
  const resolvedTheme = computed<ResolvedTheme>(() =>
    mode.value === 'dark' || (mode.value === 'system' && systemDark.value) ? 'dark' : 'light',
  )
  const isDark = computed(() => resolvedTheme.value === 'dark')
  const label = computed(() => (isDark.value ? '切换到亮色' : '切换到暗色'))

  function setMode(next: ThemeMode): void {
    mode.value = next
    try {
      if (next === 'system') window.localStorage.removeItem(STORAGE_KEY)
      else window.localStorage.setItem(STORAGE_KEY, next)
    } catch {
      // 隐私模式等场景下不影响当前会话主题
    }
    applyTheme()
  }

  function toggle(): void {
    setMode(isDark.value ? 'light' : 'dark')
  }

  return { mode, resolvedTheme, isDark, label, setMode, toggle }
}
