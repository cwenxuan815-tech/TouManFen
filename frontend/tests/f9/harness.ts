/**
 * F9 验收 harness（仅测试用，不进入 src/）。
 *
 * 挂载真实的 `ResultStage`（内部持有 useCompareStream + CompareBoard），
 * 通过 `window.__f9` 暴露「开始流式对比 / 切模式 / 读取快照与 DOM」，
 * 由 run-scenarios.mjs 用 Edge headless + CDP 驱动并断言。
 */
import { createApp, h, reactive, ref } from 'vue'
import ResultStage from '../../src/components/result/ResultStage.vue'
import Toaster from '../../src/components/common/Toaster.vue'
import type { ClassifyResponse, CompareResponse, ModelInfo } from '../../src/types'
import type { CompareStreamRequest } from '../../src/composables'
import '../../src/styles/tokens.css'
import '../../src/styles/base.css'
import '../../src/style.css'

const models: ModelInfo[] = [
  { model_id: 'rf', display_name: '随机森林', enabled: true, model_version: 'rf.pkl', status: 'up' },
  { model_id: 'fasttext', display_name: 'fastText', enabled: true, model_version: 'ft.pkl', status: 'up' },
  { model_id: 'bert', display_name: 'BERT', enabled: true, model_version: 'bert.pkl', status: 'up' },
  { model_id: 'distill', display_name: '蒸馏模型', enabled: true, model_version: 'distill.pkl', status: 'up' },
  { model_id: 'llm', display_name: 'LLM', enabled: true, model_version: null, status: 'up' },
]

const state = reactive({
  title: '',
  mode: 'compare' as 'single' | 'compare',
  single: null as ClassifyResponse | null,
  compare: null as CompareResponse | null,
  streamRequest: null as CompareStreamRequest | null,
})

const stageRef = ref<{ stream?: unknown } | null>(null)
let nonce = 0

interface CardSnapshot {
  modelId: string | null
  status: string | null
  consistency: string | null
  text: string
  latency: string | null
}

interface DomSnapshot {
  resultMode: string | null
  overview: string
  sortActive: string | null
  cards: CardSnapshot[]
  divergence: boolean
  text: string
}

interface StreamSnapshotShape {
  status: string
  requestId: string
  requestedModelIds: string[]
  arrived: Record<string, unknown>
  arrivalOrder: string[]
  summary: Record<string, unknown> | null
  done: Record<string, unknown> | null
  streamError: Record<string, unknown> | null
  error: string
  fallback: boolean
  firstEvent: boolean
  firstEventAt: number | null
  firstEventLatencyMs: number | null
  startedAt: number | null
  finishedAt: number | null
}

function streamSnapshot(): StreamSnapshotShape | null {
  const stream = stageRef.value?.stream as
    | { snapshot?: { value: StreamSnapshotShape } }
    | undefined
  const snapshot = stream?.snapshot?.value
  return snapshot ? JSON.parse(JSON.stringify(snapshot)) : null
}

function domSnapshot(): DomSnapshot {
  const cards = Array.from(document.querySelectorAll('[data-model-id]')).map((element) => {
    const latencyElement = element.querySelector('[data-latency]')
    return {
      modelId: element.getAttribute('data-model-id'),
      status: element.getAttribute('data-status'),
      consistency: element.getAttribute('data-consistency'),
      text: (element.textContent || '').replace(/\s+/g, ' ').trim(),
      latency: latencyElement ? latencyElement.getAttribute('data-latency') : null,
    }
  })
  return {
    resultMode: document.querySelector('[data-result-mode]')?.getAttribute('data-result-mode') ?? null,
    overview: (document.querySelector('.compare-overview')?.textContent || '').replace(/\s+/g, ' ').trim(),
    sortActive:
      document.querySelector('.sort-switch button.active')?.getAttribute('data-sort') ?? null,
    cards,
    divergence: Boolean(document.querySelector('[data-role="divergence-badge"]')),
    text: (document.body.textContent || '').replace(/\s+/g, ' ').trim(),
  }
}

const api = {
  ready: false,
  reset(): void {
    nonce = 0
    state.title = ''
    state.mode = 'compare'
    state.single = null
    state.compare = null
    state.streamRequest = null
  },
  start(title: string, modelIds: string[]): void {
    state.mode = 'compare'
    state.title = title
    state.single = null
    state.compare = null
    nonce += 1
    state.streamRequest = { title, models: [...modelIds], nonce }
  },
  switchMode(mode: 'single' | 'compare'): void {
    state.mode = mode
    state.streamRequest = null
  },
  snapshot: streamSnapshot,
  dom: domSnapshot,
  clickSort(mode: 'latency' | 'consensus'): void {
    const button = document.querySelector<HTMLElement>(`[data-sort="${mode}"]`)
    button?.click()
  },
}

declare global {
  interface Window {
    __f9: typeof api
  }
}

window.__f9 = api

const Root = {
  setup() {
    return () =>
      h('div', { class: 'f9-harness' }, [
        h(ResultStage as never, {
          ref: stageRef as never,
          single: state.single,
          compare: state.compare,
          title: state.title,
          models,
          mode: state.mode,
          streamRequest: state.streamRequest,
        }),
        h(Toaster as never),
      ])
  },
}

const app = createApp(Root)
app.mount('#app')
api.ready = true
