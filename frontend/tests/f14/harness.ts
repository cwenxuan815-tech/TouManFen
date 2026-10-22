/**
 * F14 验收 harness（仅测试用，不进入 src/）。
 *
 * `?mode=app`    → 挂载真实 App（键盘-only 完整流程、skip link、移动菜单、reduced-motion）。
 * `?mode=cards50`→ 挂载 50 张真实 ResultCard（50 卡仍可交互的临时压力页）。
 *
 * `window.__f14` 暴露焦点/焦点环/aria-live/输入延迟等测量钩子，
 * 由 run-scenarios.mjs 用 Edge headless + CDP 驱动并断言。
 */
import { createApp, h, reactive } from 'vue'
import App from '../../src/App.vue'
import ResultCard from '../../src/components/result/ResultCard.vue'
import Toaster from '../../src/components/common/Toaster.vue'
import type { ClassifyResponse, ModelInfo } from '../../src/types'
import '../../src/styles/tokens.css'
import '../../src/styles/base.css'
import '../../src/style.css'

const mode =
  new URLSearchParams(window.location.search).get('mode') === 'cards50' ? 'cards50' : 'app'

const models: ModelInfo[] = [
  { model_id: 'rf', display_name: '随机森林', enabled: true, model_version: 'rf.pkl', status: 'up' },
  { model_id: 'fasttext', display_name: 'fastText', enabled: true, model_version: 'ft.pkl', status: 'up' },
  { model_id: 'bert', display_name: 'BERT', enabled: true, model_version: 'bert.pkl', status: 'up' },
  { model_id: 'distill', display_name: '蒸馏模型', enabled: true, model_version: 'distill.pkl', status: 'up' },
  { model_id: 'llm', display_name: 'LLM', enabled: true, model_version: null, status: 'up' },
]

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'textarea:not([disabled])',
  'select:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',')

function cleanText(value: string | null | undefined): string {
  return (value || '').replace(/\s+/g, ' ').trim()
}

function isVisible(element: Element): boolean {
  const rect = element.getBoundingClientRect()
  if (rect.width <= 0 || rect.height <= 0) return false
  const style = getComputedStyle(element)
  return style.visibility !== 'hidden' && style.display !== 'none'
}

function pxNumber(value: string): number {
  return Number.parseFloat(value) || 0
}

interface RingInfo {
  outlineWidth: number
  outlineStyle: string
  outlineColor: string
  outlineOffset: number
  clipped: boolean
  clippingAncestors: string[]
  inViewport: boolean
  rect: { x: number; y: number; width: number; height: number }
}

function ringInfo(element: Element): RingInfo {
  const style = getComputedStyle(element)
  const width = pxNumber(style.outlineWidth)
  const offset = pxNumber(style.outlineOffset)
  const rect = element.getBoundingClientRect()
  const position = style.position
  const expand = width + offset + 1
  const expanded = {
    left: rect.left - expand,
    top: rect.top - expand,
    right: rect.right + expand,
    bottom: rect.bottom + expand,
  }
  const clippingAncestors: string[] = []
  let clipped = false
  if (position !== 'fixed') {
    let ancestor: Element | null = element.parentElement
    while (ancestor && ancestor !== document.documentElement) {
      const ancestorStyle = getComputedStyle(ancestor)
      const clipsX =
        ancestorStyle.overflowX === 'hidden' ||
        ancestorStyle.overflowX === 'clip' ||
        ancestorStyle.overflowX === 'auto' ||
        ancestorStyle.overflowX === 'scroll'
      const clipsY =
        ancestorStyle.overflowY === 'hidden' ||
        ancestorStyle.overflowY === 'clip' ||
        ancestorStyle.overflowY === 'auto' ||
        ancestorStyle.overflowY === 'scroll'
      if (clipsX || clipsY) {
        const box = ancestor.getBoundingClientRect()
        if (
          (clipsX && (expanded.left < box.left - 0.5 || expanded.right > box.right + 0.5)) ||
          (clipsY && (expanded.top < box.top - 0.5 || expanded.bottom > box.bottom + 0.5))
        ) {
          clipped = true
          clippingAncestors.push(`${ancestor.tagName.toLowerCase()}.${ancestor.className || ''}`)
        }
      }
      ancestor = ancestor.parentElement
    }
  }
  return {
    outlineWidth: width,
    outlineStyle: style.outlineStyle,
    outlineColor: style.outlineColor,
    outlineOffset: offset,
    clipped,
    clippingAncestors,
    inViewport:
      rect.left >= 0 &&
      rect.top >= 0 &&
      rect.right <= window.innerWidth + 0.5 &&
      rect.bottom <= window.innerHeight + 0.5,
    rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
  }
}

function activeSnapshot(): Record<string, unknown> | null {
  const element = document.activeElement
  if (!element || element === document.body) {
    return { tag: 'body', id: '', cls: '', text: '', href: null, focusVisible: false }
  }
  const ring = ringInfo(element)
  let focusVisible = false
  try {
    focusVisible = element.matches(':focus-visible')
  } catch {
    focusVisible = false
  }
  return {
    tag: element.tagName.toLowerCase(),
    id: element.id || '',
    cls: typeof element.className === 'string' ? element.className : '',
    text: cleanText(element.textContent).slice(0, 80),
    href: element.getAttribute('href'),
    role: element.getAttribute('role'),
    ariaLabel: element.getAttribute('aria-label'),
    focusVisible,
    ...ring,
  }
}

function firstFocusables(limit = 20): Array<Record<string, unknown>> {
  const nodes = Array.from(document.querySelectorAll(FOCUSABLE_SELECTOR))
  const output: Array<Record<string, unknown>> = []
  for (const element of nodes) {
    if (!isVisible(element)) continue
    output.push({
      tag: element.tagName.toLowerCase(),
      id: element.id || '',
      cls: typeof element.className === 'string' ? element.className.slice(0, 120) : '',
      text: cleanText(element.textContent).slice(0, 60),
      href: element.getAttribute('href'),
      ariaLabel: element.getAttribute('aria-label'),
      tabindex: element.getAttribute('tabindex'),
      isSkipLink: element.hasAttribute('data-skip-link'),
    })
    if (output.length >= limit) break
  }
  return output
}

function liveRegions(): Array<Record<string, unknown>> {
  const nodes = Array.from(
    document.querySelectorAll('[aria-live], [role="status"], [role="alert"]'),
  )
  return nodes.map((element) => {
    const style = getComputedStyle(element)
    return {
      tag: element.tagName.toLowerCase(),
      cls: typeof element.className === 'string' ? element.className : '',
      role: element.getAttribute('role'),
      live: element.getAttribute('aria-live'),
      atomic: element.getAttribute('aria-atomic'),
      text: cleanText(element.textContent).slice(0, 200),
      srOnly: style.position === 'absolute' && pxNumber(style.width) <= 1,
    }
  })
}

function domSummary(): Record<string, unknown> {
  const skipLink = document.querySelector('[data-skip-link]')
  const main = document.querySelector('main')
  const resultSection = document.querySelector('.result-section')
  const menuToggle = document.querySelector('.menu-toggle')
  const nav = document.getElementById('primary-nav')
  const arena = document.querySelector('[data-arena-page-state]')
  const bench = document.querySelector('[data-page="bench"]')
  const toasts = Array.from(document.querySelectorAll('.toast'))
  return {
    skipLink: skipLink
      ? {
          href: skipLink.getAttribute('href'),
          text: cleanText(skipLink.textContent),
          first: firstFocusables(1)[0]?.isSkipLink === true,
        }
      : null,
    main: main
      ? {
          id: main.id,
          tabindex: main.getAttribute('tabindex'),
          targetExists: Boolean(document.getElementById('main-content')),
        }
      : null,
    active: activeSnapshot(),
    firstFocusables: firstFocusables(18),
    result: {
      exists: Boolean(resultSection),
      sectionAriaLive: resultSection?.getAttribute('aria-live') ?? null,
      heading: cleanText(document.querySelector('.result-heading h3')?.textContent),
      cards: document.querySelectorAll('[data-result-card]').length,
      liveText: cleanText(document.querySelector('[data-live="result"]')?.textContent),
    },
    arena: arena
      ? {
          state: arena.getAttribute('data-arena-page-state'),
          cards: document.querySelectorAll('[data-arena-card]').length,
          revealed: Boolean(document.querySelector('[data-arena-revealed]')),
        }
      : null,
    bench: bench
      ? {
          state: document.querySelector('.bench-body')?.getAttribute('data-state') ?? null,
          rows: document.querySelectorAll('[data-bench-key]').length,
          liveText: cleanText(document.querySelector('[data-live="bench"]')?.textContent),
        }
      : null,
    menu: {
      toggleVisible: menuToggle ? isVisible(menuToggle) : false,
      expanded: menuToggle?.getAttribute('aria-expanded') ?? null,
      controls: menuToggle?.getAttribute('aria-controls') ?? null,
      navVisible: nav ? isVisible(nav) : false,
      navLinks: Array.from(nav?.querySelectorAll('a') ?? []).map((anchor) => ({
        href: anchor.getAttribute('href'),
        visible: isVisible(anchor),
      })),
    },
    theme: document.documentElement.dataset.theme ?? null,
    reducedMotion: window.matchMedia('(prefers-reduced-motion: reduce)').matches,
    toasts: toasts.map((toast) => cleanText(toast.textContent).slice(0, 120)),
    hash: window.location.hash,
    liveRegions: liveRegions(),
    cardMode:
      document.querySelectorAll('[data-card-index]').length > 0
        ? {
            cards: document.querySelectorAll('[data-result-card]').length,
            indexes: document.querySelectorAll('[data-card-index]').length,
            feedbackControls: Array.from(document.querySelectorAll('.feedback-toggle')).map(
              (button) => button.getAttribute('aria-controls'),
            ),
            uniqueControls: new Set(
              Array.from(document.querySelectorAll('.feedback-toggle')).map(
                (button) => button.getAttribute('aria-controls'),
              ),
            ).size,
            panelsOpen: document.querySelectorAll('[data-feedback-panel]').length,
            scrollY: window.scrollY,
            docHeight: document.documentElement.scrollHeight,
          }
        : null,
  }
}

async function nextFrames(count = 2): Promise<void> {
  for (let index = 0; index < count; index += 1) {
    await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()))
  }
}

async function measureInputLatency(text: string): Promise<Record<string, unknown>> {
  const area = document.querySelector<HTMLTextAreaElement>('#news-title')
  if (!area) return { ok: false, ms: -1 }
  const counter = document.querySelector('.char-count')
  area.focus()
  const started = performance.now()
  area.value = text
  area.dispatchEvent(new Event('input', { bubbles: true }))
  await nextFrames(2)
  return {
    ok: true,
    ms: performance.now() - started,
    value: area.value,
    counter: cleanText(counter?.textContent),
    titleTrimmedLength: area.value.trim().length,
    submitting: Boolean(document.querySelector('.submit-button')),
  }
}

async function scrollCheck(): Promise<Record<string, unknown>> {
  window.scrollTo(0, Math.max(0, document.documentElement.scrollHeight - window.innerHeight))
  await nextFrames(2)
  const area = document.querySelector<HTMLTextAreaElement>('#news-title')
  return {
    scrollY: window.scrollY,
    innerHeight: window.innerHeight,
    docHeight: document.documentElement.scrollHeight,
    textareaStillPresent: Boolean(area),
  }
}

const cardsState = reactive({
  items: Array.from({ length: 50 }, (_, index) => ({
    id: index,
    title: `示例新闻标题 ${index + 1}`,
  })),
  updated: 0,
})

function singleFor(index: number): ClassifyResponse {
  return {
    request_id: `mock-card-${index}`,
    model_id: 'rf',
    model_version: 'rf.pkl',
    pred_class: 'finance',
    latency_ms: 10 + index,
    scores: { finance: 0.91, stocks: 0.06, realty: 0.02 },
    score_kind: 'raw_proba',
    top_k: [
      { class_id: 'finance', score: 0.91 },
      { class_id: 'stocks', score: 0.06 },
      { class_id: 'realty', score: 0.02 },
    ],
    data_source: 'mock',
  }
}

async function measureCardUpdate(): Promise<Record<string, unknown>> {
  const started = performance.now()
  const lastIndex = cardsState.items.length - 1
  cardsState.items[lastIndex]!.title = `更新后的标题 ${++cardsState.updated}`
  await nextFrames(2)
  const card = document.querySelector(`[data-card-index="${lastIndex}"]`)
  return {
    ms: performance.now() - started,
    count: document.querySelectorAll('[data-result-card]').length,
    lastText: cleanText(card?.querySelector('.result-context')?.textContent),
    scrollY: window.scrollY,
  }
}

function scrollToCard(index: number): Record<string, unknown> {
  const element = document.querySelector(`[data-card-index="${index}"]`)
  if (!element) return { ok: false }
  element.scrollIntoView({ block: 'end' })
  const toggle = element.querySelector<HTMLElement>('.feedback-toggle')
  toggle?.focus()
  const rect = element.getBoundingClientRect()
  return {
    ok: true,
    scrollY: window.scrollY,
    rect: { top: rect.top, bottom: rect.bottom },
    innerHeight: window.innerHeight,
  }
}

const api = {
  ready: false,
  mode,
  dom: domSummary,
  active: activeSnapshot,
  firstFocusables,
  liveRegions,
  ring: (selector: string) => {
    const element = document.querySelector(selector)
    if (!element) return null
    element.scrollIntoView({ block: 'center' })
    return ringInfo(element)
  },
  focusSelector(selector: string): Record<string, unknown> | null {
    const element = document.querySelector<HTMLElement>(selector)
    if (!element) return null
    element.scrollIntoView({ block: 'center' })
    element.focus()
    return activeSnapshot()
  },
  clickSelector(selector: string): boolean {
    const element = document.querySelector<HTMLElement>(selector)
    if (!element) return false
    element.click()
    return true
  },
  setHash(hash: string): void {
    window.location.hash = hash
  },
  measureInputLatency,
  scrollCheck,
  cardSummary(): Record<string, unknown> {
    const controls = Array.from(document.querySelectorAll('.feedback-toggle')).map(
      (button) => button.getAttribute('aria-controls'),
    )
    return {
      cards: document.querySelectorAll('[data-result-card]').length,
      indexes: document.querySelectorAll('[data-card-index]').length,
      controls,
      uniqueControls: new Set(controls).size,
      openPanels: document.querySelectorAll('[data-feedback-panel]').length,
      openPanelIds: Array.from(document.querySelectorAll('[data-feedback-panel]')).map(
        (panel) => panel.id,
      ),
      scrollY: window.scrollY,
      docHeight: document.documentElement.scrollHeight,
    }
  },
  measureCardUpdate,
  scrollToCard,
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
    __f14: typeof api
  }
}

window.__f14 = api

const CardsRoot = {
  setup() {
    return () =>
      h('div', { class: 'f14-cards-page', style: 'padding: 24px; max-width: 1080px; margin: 0 auto;' }, [
        h('h1', { style: 'font-size: 20px; margin-bottom: 16px' }, '50 张结果卡压力测试（临时 harness）'),
        ...cardsState.items.map((item) =>
          h('div', { key: item.id, 'data-card-index': item.id }, [
            h(ResultCard as never, {
              single: singleFor(item.id),
              title: item.title,
              models,
            }),
          ]),
        ),
        h(Toaster as never),
      ])
  },
}

const app = createApp(mode === 'cards50' ? CardsRoot : { setup: () => () => h(App as never) })
app.mount('#app')
api.ready = true
