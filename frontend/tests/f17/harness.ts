/** F17 阶段 2B harness：挂载真实 ClassifyPanel + Toaster，仅测试用。 */
import { createApp, h } from 'vue'
import ClassifyPanel from '../../src/components/sections/ClassifyPanel.vue'
import Toaster from '../../src/components/common/Toaster.vue'
import '../../src/styles/tokens.css'
import '../../src/styles/base.css'
import '../../src/style.css'

interface ModelInfo {
  model_id: string
  display_name: string
  enabled: boolean
  model_version: null
  status: string
}

const MODELS: ModelInfo[] = [
  { model_id: 'rf', display_name: '随机森林', enabled: true, model_version: null, status: 'up' },
  { model_id: 'fasttext', display_name: 'fastText', enabled: true, model_version: null, status: 'up' },
  { model_id: 'bert', display_name: 'BERT', enabled: true, model_version: null, status: 'up' },
  { model_id: 'llm', display_name: '大模型', enabled: true, model_version: null, status: 'up' },
  { model_id: 'distill', display_name: '蒸馏 BiLSTM', enabled: true, model_version: null, status: 'up' },
]

function clean(value: string | null | undefined): string {
  return (value || '').replace(/\s+/g, ' ').trim()
}

function textOf(selector: string): string {
  return clean(document.querySelector(selector)?.textContent)
}

function setTextarea(selector: string, value: string): boolean {
  const element = document.querySelector<HTMLTextAreaElement>(selector)
  if (!element) return false
  const setter = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    'value',
  )?.set
  setter?.call(element, value)
  element.dispatchEvent(new Event('input', { bubbles: true }))
  return true
}

function click(selector: string): boolean {
  const element = document.querySelector<HTMLElement>(selector)
  if (!element) return false
  element.click()
  return true
}

function setModel(modelId: string): boolean {
  const select = document.querySelector<HTMLSelectElement>('#model-select')
  if (!select) return false
  select.value = modelId
  select.dispatchEvent(new Event('change', { bubbles: true }))
  return true
}

function previewRows() {
  return Array.from(document.querySelectorAll('[data-batch-preview-row]')).map((element) => {
    const html = element as HTMLElement
    const countText = clean(
      element.querySelector('[data-batch-char-count]')?.textContent,
    )
    return {
      itemId: html.dataset.itemId ?? null,
      lineNo: Number(html.dataset.lineNo ?? 0),
      title: clean(element.querySelector('.batch-preview-title')?.textContent),
      charText: countText,
      charCount: Number.parseInt(countText.split('/')[0] || '0', 10),
      duplicate: clean(element.querySelector('.batch-duplicate-tag')?.textContent),
    }
  })
}

function batchResultRows() {
  return Array.from(document.querySelectorAll('[data-batch-result-row]')).map((element) => {
    const html = element as HTMLElement
    return {
      itemId: html.dataset.itemId ?? null,
      status: html.dataset.status ?? null,
      title: clean(element.querySelector('.batch-result-title')?.textContent),
      category: clean(element.querySelector('.batch-result-category')?.textContent),
      latency: clean(element.querySelector('.batch-result-latency')?.textContent),
      error: clean(element.querySelector('.batch-result-error')?.textContent),
    }
  })
}

function snapshot() {
  const single = document.querySelector<HTMLTextAreaElement>('#news-title')
  const batch = document.querySelector<HTMLTextAreaElement>('[data-batch-input]')
  const select = document.querySelector<HTMLSelectElement>('#model-select')
  const topMode =
    document.querySelector('[data-top-mode][aria-selected="true"]')?.getAttribute('data-top-mode')
    ?? null
  const titleMode =
    document.querySelector('[data-title-mode][aria-pressed="true"]')?.getAttribute('data-title-mode')
    ?? null
  const progress = document.querySelector<HTMLElement>('[data-batch-progress]')
  const panel = document.querySelector<HTMLElement>('[data-batch-result]')
  const submit = document.querySelector<HTMLButtonElement>('[data-submit]')
  return {
    topMode,
    titleMode,
    singleValue: single?.value ?? '',
    batchValue: batch?.value ?? '',
    submitDisabled: Boolean(submit?.disabled),
    submitText: clean(submit?.textContent),
    modelValue: select?.value ?? '',
    llmOptionDisabled:
      select?.querySelector<HTMLOptionElement>('option[value="llm"]')?.disabled ?? null,
    llmConsentVisible: Boolean(document.querySelector('.llm-consent')),
    titleModeSwitchVisible: Boolean(document.querySelector('[data-title-mode="single"]')),
    newlineHint: Boolean(document.querySelector('[data-single-newline-hint]')),
    batchLlmNote: Boolean(document.querySelector('[data-batch-llm-note]')),
    batchValidation: textOf('[data-batch-validation]'),
    batchCount: textOf('[data-batch-count]'),
    batchDuplicate: textOf('[data-batch-duplicate]'),
    batchTooMany: textOf('[data-batch-too-many]'),
    batchTotalError: textOf('[data-batch-total-error]'),
    batchTooLong: textOf('[data-batch-too-long]'),
    preview: previewRows(),
    batchPanelVisible: Boolean(panel),
    batchPhase: panel?.dataset.batchPhase ?? null,
    batchId: panel?.dataset.batchId ?? '',
    batchRequestId: panel?.dataset.requestId ?? '',
    progressDone: Number(progress?.dataset.progressDone ?? -1),
    progressTotal: Number(progress?.dataset.progressTotal ?? -1),
    progressText: clean(progress?.textContent),
    batchSummary: textOf('[data-batch-summary]'),
    batchError: textOf('[data-batch-error]'),
    batchRows: batchResultRows(),
    resultCardCount: document.querySelectorAll('[data-result-card]').length,
    singleNewlineHintVisible:
      Boolean(document.querySelector('[data-single-newline-hint]')),
    pageScrollWidth: document.documentElement.scrollWidth,
    pageClientWidth: document.documentElement.clientWidth,
    bodyText: clean(document.body.textContent),
  }
}

const api = {
  ready: false,
  snapshot,
  setSingleText(value: string): boolean {
    return setTextarea('#news-title', value)
  },
  setBatchText(value: string): boolean {
    return setTextarea('[data-batch-input]', value)
  },
  clickTopMode(mode: 'single' | 'compare'): boolean {
    return click(`[data-top-mode="${mode}"]`)
  },
  clickTitleMode(mode: 'single' | 'multiple'): boolean {
    return click(`[data-title-mode="${mode}"]`)
  },
  clickSubmit(): boolean {
    return click('[data-submit]')
  },
  clickCancel(): boolean {
    return click('[data-batch-cancel]')
  },
  clickRetry(): boolean {
    return click('[data-batch-retry]')
  },
  clickNewlineSwitch(): boolean {
    return click('[data-use-single-as-batch]')
  },
  setModel,
  toggleLlmConsent(): boolean {
    const checkbox = document.querySelector<HTMLInputElement>('.llm-consent input')
    if (!checkbox) return false
    checkbox.click()
    return true
  },
}

declare global {
  interface Window {
    __f17: typeof api
  }
}

window.__f17 = api

const Root = {
  setup() {
    return () =>
      h('div', { class: 'f17-harness' }, [
        h(ClassifyPanel as never, {
          models: MODELS,
          defaultModel: 'fasttext',
          loadingModels: false,
          modelsError: '',
          onRetryModels: () => undefined,
        }),
        h(Toaster as never),
      ])
  },
}

const app = createApp(Root)
app.mount('#app')
api.ready = true
