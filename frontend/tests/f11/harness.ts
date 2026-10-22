/**
 * F11 验收 harness（仅测试用，不进入 src/）。
 *
 * 挂载真实的 `BenchPage`；`window.__f11` 暴露 DOM 快照与指标切换，
 * 由 run-scenarios.mjs 用 Edge headless + CDP 驱动并断言。
 */
import { createApp, h, ref } from 'vue'
import BenchPage from '../../src/components/placeholders/BenchPage.vue'
import Toaster from '../../src/components/common/Toaster.vue'
import '../../src/styles/tokens.css'
import '../../src/styles/base.css'
import '../../src/style.css'

interface MetaSnapshot {
  field: string | null
  raw: string | null
  display: string
}

interface RowSnapshot {
  key: string | null
  model: string | null
  kind: string | null
  derived: string | null
  status: string | null
  metric: string | null
  metricValue: string | null
  barWidth: string | null
  hasBar: boolean
  valueText: string
  missingText: string
  tag: string
  text: string
}

interface BenchDomSnapshot {
  state: string | null
  meta: MetaSnapshot[]
  activeMetric: string | null
  metricButtons: string[]
  metricHint: string
  rows: RowSnapshot[]
  rowCount: number
  oracleLine: { percent: string | null } | null
  errorText: string
  emptyText: string
  hasRetry: boolean
  text: string
}

function cleanText(value: string | null | undefined): string {
  return (value || '').replace(/\s+/g, ' ').trim()
}

function domSnapshot(): BenchDomSnapshot {
  const page = document.querySelector('[data-page="bench"]')
  const body = document.querySelector('.bench-body')
  const meta = Array.from(document.querySelectorAll('[data-meta-field]')).map((element) => ({
    field: element.getAttribute('data-meta-field'),
    raw: element.getAttribute('data-meta-value'),
    display: cleanText(element.querySelector('dd')?.textContent),
  }))
  const rows = Array.from(document.querySelectorAll('[data-bench-key]')).map((element) => {
    const bar = element.querySelector('[data-bar-width]')
    return {
      key: element.getAttribute('data-bench-key'),
      model: element.getAttribute('data-bench-model'),
      kind: element.getAttribute('data-bench-kind'),
      derived: element.getAttribute('data-bench-derived'),
      status: element.getAttribute('data-bench-status'),
      metric: element.getAttribute('data-metric'),
      metricValue: element.getAttribute('data-metric-value'),
      barWidth: bar ? bar.getAttribute('data-bar-width') : null,
      hasBar: Boolean(bar),
      valueText: cleanText(element.querySelector('[data-role="metric-value"]')?.textContent),
      missingText: cleanText(element.querySelector('[data-role="missing-label"]')?.textContent),
      tag: cleanText(element.querySelector('.row-derived-tag')?.textContent),
      text: cleanText(element.textContent),
    }
  })
  const oracleLine = document.querySelector('[data-role="oracle-line"]')
  return {
    state: body ? body.getAttribute('data-state') : null,
    meta,
    activeMetric: document.querySelector('.metric-button.active')?.getAttribute('data-metric') ?? null,
    metricButtons: Array.from(document.querySelectorAll('[data-metric]'))
      .filter((element) => element.classList.contains('metric-button'))
      .map((element) => element.getAttribute('data-metric') || ''),
    metricHint: cleanText(document.querySelector('[data-role="metric-hint"]')?.textContent),
    rows,
    rowCount: rows.length,
    oracleLine: oracleLine
      ? { percent: oracleLine.getAttribute('data-oracle-percent') }
      : null,
    errorText: cleanText(document.querySelector('.async-state.state-error')?.textContent),
    emptyText: cleanText(document.querySelector('.async-state.state-empty')?.textContent),
    hasRetry: Boolean(document.querySelector('.async-state.state-error .state-retry')),
    text: cleanText(document.body.textContent),
  }
}

const api = {
  ready: false,
  dom: domSnapshot,
  clickMetric(metric: 'macro_f1' | 'avg_latency_ms' | 'size_bytes'): void {
    const button = document.querySelector<HTMLElement>(`.metric-button[data-metric="${metric}"]`)
    button?.click()
  },
  hasBenchPage(): boolean {
    return Boolean(document.querySelector('[data-page="bench"]'))
  },
}

declare global {
  interface Window {
    __f11: typeof api
  }
}

window.__f11 = api

const stageRef = ref<{ reload?: () => Promise<void> } | null>(null)

const Root = {
  setup() {
    return () => h('div', { class: 'f11-harness' }, [h(BenchPage as never, { ref: stageRef as never }), h(Toaster as never)])
  },
}

const app = createApp(Root)
app.mount('#app')
api.ready = true
