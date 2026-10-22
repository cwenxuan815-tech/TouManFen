/**
 * F10 验收 harness（仅测试用，不进入 src/）。
 *
 * 挂载真实的 `ArenaPage`（内部持有 useArena + ArenaAnonymousCard + ArenaLeaderboard）
 * 与 F4 `Toaster`，通过 `window.__f10` 暴露 DOM 快照/点击/本地存储读取，
 * 由 run-scenarios.mjs 用 Edge headless + CDP 驱动并断言。
 */
import { createApp, h } from 'vue'
import ArenaPage from '../../src/components/placeholders/ArenaPage.vue'
import Toaster from '../../src/components/common/Toaster.vue'
import '../../src/styles/tokens.css'
import '../../src/styles/base.css'
import '../../src/style.css'

function clean(text: string | null | undefined): string {
  return (text || '').replace(/\s+/g, ' ').trim()
}

interface CardSnapshot {
  letter: string | null
  anonId: string | null
  state: string | null
  revealedModelId: string | null
  hasBackFace: boolean
  text: string
  animationName: string
  animationDuration: string
  transitionDuration: string
  disabled: boolean
}

interface BoardRowSnapshot {
  modelId: string | null
  displayName: string | null
  votes: number
  elo: number
  rank: string | null
  wilsonLower: number
  wilsonUpper: number
  warmingUp: boolean
  text: string
}

interface BoardSnapshot {
  present: boolean
  status: string | null
  available: string | null
  hasPlaceholder: boolean
  hasError: boolean
  loading: boolean
  statusText: string
  ruleText: string
  disclaimer: string
  headers: string[]
  rows: BoardRowSnapshot[]
  note: string
  text: string
}

interface DomSnapshot {
  pageState: string | null
  cards: CardSnapshot[]
  candidatesText: string
  bodyText: string
  toastMessages: string[]
  toastDetails: string[]
  hasLoading: boolean
  hasEmpty: boolean
  emptyText: string
  hasLoadError: boolean
  loadErrorText: string
  hasPartial: boolean
  revealedNote: string
  itemId: string | null
  board: BoardSnapshot
  reducedMotion: boolean
}

function cardSnapshots(): CardSnapshot[] {
  return Array.from(document.querySelectorAll('[data-arena-card]')).map((element) => {
    const inner = element.querySelector('.arena-card-inner')
    const style = inner ? getComputedStyle(inner) : null
    return {
      letter: element.getAttribute('data-anon-letter'),
      anonId: element.getAttribute('data-anon-id'),
      state: element.getAttribute('data-card-state'),
      revealedModelId: element.getAttribute('data-revealed-model-id'),
      hasBackFace: Boolean(element.querySelector('[data-arena-reveal]')),
      text: clean(element.textContent),
      animationName: style ? style.animationName : '',
      animationDuration: style ? style.animationDuration : '',
      transitionDuration: style ? style.transitionDuration : '',
      disabled: Boolean((element as HTMLButtonElement).disabled),
    }
  })
}

function boardSnapshot(): BoardSnapshot {
  const board = document.querySelector('[data-arena-leaderboard]')
  const rows = Array.from(document.querySelectorAll('[data-arena-entry]')).map((element) => ({
    modelId: element.getAttribute('data-model-id'),
    displayName: element.getAttribute('data-display-name'),
    votes: Number(element.getAttribute('data-votes') ?? 0),
    elo: Number(element.getAttribute('data-elo') ?? 0),
    rank: element.getAttribute('data-rank'),
    wilsonLower: Number(element.getAttribute('data-wilson-lower') ?? 0),
    wilsonUpper: Number(element.getAttribute('data-wilson-upper') ?? 0),
    warmingUp: element.getAttribute('data-warming-up') === 'true',
    text: clean(element.textContent),
  }))
  return {
    present: Boolean(board),
    status: board?.getAttribute('data-arena-board-status') ?? null,
    available: board?.getAttribute('data-arena-board-available') ?? null,
    hasPlaceholder: Boolean(document.querySelector('[data-arena-board-placeholder]')),
    hasError: Boolean(document.querySelector('[data-arena-board-error]')),
    loading: Boolean(document.querySelector('[data-arena-leaderboard] .state-loading')),
    statusText: clean(document.querySelector('[data-arena-leaderboard] .arena-board-badge')?.textContent),
    ruleText: clean(document.querySelector('[data-arena-board-rule]')?.textContent),
    disclaimer: clean(document.querySelector('[data-arena-disclaimer]')?.textContent),
    headers: Array.from(document.querySelectorAll('[data-arena-leaderboard] th')).map((header) =>
      clean(header.textContent),
    ),
    rows,
    note: clean(document.querySelector('[data-arena-board-note]')?.textContent),
    text: clean(board?.textContent),
  }
}

function domSnapshot(): DomSnapshot {
  const candidates = document.querySelector('[data-arena-candidates]')
  return {
    pageState:
      document.querySelector('[data-arena-page-state]')?.getAttribute('data-arena-page-state') ?? null,
    cards: cardSnapshots(),
    candidatesText: clean(candidates?.textContent),
    bodyText: clean(document.body.textContent),
    toastMessages: Array.from(document.querySelectorAll('.toast-copy b')).map((element) =>
      clean(element.textContent),
    ),
    toastDetails: Array.from(document.querySelectorAll('.toast-copy small')).map((element) =>
      clean(element.textContent),
    ),
    hasLoading: Boolean(document.querySelector('[data-arena-loading]')),
    hasEmpty: Boolean(document.querySelector('[data-arena-empty]')),
    emptyText: clean(document.querySelector('[data-arena-empty]')?.textContent),
    hasLoadError: Boolean(document.querySelector('[data-arena-load-error]')),
    loadErrorText: clean(document.querySelector('[data-arena-load-error]')?.textContent),
    hasPartial: Boolean(document.querySelector('.async-state.state-partial')),
    revealedNote: clean(document.querySelector('[data-arena-revealed]')?.textContent),
    itemId: candidates?.getAttribute('data-arena-item-id') ?? null,
    board: boardSnapshot(),
    reducedMotion: window.matchMedia('(prefers-reduced-motion: reduce)').matches,
  }
}

const api = {
  ready: false,
  dom: domSnapshot,
  clickCard(letter: string): boolean {
    const element = document.querySelector<HTMLElement>(`[data-anon-letter="${letter}"]`)
    if (!element) return false
    element.click()
    return true
  },
  clickNext(): boolean {
    const element = document.querySelector<HTMLElement>('[data-arena-next]')
    if (!element) return false
    element.click()
    return true
  },
  clickEmptyRetry(): boolean {
    const element = document.querySelector<HTMLElement>('[data-arena-empty-retry]')
    if (!element) return false
    element.click()
    return true
  },
  clickBoardRetry(): boolean {
    const element = document.querySelector<HTMLElement>('[data-arena-board-error] .state-retry')
    if (!element) return false
    element.click()
    return true
  },
  clientId(): string | null {
    try {
      return localStorage.getItem('tmf.arena.client_id')
    } catch {
      return null
    }
  },
  storageAvailable(): boolean {
    try {
      return typeof localStorage !== 'undefined'
    } catch {
      return false
    }
  },
}

declare global {
  interface Window {
    __f10: typeof api
  }
}

window.__f10 = api

const Root = {
  setup() {
    return () =>
      h('div', { class: 'f10-harness' }, [h(ArenaPage as never), h(Toaster as never)])
  },
}

const app = createApp(Root)
app.mount('#app')
api.ready = true
