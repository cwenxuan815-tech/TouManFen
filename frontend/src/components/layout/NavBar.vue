<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useTheme } from '../../composables/useTheme'
import { useLargeScreen } from '../../composables/useLargeScreen'

defineProps<{
  loading: boolean
  hasError: boolean
}>()

const mobileMenuOpen = ref(false)
const menuToggleRef = ref<HTMLButtonElement | null>(null)
type ActiveNav = 'lab' | 'models' | 'arena' | 'bench' | 'about'

function resolveActiveNav(): ActiveNav {
  if (typeof window === 'undefined') return 'lab'
  switch (window.location.hash) {
    case '#models': return 'models'
    case '#arena': return 'arena'
    case '#bench': return 'bench'
    case '#about': return 'about'
    default: return 'lab'
  }
}

const activeNav = ref<ActiveNav>(resolveActiveNav())
function syncActiveNav(): void {
  activeNav.value = resolveActiveNav()
}

const theme = useTheme()
const presentation = useLargeScreen()
const themeLabel = computed(() => (theme.isDark.value ? '亮色' : '暗色'))
const themeTitle = computed(() => (theme.isDark.value ? '切换到亮色主题' : '切换到暗色主题'))
const largeLabel = computed(() => (presentation.largeScreen.value ? '退出大屏' : '大屏'))
const largeTitle = computed(() =>
  presentation.largeScreen.value ? '退出大屏演示模式' : '进入大屏演示模式，并尝试全屏',
)

function closeMobileMenu(): void {
  mobileMenuOpen.value = false
}

/** F14：Esc 关闭移动菜单，并把焦点交还给触发按钮（键盘用户不迷路）。 */
function closeMobileMenuAndRestoreFocus(): void {
  if (!mobileMenuOpen.value) return
  mobileMenuOpen.value = false
  menuToggleRef.value?.focus()
}

function onNavKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Escape') return
  if (!mobileMenuOpen.value) return
  event.preventDefault()
  event.stopPropagation()
  closeMobileMenuAndRestoreFocus()
}

onMounted(() => window.addEventListener('hashchange', syncActiveNav))
onUnmounted(() => window.removeEventListener('hashchange', syncActiveNav))
</script>

<template>
  <header class="topbar" @keydown="onNavKeydown">
    <a class="brand" href="#top" aria-label="投满分首页">
      <span class="brand-mark">T<span class="brand-mark-dot">·</span></span>
      <span class="brand-name">投满分 <small>TMF</small></span>
    </a>

    <button
      ref="menuToggleRef"
      class="menu-toggle"
      type="button"
      :aria-expanded="mobileMenuOpen"
      aria-controls="primary-nav"
      aria-label="切换导航"
      @click="mobileMenuOpen = !mobileMenuOpen"
    >
      <span></span><span></span>
    </button>

    <nav id="primary-nav" class="nav-links" :class="{ 'nav-open': mobileMenuOpen }" aria-label="主导航">
      <a :class="{ 'nav-active': activeNav === 'lab' }" :aria-current="activeNav === 'lab' ? 'page' : undefined" href="#lab" @click="closeMobileMenu">分类实验台</a>
      <a :class="{ 'nav-active': activeNav === 'models' }" :aria-current="activeNav === 'models' ? 'page' : undefined" href="#models" @click="closeMobileMenu">模型矩阵</a>
      <a :class="{ 'nav-active': activeNav === 'arena' }" :aria-current="activeNav === 'arena' ? 'page' : undefined" href="#arena" @click="closeMobileMenu">竞技场</a>
      <a :class="{ 'nav-active': activeNav === 'bench' }" :aria-current="activeNav === 'bench' ? 'page' : undefined" href="#bench" @click="closeMobileMenu">PK 榜单</a>
      <a href="http://192.168.12.82:8000/#image" @click="closeMobileMenu">OCR 识别</a>
      <a href="http://192.168.12.54:8080/" @click="closeMobileMenu">微博热搜</a>
      <a :class="{ 'nav-active': activeNav === 'about' }" :aria-current="activeNav === 'about' ? 'page' : undefined" href="#about" @click="closeMobileMenu">关于 TMF</a>
    </nav>

    <div class="topbar-right">
      <div class="display-controls">
        <button
          type="button"
          class="display-toggle"
          :aria-label="themeTitle"
          :title="themeTitle"
          @click="theme.toggle()"
        >
          {{ themeLabel }}
        </button>
        <button
          type="button"
          class="display-toggle"
          :class="{ 'is-on': presentation.largeScreen.value }"
          :aria-pressed="presentation.largeScreen.value"
          :aria-label="largeTitle"
          :title="largeTitle"
          @click="presentation.toggle()"
        >
          {{ largeLabel }}
        </button>
      </div>
      <span class="api-status" :class="{ 'is-online': !hasError && !loading, 'is-loading': loading }">
        <i></i>{{ loading ? '连接中' : hasError ? '后端离线' : '实验环境' }}
      </span>
      <a class="top-cta" href="#lab">开始体验 <span>↗</span></a>
    </div>
  </header>
</template>

<style scoped>
  .topbar {
    height: 76px;
    max-width: var(--layout-wide);
    margin: 0 auto;
    padding: 0 6.1%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    position: relative;
    z-index: 20;
  }

  .nav-links {
    display: flex;
    align-items: center;
    gap: 38px;
    margin-left: var(--space-30);
  }

  .nav-links a {
    font-size: 15px;
    color: #747c8a;
    transition: color var(--duration-fast);
  }

  .nav-links a:hover,.nav-links .nav-active {
    color: var(--text-strong);
  }

  .nav-links .nav-active {
    font-weight: 700;
  }

  .topbar-right {
    display: flex;
    align-items: center;
    gap: 22px;
  }

  .display-controls {
    display: flex;
    align-items: center;
    gap: 6px;
  }

  .display-toggle {
    height: 28px;
    padding: 0 9px;
    border: 1px solid var(--border-control);
    border-radius: var(--radius-pill);
    background: var(--surface-card);
    color: var(--text-secondary);
    font: inherit;
    font-size: 10px;
    line-height: 1;
    white-space: nowrap;
  }

  .display-toggle:hover,
  .display-toggle.is-on {
    border-color: var(--brand-light);
    color: var(--brand);
  }

  .api-status {
    display: inline-flex;
    align-items: center;
    gap: var(--space-8);
    font-size: 12px;
    color: #8c94a2;
  }

  .api-status i {
    width: 7px;
    height: 7px;
    border-radius: var(--radius-circle);
    background: #b2bac7;
  }

  .api-status.is-online i {
    background: #38b47e;
    box-shadow: var(--ring-success);
  }

  .api-status.is-loading i {
    background: #f4b740;
    animation: pulse var(--duration-pulse) infinite;
  }

  .top-cta {
    height: 38px;
    padding: 0 var(--space-16);
    border: 1px solid #dfe4eb;
    border-radius: var(--radius-pill);
    display: flex;
    align-items: center;
    gap: var(--space-12);
    font-size: 13px;
    font-weight: 600;
    transition: all var(--duration-fast);
  }

  .top-cta:hover {
    border-color: var(--brand);
    color: var(--brand);
  }

  .top-cta span {
    font-size: 18px;
    color: var(--brand);
  }

  .menu-toggle {
    display: none;
    border: 0;
    background: transparent;
    padding: var(--space-8);
  }

  /* F14：减少动效时停掉状态点脉冲与颜色过渡。 */
  @media (prefers-reduced-motion: reduce) {
    .api-status.is-loading i {
      animation: none;
    }
    .nav-links a,
    .display-toggle,
    .top-cta {
      transition: none;
    }
  }

  .menu-toggle span {
    display: block;
    width: 20px;
    height: 1.5px;
    background: var(--text-strong);
    margin: var(--space-5);
  }

  @media(max-width:760px) {
    .topbar {
      height: 65px;
      padding: 0 5%;
    }
    .menu-toggle {
      display: block;
      margin-left: auto;
      margin-right: var(--space-14);
    }
    .nav-links {
      display: none;
      position: absolute;
      top: 58px;
      left: 4%;
      right: 4%;
      margin: 0;
      padding: var(--space-12);
      background: #ffffffef;
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      box-shadow: 0 16px 36px #1e2f4d19;
      flex-direction: column;
      align-items: stretch;
      gap: 0;
    }
    .nav-links.nav-open {
      display: flex;
    }
    .nav-links a {
      padding: var(--space-12) var(--space-10);
    }
    .topbar-right {
      gap: var(--space-10);
    }
    .display-controls {
      gap: 4px;
    }
    .display-toggle {
      height: 26px;
      padding: 0 6px;
      font-size: 9px;
    }
    .api-status {
      font-size: 0;
    }
    .api-status i {
      width: 8px;
      height: 8px;
    }
    .top-cta {
      height: 34px;
      padding: 0 var(--space-12);
      font-size: 11px;
      white-space: nowrap;
    }
  }

  @media(max-width:390px) {
    .topbar-right {
      gap: var(--space-7);
    }
    .top-cta {
      padding: 0 var(--space-9);
    }
  }
</style>
