/**
 * 阶段 2B：多条标题 × 单模型的前端编排。
 *
 * 边界：
 * - 用户输入先在前端解析/校验；后端仍会做最终校验；
 * - 每 10 条串行提交一个 /classify/batch；不并行；
 * - 已完成分片结果在后续分片失败/取消时保留；
 * - 只重试失败项，不自动重试；
 * - 不读取/展示 human_feedback_hint（批量契约不返回）。
 */
import { computed, onUnmounted, ref, watch, type Ref } from 'vue'
import {
  ApiClientError,
  classifyBatch,
  friendlyErrorMessage,
  isAbortError,
} from '../api'
import type { BatchItemResult } from '../types'
import {
  BATCH_LLM_MODEL_ID,
  chunkBatchItems,
  createBatchId,
  parseBatchTitles,
  validateBatch,
  type BatchParseResult,
  type ParsedBatchItem,
} from '../utils/batchTitles'
import { setObservedSource } from './useDemoState'

export type BatchPhase = 'idle' | 'running' | 'done' | 'partial' | 'cancelled' | 'error'
export type BatchRowStatus = 'pending' | 'ok' | 'error' | 'cancelled' | 'skipped'

export interface BatchRowState {
  itemId: string
  lineNo: number
  /** CSV 数据记录号；TXT/手动输入为空。 */
  recordNo?: number
  title: string
  charCount: number
  status: BatchRowStatus
  predClass: string | null
  latencyMs: number
  dataSource: string | null
  errorCode: string
  errorMessage: string
}

/** 文件导入条目在本地保留 CSV 记录号；不改变批量 API 的 item_id。 */
export interface ImportedBatchItem extends ParsedBatchItem {
  recordNo?: number
}

export interface ImportedFileMeta {
  fileName: string
  format: 'txt' | 'csv'
  encoding: string
  hadBom: boolean
  recordCount: number
  blankRecordCount: number
  emptyTitleCount: number
  duplicateExtraCount: number
  duplicateGroupCount: number
  /** 用户显式跳过的可恢复坏记录数（唯一行数）。 */
  skippedErrorCount: number
}

export interface ImportedBatchInput {
  items: ImportedBatchItem[]
  parsed: BatchParseResult
  meta: ImportedFileMeta
}

export interface UseBatchClassifyOptions {
  /** 当前单模型选择；批量与单条共用。 */
  model: Ref<string>
  modelsError: Ref<string>
  /** 每片条数；默认 10，测试可缩短。 */
  chunkSize?: number
}

export function useBatchClassify(options: UseBatchClassifyOptions) {
  const { model, modelsError } = options
  const chunkSize = options.chunkSize ?? 10

  const text = ref('')
  const manualParsed = computed<BatchParseResult>(() => parseBatchTitles(text.value))
  const imported = ref<ImportedBatchInput | null>(null)
  const parsed = computed<BatchParseResult>(() => {
    if (!imported.value) return manualParsed.value
    return {
      ...imported.value.parsed,
      items: imported.value.items,
    }
  })
  const activeItems = computed<readonly ParsedBatchItem[]>(() =>
    imported.value?.items ?? manualParsed.value.items,
  )
  const hasImported = computed(() => imported.value !== null)
  const importedMeta = computed<ImportedFileMeta | null>(
    () => imported.value?.meta ?? null,
  )
  const validation = computed(() =>
    validateBatch(parsed.value, model.value, modelsError.value),
  )
  const rows = ref<BatchRowState[]>([])
  const phase = ref<BatchPhase>('idle')
  const submitting = ref(false)
  const cancelRequested = ref(false)
  const batchId = ref('')
  const requestId = ref('')
  const lastError = ref('')
  const lastErrorCode = ref('')

  let activeController: AbortController | null = null
  let runSeq = 0

  onUnmounted(() => {
    // 组件/页面卸载时中止进行中的批量请求；异步 finally 不再更新状态。
    runSeq += 1
    activeController?.abort()
    activeController = null
  })

  const canSubmit = computed(() => validation.value.ok && !submitting.value)
  const failedRows = computed(() => rows.value.filter((row) => row.status === 'error'))
  const canRetry = computed(() => failedRows.value.length > 0 && !submitting.value)
  const progressDone = computed(
    () =>
      rows.value.filter(
        (row) => row.status === 'ok' || row.status === 'error',
      ).length,
  )
  const progressTotal = computed(() => rows.value.length)
  const okCount = computed(() => rows.value.filter((row) => row.status === 'ok').length)
  const errorCount = computed(
    () => rows.value.filter((row) => row.status === 'error').length,
  )

  function clearResults(): void {
    if (!rows.value.length && phase.value === 'idle') return
    rows.value = []
    phase.value = 'idle'
    batchId.value = ''
    requestId.value = ''
    lastError.value = ''
    lastErrorCode.value = ''
  }

  // 输入或模型变化后，旧结果不再对应当前输入/模型；保留草稿，清空结果。
  watch([text, model], () => {
    if (submitting.value) return
    // 手动文本一旦被编辑，文件来源不再有效，恢复文本来源。
    if (imported.value) imported.value = null
    clearResults()
  })

  // 文件来源切换（导入/清除）后，旧结果不再对应当前标题列表。
  watch(imported, () => {
    if (submitting.value) return
    clearResults()
  })

  function setImportedBatch(input: ImportedBatchInput): void {
    if (submitting.value) return
    imported.value = input
  }

  function clearImportedBatch(): void {
    if (submitting.value) return
    imported.value = null
  }

  function startRows(items: readonly ParsedBatchItem[]): void {
    rows.value = items.map((item) => ({
      itemId: item.itemId,
      lineNo: item.lineNo,
      recordNo: (item as ImportedBatchItem).recordNo,
      title: item.title,
      charCount: item.charCount,
      status: 'pending',
      predClass: null,
      latencyMs: 0,
      dataSource: null,
      errorCode: '',
      errorMessage: '',
    }))
  }

  function rowIndex(itemId: string): number {
    return rows.value.findIndex((row) => row.itemId === itemId)
  }

  function patchRow(itemId: string, patch: Partial<BatchRowState>): void {
    const index = rowIndex(itemId)
    if (index < 0) return
    rows.value[index] = { ...rows.value[index], ...patch }
  }

  function markChunk(
    chunk: readonly ParsedBatchItem[],
    status: BatchRowStatus,
    errorCode: string,
    errorMessage: string,
  ): void {
    for (const item of chunk) {
      patchRow(item.itemId, {
        status,
        predClass: null,
        latencyMs: 0,
        dataSource: null,
        errorCode,
        errorMessage,
      })
    }
  }

  function applyResult(result: BatchItemResult): void {
    if (result.status === 'ok' && result.pred_class) {
      patchRow(result.item_id, {
        status: 'ok',
        predClass: result.pred_class,
        latencyMs: typeof result.latency_ms === 'number' ? result.latency_ms : 0,
        dataSource: result.data_source ?? null,
        errorCode: '',
        errorMessage: '',
      })
      return
    }
    patchRow(result.item_id, {
      status: 'error',
      predClass: null,
      latencyMs: 0,
      dataSource: null,
      errorCode: result.error?.code || 'BATCH_ITEM_INVALID',
      errorMessage: result.error?.message || '该条分类未返回有效结果。',
    })
  }

  function setObservedBatchSource(): void {
    const sources = Array.from(
      new Set(
        rows.value
          .map((row) => row.dataSource)
          .filter((source): source is string => Boolean(source)),
      ),
    )
    setObservedSource(sources.length === 1 ? sources[0] : null)
  }

  async function requestChunk(
    chunk: readonly ParsedBatchItem[],
    controller: AbortController,
  ): Promise<void> {
    const response = await classifyBatch(
      {
        model: model.value,
        batch_id: batchId.value,
        items: chunk.map((item) => ({
          item_id: item.itemId,
          title: item.title,
        })),
      },
      controller.signal,
    )
    requestId.value = response.request_id || requestId.value
    const expected = new Set(chunk.map((item) => item.itemId))
    const seen = new Set<string>()
    for (const result of response.results) {
      if (!expected.has(result.item_id)) continue
      seen.add(result.item_id)
      applyResult(result)
    }
    for (const item of chunk) {
      if (seen.has(item.itemId)) continue
      patchRow(item.itemId, {
        status: 'error',
        predClass: null,
        latencyMs: 0,
        errorCode: 'BATCH_RESPONSE_MISSING',
        errorMessage: '服务端未返回该条结果。',
      })
    }
  }

  function markUnstartedAs(status: BatchRowStatus, message: string): void {
    for (const row of rows.value) {
      if (row.status !== 'pending') continue
      patchRow(row.itemId, {
        status,
        predClass: null,
        latencyMs: 0,
        errorCode: status === 'cancelled' ? 'CLIENT_ABORTED' : 'BATCH_NOT_SUBMITTED',
        errorMessage: message,
      })
    }
  }

  async function runChunks(
    items: readonly ParsedBatchItem[],
    controller: AbortController,
    token: number,
  ): Promise<'completed' | 'request_error' | 'cancelled'> {
    const chunks = chunkBatchItems(items, chunkSize)
    for (let index = 0; index < chunks.length; index += 1) {
      if (token !== runSeq) return 'cancelled'
      if (cancelRequested.value) {
        markChunk(chunks[index], 'cancelled', 'CLIENT_ABORTED', '请求已取消。')
        for (const rest of chunks.slice(index + 1)) {
          markChunk(rest, 'skipped', 'BATCH_NOT_SUBMITTED', '未提交：批次已取消。')
        }
        return 'cancelled'
      }
      try {
        await requestChunk(chunks[index], controller)
      } catch (reason) {
        if (token !== runSeq) return 'cancelled'
        if (isAbortError(reason) || cancelRequested.value) {
          markChunk(chunks[index], 'cancelled', 'CLIENT_ABORTED', '请求已取消。')
          for (const rest of chunks.slice(index + 1)) {
            markChunk(rest, 'skipped', 'BATCH_NOT_SUBMITTED', '未提交：批次已取消。')
          }
          return 'cancelled'
        }
        const apiError = reason instanceof ApiClientError ? reason : null
        lastError.value = friendlyErrorMessage(reason)
        lastErrorCode.value = apiError?.code || 'NETWORK_ERROR'
        if (apiError?.requestId) requestId.value = apiError.requestId
        markChunk(
          chunks[index],
          'error',
          lastErrorCode.value,
          lastError.value,
        )
        for (const rest of chunks.slice(index + 1)) {
          markChunk(rest, 'skipped', 'BATCH_NOT_SUBMITTED', '未提交：前一分片失败。')
        }
        return 'request_error'
      }
    }
    return 'completed'
  }

  function finalize(outcome: 'completed' | 'request_error' | 'cancelled'): void {
    if (outcome === 'cancelled') {
      phase.value = 'cancelled'
      markUnstartedAs('cancelled', '未提交：批次已取消。')
    } else if (outcome === 'request_error') {
      phase.value = okCount.value > 0 ? 'partial' : 'error'
    } else if (errorCount.value > 0) {
      phase.value = okCount.value > 0 ? 'partial' : 'error'
    } else if (rows.value.some((row) => row.status === 'skipped')) {
      // 后续分片失败后，重试只发送失败项；未发送条目仍保持“未提交”，
      // 不能把整个批次标为完全完成。
      phase.value = 'partial'
    } else {
      phase.value = 'done'
    }
    setObservedBatchSource()
  }

  async function submit(): Promise<void> {
    if (!canSubmit.value) return
    const items = activeItems.value
    const token = ++runSeq
    cancelRequested.value = false
    startRows(items)
    phase.value = 'running'
    submitting.value = true
    batchId.value = createBatchId()
    requestId.value = ''
    lastError.value = ''
    lastErrorCode.value = ''
    const controller = new AbortController()
    activeController = controller
    let outcome: 'completed' | 'request_error' | 'cancelled' = 'completed'
    try {
      outcome = await runChunks(items, controller, token)
    } finally {
      if (token === runSeq) {
        finalize(outcome)
        submitting.value = false
        activeController = null
      }
    }
  }

  async function retryFailed(): Promise<void> {
    if (!canRetry.value) return
    const failed = rows.value.filter((row) => row.status === 'error')
    if (!failed.length) return
    const items: ImportedBatchItem[] = failed.map((row) => ({
      itemId: row.itemId,
      lineNo: row.lineNo,
      recordNo: row.recordNo,
      title: row.title,
      charCount: row.charCount,
      duplicateCount: 1,
      tooLong: false,
    }))
    for (const row of failed) {
      patchRow(row.itemId, {
        status: 'pending',
        predClass: null,
        latencyMs: 0,
        dataSource: null,
        errorCode: '',
        errorMessage: '',
      })
    }
    const token = ++runSeq
    cancelRequested.value = false
    phase.value = 'running'
    submitting.value = true
    batchId.value = createBatchId()
    lastError.value = ''
    lastErrorCode.value = ''
    const controller = new AbortController()
    activeController = controller
    let outcome: 'completed' | 'request_error' | 'cancelled' = 'completed'
    try {
      outcome = await runChunks(items, controller, token)
    } finally {
      if (token === runSeq) {
        finalize(outcome)
        submitting.value = false
        activeController = null
      }
    }
  }

  function cancel(): void {
    if (!submitting.value) return
    cancelRequested.value = true
    activeController?.abort()
  }

  return {
    text,
    parsed,
    activeItems,
    hasImported,
    imported,
    importedMeta,
    validation,
    rows,
    phase,
    submitting,
    canSubmit,
    canRetry,
    progressDone,
    progressTotal,
    okCount,
    errorCount,
    batchId,
    requestId,
    lastError,
    lastErrorCode,
    failedRows,
    modelIsLlm: computed(() => model.value === BATCH_LLM_MODEL_ID),
    setImportedBatch,
    clearImportedBatch,
    submit,
    retryFailed,
    cancel,
    clearResults,
  }
}
