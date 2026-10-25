/** F19 阶段 3B harness：真实 ClassifyPanel + 内存 File + mock API。 */
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

let responseMode: 'normal' | 'reorder-omit' = 'normal'
const originalFetch = window.fetch.bind(window)
window.fetch = async (input: RequestInfo | URL, init?: RequestInit) => {
  const response = await originalFetch(input, init)
  const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
  if (responseMode !== 'reorder-omit' || !url.includes('/classify/batch') || !response.ok) {
    return response
  }
  const data = await response.clone().json()
  if (Array.isArray(data.results)) {
    data.results = data.results.slice().reverse()
    if (data.results.length > 1) data.results.splice(1, 1)
  }
  return new Response(JSON.stringify(data), {
    status: response.status,
    statusText: response.statusText,
    headers: { 'Content-Type': 'application/json' },
  })
}

const originalArrayBuffer = File.prototype.arrayBuffer
let arrayBufferCalls = 0
let readDelayMs = 0
const delay = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
Object.defineProperty(File.prototype, 'arrayBuffer', {
  configurable: true,
  value: async function patchedArrayBuffer(this: File): Promise<ArrayBuffer> {
    arrayBufferCalls += 1
    if (readDelayMs > 0) await delay(readDelayMs)
    return originalArrayBuffer.call(this)
  },
})

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

function csvItems() {
  return Array.from(document.querySelectorAll('[data-file-item]')).map((element) => {
    const html = element as HTMLElement
    return {
      itemId: html.dataset.itemId ?? null,
      lineNo: Number(html.dataset.lineNo ?? 0),
      recordNo: html.dataset.recordNo ? Number(html.dataset.recordNo) : null,
      title: clean(element.querySelector('.file-item-title')?.textContent),
      length: clean(element.querySelector('.file-item-length')?.textContent),
    }
  })
}

function importedItems() {
  return Array.from(document.querySelectorAll('[data-imported-item]')).map((element) => {
    const html = element as HTMLElement
    return {
      itemId: html.dataset.itemId ?? null,
      lineNo: Number(html.dataset.lineNo ?? 0),
      recordNo: html.dataset.recordNo ? Number(html.dataset.recordNo) : null,
      title: clean(element.querySelector('.imported-title')?.textContent),
    }
  })
}

function batchRows() {
  return Array.from(document.querySelectorAll('[data-batch-result-row]')).map((element) => {
    const html = element as HTMLElement
    return {
      itemId: html.dataset.itemId ?? null,
      lineNo: Number(html.dataset.lineNo ?? 0),
      recordNo: html.dataset.recordNo ? Number(html.dataset.recordNo) : null,
      status: html.dataset.status ?? null,
      title: clean(element.querySelector('.batch-result-title')?.textContent),
      category: clean(element.querySelector('.batch-result-category')?.textContent),
      error: clean(element.querySelector('.batch-result-error')?.textContent),
    }
  })
}

function snapshot() {
  const batchInput = document.querySelector<HTMLTextAreaElement>('#batch-news-title')
  const fileInput = document.querySelector<HTMLInputElement>('[data-file-input]')
  const confirm = document.querySelector<HTMLButtonElement>('[data-file-confirm]')
  const columnSelect = document.querySelector<HTMLSelectElement>('[data-csv-column-select]')
  const columnConfirm = document.querySelector<HTMLInputElement>('[data-csv-column-confirm]')
  const skip = document.querySelector<HTMLInputElement>('[data-skip-bad-rows]')
  const progress = document.querySelector<HTMLElement>('[data-batch-progress]')
  return {
    titleMode: document.querySelector('[data-title-mode][aria-pressed="true"]')?.getAttribute('data-title-mode') ?? null,
    fileInputPresent: Boolean(fileInput),
    fileInputAccept: fileInput?.accept ?? '',
    fileButtonPresent: Boolean(document.querySelector('[data-file-import-button]')),
    fileError: textOf('[data-file-error]'),
    previewVisible: Boolean(document.querySelector('[data-file-preview]')),
    fileMeta: textOf('[data-file-meta]'),
    fileStats: textOf('.file-stats'),
    fileItems: csvItems(),
    rawPreviewVisible: Boolean(document.querySelector('[data-file-raw-preview]')),
    headerState: textOf('[data-csv-header-state]'),
    headerControlsVisible: Boolean(document.querySelector('[data-csv-header-controls]')),
    headerChoice: document.querySelector('[data-csv-header][aria-pressed="true"]')?.getAttribute('data-csv-header') ?? null,
    columnOptions: columnSelect
      ? Array.from(columnSelect.options).map((option) => ({
        value: option.value,
        text: clean(option.textContent),
      }))
      : [],
    columnValue: columnSelect?.value ?? null,
    columnConfirmVisible: Boolean(columnConfirm),
    columnConfirmChecked: Boolean(columnConfirm?.checked),
    skipVisible: Boolean(skip),
    skipChecked: Boolean(skip?.checked),
    skipText: textOf('.skip-errors'),
    skipUnavailable: Boolean(document.querySelector('[data-skip-unavailable]')),
    fileErrors: textOf('.file-errors'),
    confirmDisabled: Boolean(confirm?.disabled),
    confirmText: clean(confirm?.textContent),
    replacementVisible: Boolean(document.querySelector('[data-replace-dialog]')),
    replacementFocusCancel:
      document.activeElement?.hasAttribute('data-replace-cancel') ?? false,
    replacementText: textOf('[data-replace-dialog]'),
    importedVisible: Boolean(document.querySelector('[data-imported-titles]')),
    importedMeta: textOf('[data-imported-meta]'),
    importedItems: importedItems(),
    manualValue: batchInput?.value ?? '',
    batchPhase: document.querySelector<HTMLElement>('[data-batch-result]')?.dataset.batchPhase ?? null,
    batchRows: batchRows(),
    progressDone: Number(progress?.dataset.progressDone ?? -1),
    progressTotal: Number(progress?.dataset.progressTotal ?? -1),
    bodyText: clean(document.body.textContent),
    pageScrollWidth: document.documentElement.scrollWidth,
    pageClientWidth: document.documentElement.clientWidth,
    arrayBufferCalls,
    xss: (window as unknown as { __xss?: number }).__xss ?? 0,
  }
}

function findFileInput(): HTMLInputElement | null {
  return document.querySelector<HTMLInputElement>('[data-file-input]')
}

function makeFile(name: string, bytes: Uint8Array): File {
  return new File([bytes], name, {
    type: name.toLowerCase().endsWith('.csv') ? 'text/csv' : 'text/plain',
  })
}

async function selectBytes(name: string, bytes: Uint8Array | number[]): Promise<boolean> {
  const input = findFileInput()
  if (!input) return false
  const byteArray = Array.isArray(bytes) ? new Uint8Array(bytes) : bytes
  const transfer = new DataTransfer()
  transfer.items.add(makeFile(name, byteArray))
  input.files = transfer.files
  input.dispatchEvent(new Event('change', { bubbles: true }))
  return true
}

async function selectText(name: string, text: string): Promise<boolean> {
  return selectBytes(name, new TextEncoder().encode(text))
}

async function selectLargeFile(name: string, size: number): Promise<boolean> {
  return selectBytes(name, new Uint8Array(size))
}

function resetArrayBufferCalls(): void {
  arrayBufferCalls = 0
}

function setReadDelay(ms: number): void {
  readDelayMs = Math.max(0, ms)
}

function setResponseMode(mode: 'normal' | 'reorder-omit'): void {
  responseMode = mode
}

function setTitleMode(mode: 'single' | 'multiple'): boolean {
  return click(`[data-title-mode="${mode}"]`)
}

function setTopMode(mode: 'single' | 'compare'): boolean {
  return click(`[data-top-mode="${mode}"]`)
}

function clickFileConfirm(): boolean {
  return click('[data-file-confirm]')
}

function clickFileCancel(): boolean {
  return click('[data-file-preview-cancel]')
}

function clickHeader(choice: 'header' | 'data'): boolean {
  return click(`[data-csv-header="${choice}"]`)
}

function chooseColumn(index: number): boolean {
  const select = document.querySelector<HTMLSelectElement>('[data-csv-column-select]')
  if (!select) return false
  select.value = String(index)
  select.dispatchEvent(new Event('change', { bubbles: true }))
  return true
}

function toggleColumnConfirm(): boolean {
  const checkbox = document.querySelector<HTMLInputElement>('[data-csv-column-confirm]')
  if (!checkbox) return false
  checkbox.click()
  return true
}

function toggleSkip(): boolean {
  const checkbox = document.querySelector<HTMLInputElement>('[data-skip-bad-rows]')
  if (!checkbox) return false
  checkbox.click()
  return true
}

function clickReplaceConfirm(): boolean {
  return click('[data-replace-confirm]')
}

function clickReplaceCancel(): boolean {
  return click('[data-replace-cancel]')
}

function clickClearImported(): boolean {
  return click('[data-imported-clear]')
}

function clickSubmit(): boolean {
  return click('[data-submit]')
}

function clickCancelBatch(): boolean {
  return click('[data-batch-cancel]')
}

function clickRetry(): boolean {
  return click('[data-batch-retry]')
}

const api = {
  ready: false,
  snapshot,
  selectText,
  selectBytes,
  selectLargeFile,
  resetArrayBufferCalls,
  setReadDelay,
  setResponseMode,
  setTitleMode,
  setTopMode,
  setModel,
  setSingleText(value: string): boolean {
    return setTextarea('#news-title', value)
  },
  setBatchText(value: string): boolean {
    return setTextarea('#batch-news-title', value)
  },
  clickSubmit,
  clickCancelBatch,
  clickRetry,
  clickFileConfirm,
  clickFileCancel,
  clickHeader,
  chooseColumn,
  toggleColumnConfirm,
  toggleSkip,
  clickReplaceConfirm,
  clickReplaceCancel,
  clickClearImported,
}

declare global {
  interface Window {
    __f19: typeof api
    __xss?: number
  }
}

window.__xss = 0
window.__f19 = api

const Root = {
  setup() {
    return () =>
      h('div', { class: 'f19-harness' }, [
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
