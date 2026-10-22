/** F13：大屏演示模式（字号放大、隐藏调试信息、可选全屏）。 */
import { ref } from 'vue'

const STORAGE_KEY = 'tmf.presentation.large'
const largeScreen = ref(false)
const fullscreenNote = ref('')
let initialized = false

function applyPresentation(): void {
  if (typeof document === 'undefined') return
  document.documentElement.dataset.presentation = largeScreen.value ? 'large' : 'normal'
}

function readStored(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === '1'
  } catch {
    return false
  }
}

export function initPresentation(): void {
  if (initialized || typeof window === 'undefined') return
  initialized = true
  largeScreen.value = readStored()
  applyPresentation()
}

export function useLargeScreen() {
  initPresentation()

  async function setLarge(next: boolean): Promise<void> {
    largeScreen.value = next
    fullscreenNote.value = ''
    applyPresentation()
    try {
      window.localStorage.setItem(STORAGE_KEY, next ? '1' : '0')
    } catch {
      // 不阻塞大屏布局
    }
    if (typeof document === 'undefined') return
    if (next) {
      const root = document.documentElement as HTMLElement & {
        requestFullscreen?: () => Promise<void>
      }
      if (typeof root.requestFullscreen === 'function') {
        try {
          await root.requestFullscreen()
        } catch {
          // 用户拒绝或浏览器不允许：布局仍可用，不抛错
          fullscreenNote.value = '浏览器未允许全屏；大屏布局仍可使用'
        }
      } else {
        fullscreenNote.value = '当前浏览器不支持全屏；大屏布局仍可使用'
      }
    } else if (document.fullscreenElement) {
      try {
        await document.exitFullscreen()
      } catch {
        // 退出失败不影响布局
      }
    }
  }

  async function toggle(): Promise<void> {
    await setLarge(!largeScreen.value)
  }

  return { largeScreen, fullscreenNote, setLarge, toggle }
}
