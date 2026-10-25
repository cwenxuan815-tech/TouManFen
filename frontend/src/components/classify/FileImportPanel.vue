<script setup lang="ts">
/**
 * 阶段 3B：TXT/CSV 本地文件选择、解析预览、CSV 表头/标题列确认、导入确认。
 *
 * 边界：
 * - 读取前检查 File.size；不超限才调用 arrayBuffer；
 * - 解析完全在浏览器内，不上传文件；不保留 File/原始文件路径；
 * - 只有“确认导入”才 emit；预览、取消、解析失败都不改变既有批量状态；
 * - 表头/列选择使用已读取的字节重新解析，不重新读磁盘。
 */
import { computed, nextTick, onUnmounted, ref } from 'vue'
import type {
  ImportedBatchInput,
  ImportedBatchItem,
  ImportedFileMeta,
} from '../../composables'
import {
  MAX_TITLE_FILE_BYTES,
  parseTitleFile,
  fileFormatFromName,
  type TitleFileIssue,
  type TitleFileParseResult,
} from '../../utils/titleFiles'
import {
  BATCH_ITEM_MAX_CHARS,
  BATCH_MAX_ITEMS,
  BATCH_MAX_TOTAL_CHARS,
} from '../../utils/batchTitles'

const props = defineProps<{
  disabled?: boolean
  hasExistingContent: boolean
  existingSummary?: string
}>()

const emit = defineEmits<{
  import: [payload: ImportedBatchInput]
}>()

const RECOVERABLE_CODES = new Set<string>([
  'CSV_FIELD_COUNT_MISMATCH',
  'CSV_INVALID_QUOTE',
])

const fileInput = ref<HTMLInputElement | null>(null)
const bytes = ref<Uint8Array | null>(null)
const fileName = ref('')
const fileSize = ref(0)
const baseResult = ref<TitleFileParseResult | null>(null)
const reading = ref(false)
const previewOpen = ref(false)
const fileError = ref('')
const headerChoice = ref<'auto' | 'header' | 'data'>('auto')
const columnChoice = ref<number | null>(null)
const columnConfirmed = ref(false)
const skipBadRows = ref(false)
const replacementOpen = ref(false)
const cancelReplacementRef = ref<HTMLButtonElement | null>(null)

/** 防止旧文件的异步 arrayBuffer 结果覆盖新文件/已卸载组件。 */
let readToken = 0
/** 防止双击确认/替换重复 emit。 */
let importEmitted = false

onUnmounted(() => {
  // 无法取消 File.arrayBuffer() 本身，但可让完成后的回调失效并释放引用。
  readToken += 1
  bytes.value = null
  baseResult.value = null
})

const recoverableErrors = computed<TitleFileIssue[]>(() => {
  const result = baseResult.value
  if (!result || result.format !== 'csv') return []
  return result.errors.filter(
    (error) => RECOVERABLE_CODES.has(error.code) && error.lineNo !== undefined,
  )
})

const nonRecoverableErrors = computed<TitleFileIssue[]>(() => {
  const result = baseResult.value
  if (!result) return []
  return result.errors.filter(
    (error) => !RECOVERABLE_CODES.has(error.code) || error.lineNo === undefined,
  )
})

const canSkipBadRows = computed(
  () =>
    Boolean(baseResult.value)
    && baseResult.value?.format === 'csv'
    && recoverableErrors.value.length > 0
    && nonRecoverableErrors.value.length === 0,
)

const skippedErrorCount = computed(() => {
  if (!skipBadRows.value || !canSkipBadRows.value) return 0
  return new Set(
    recoverableErrors.value
      .map((error) => error.lineNo)
      .filter((lineNo): lineNo is number => typeof lineNo === 'number'),
  ).size
})

/** 只跳过解析器标记为可恢复的记录级错误；整体/编码/超限错误不在此列。 */
function rebuildAfterSkipping(
  base: TitleFileParseResult,
  skippedLines: ReadonlySet<number>,
): TitleFileParseResult {
  const items = base.items.filter((item) => !skippedLines.has(item.lineNo))
  const counts = new Map<string, number>()
  for (const item of items) {
    counts.set(item.title, (counts.get(item.title) ?? 0) + 1)
  }
  const normalizedItems = items.map((item) => ({
    ...item,
    duplicateCount: counts.get(item.title) ?? 1,
  }))
  const totalChars = normalizedItems.reduce((sum, item) => sum + item.charCount, 0)
  const tooLongCount = normalizedItems.filter((item) => item.tooLong).length
  const errors = base.errors.filter((error) => !RECOVERABLE_CODES.has(error.code))
  if (!normalizedItems.length) {
    errors.push({ code: 'NO_VALID_TITLES', message: '跳过后没有可导入标题' })
  }
  if (normalizedItems.length > BATCH_MAX_ITEMS) {
    errors.push({
      code: 'TOO_MANY_TITLES',
      message: `有效标题 ${normalizedItems.length} 条，超过 ${BATCH_MAX_ITEMS} 条上限`,
    })
  }
  if (totalChars > BATCH_MAX_TOTAL_CHARS) {
    errors.push({
      code: 'TOTAL_CHARS_EXCEEDED',
      message: `标题总字符 ${totalChars} 超过 ${BATCH_MAX_TOTAL_CHARS} 上限`,
    })
  }
  if (tooLongCount > 0) {
    errors.push({
      code: 'TITLE_TOO_LONG',
      message: `有 ${tooLongCount} 条标题超过 ${BATCH_ITEM_MAX_CHARS} 个字符`,
    })
  }
  const ok = errors.length === 0
  const result: TitleFileParseResult = {
    ...base,
    items: normalizedItems,
    validItemCount: normalizedItems.filter((item) => !item.tooLong).length,
    totalChars,
    duplicateExtraCount: Math.max(0, normalizedItems.length - counts.size),
    duplicateGroupCount: [...counts.values()].filter((count) => count > 1).length,
    tooLongCount,
    errors,
    ok,
    importable: ok,
    status: 'ok',
  }
  if (!ok) {
    result.status = errors[0]?.code ?? 'CSV_PARSE_ERROR'
  } else if (result.columnSelection.confirmationRequired) {
    result.status = 'COLUMN_SELECTION_REQUIRED'
  } else if (result.header.confirmationRequired) {
    result.status = 'HEADER_CONFIRMATION_REQUIRED'
  }
  return result
}

const effectiveResult = computed<TitleFileParseResult | null>(() => {
  const base = baseResult.value
  if (!base) return null
  if (!skipBadRows.value || !canSkipBadRows.value) return base
  const skippedLines = new Set(
    recoverableErrors.value
      .map((error) => error.lineNo)
      .filter((lineNo): lineNo is number => typeof lineNo === 'number'),
  )
  return rebuildAfterSkipping(base, skippedLines)
})

const currentErrors = computed<TitleFileIssue[]>(() => {
  const result = effectiveResult.value
  if (!result) return []
  return result.errors
})

const canImport = computed(() => {
  const result = effectiveResult.value
  if (!result || result.errors.length > 0) return false
  if (result.header.confirmationRequired) return false
  if (result.format === 'csv' && result.columnSelection.selectedIndex === null) return false
  if (result.columnSelection.confirmationRequired && !columnConfirmed.value) return false
  if (result.items.length === 0 || result.items.length > BATCH_MAX_ITEMS) return false
  if (result.totalChars > BATCH_MAX_TOTAL_CHARS) return false
  if (result.tooLongCount > 0) return false
  return true
})

const displayItems = computed(() => (effectiveResult.value?.items ?? []).slice(0, 100))
const hasMoreItems = computed(
  () => (effectiveResult.value?.items.length ?? 0) > displayItems.value.length,
)

const totalLines = computed(() => {
  const result = baseResult.value
  if (!result) return 0
  return result.recordCount + result.blankRecordCount
})

const encodingLabel = computed(() => {
  const result = baseResult.value
  if (!result?.encoding) return '未知'
  return `${result.encoding === 'utf-8' ? 'UTF-8' : 'GB18030'}${result.hadBom ? ' BOM' : ''}`
})

const formatLabel = computed(() =>
  baseResult.value?.format === 'csv' ? 'CSV' : 'TXT',
)

const headerLabel = computed(() => {
  const result = baseResult.value
  if (!result || result.format !== 'csv') return ''
  if (headerChoice.value === 'header') return '首行是表头'
  if (headerChoice.value === 'data') return '首行是数据'
  if (result.header.detected) return '已自动识别首行表头'
  return '首行表头未确认'
})

const columnConfirmationNeeded = computed(() => {
  const result = effectiveResult.value
  return Boolean(
    result
    && result.format === 'csv'
    && result.columnSelection.confirmationRequired
    && result.columnSelection.selectedIndex !== null,
  )
})

const currentColumnName = computed(() => {
  const result = effectiveResult.value
  const index = result?.columnSelection.selectedIndex
  if (index === null || index === undefined) return ''
  return result?.columns[index]?.name ?? `第${index + 1}列`
})

function openFilePicker(): void {
  if (props.disabled || reading.value) return
  fileInput.value?.click()
}

function resetPreview(): void {
  bytes.value = null
  fileName.value = ''
  fileSize.value = 0
  baseResult.value = null
  headerChoice.value = 'auto'
  columnChoice.value = null
  columnConfirmed.value = false
  skipBadRows.value = false
  replacementOpen.value = false
  previewOpen.value = false
}

function closePreview(): void {
  // 使仍可能在读取的 File.arrayBuffer() 回调失效。
  readToken += 1
  reading.value = false
  fileError.value = ''
  resetPreview()
}

function errorText(error: TitleFileIssue): string {
  const line = error.lineNo ? `第 ${error.lineNo} 行` : ''
  return `${line}${line ? '：' : ''}${error.message}`
}

function parseCurrent(): void {
  if (!bytes.value || !fileName.value) return
  const result = parseTitleFile(
    { name: fileName.value, bytes: bytes.value },
    {
      header:
        headerChoice.value === 'auto'
          ? undefined
          : headerChoice.value === 'header',
      columnIndex: columnChoice.value ?? undefined,
    },
  )
  baseResult.value = result
  if (result.columnSelection.selectedIndex !== null) {
    columnChoice.value = result.columnSelection.selectedIndex
  } else {
    columnChoice.value = null
  }
  columnConfirmed.value = false
  skipBadRows.value = false
}

function setHeaderChoice(choice: 'header' | 'data'): void {
  headerChoice.value = choice
  columnChoice.value = null
  parseCurrent()
}

function onColumnChange(event: Event): void {
  const raw = (event.target as HTMLSelectElement).value
  columnChoice.value = raw === '' ? null : Number(raw)
  columnConfirmed.value = false
  parseCurrent()
}

async function onFileChange(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return

  const token = ++readToken
  importEmitted = false
  fileError.value = ''
  resetPreview()

  const format = fileFormatFromName(file.name)
  if (!format) {
    fileError.value = '仅支持 .txt 和 .csv 文件；其他格式已拒绝。'
    return
  }
  if (file.size > MAX_TITLE_FILE_BYTES) {
    // 关键：先检查大小，超限时绝不调用 arrayBuffer()。
    fileError.value = `文件超过 1 MiB（${MAX_TITLE_FILE_BYTES} 字节）上限，未读取内容。`
    return
  }

  reading.value = true
  try {
    const buffer = await file.arrayBuffer()
    if (token !== readToken) return
    const view = new Uint8Array(buffer)
    bytes.value = view
    fileName.value = file.name
    fileSize.value = file.size
    baseResult.value = parseTitleFile({ name: file.name, bytes: view })
    headerChoice.value = 'auto'
    columnChoice.value = baseResult.value.columnSelection.selectedIndex
    previewOpen.value = true
  } catch {
    if (token !== readToken) return
    fileError.value = '文件读取失败，请重试或更换文件。'
    resetPreview()
  } finally {
    if (token === readToken) reading.value = false
  }
}

function payloadFromResult(result: TitleFileParseResult): ImportedBatchInput {
  const items: ImportedBatchItem[] = result.items.map((item) => ({
    ...item,
    recordNo: item.recordNo,
  }))
  const meta: ImportedFileMeta = {
    fileName: fileName.value,
    format: result.format === 'csv' ? 'csv' : 'txt',
    encoding: result.encoding === 'gb18030' ? 'gb18030' : 'utf-8',
    hadBom: result.hadBom,
    recordCount: result.recordCount,
    blankRecordCount: result.blankRecordCount,
    emptyTitleCount: result.emptyTitleCount,
    duplicateExtraCount: result.duplicateExtraCount,
    duplicateGroupCount: result.duplicateGroupCount,
    skippedErrorCount: skippedErrorCount.value,
  }
  return {
    items,
    parsed: {
      items: result.items,
      totalChars: result.totalChars,
      emptyLineCount: result.blankRecordCount + result.emptyTitleCount,
      duplicateExtraCount: result.duplicateExtraCount,
      duplicateGroupCount: result.duplicateGroupCount,
      tooLongCount: result.tooLongCount,
    },
    meta,
  }
}

function confirmImport(): void {
  if (importEmitted || replacementOpen.value) return
  const result = effectiveResult.value
  if (!result || !canImport.value) return
  if (props.hasExistingContent) {
    replacementOpen.value = true
    void nextTick(() => cancelReplacementRef.value?.focus())
    return
  }
  emitImport()
}

function emitImport(): void {
  if (importEmitted) return
  const result = effectiveResult.value
  if (!result) return
  importEmitted = true
  emit('import', payloadFromResult(result))
  closePreview()
}

function cancelReplacement(): void {
  // 安全默认：只关闭替换确认，不触碰当前草稿、结果或已加载的预览。
  replacementOpen.value = false
}

function confirmReplacement(): void {
  replacementOpen.value = false
  emitImport()
}

function itemSource(item: ImportedBatchItem): string {
  if (item.recordNo !== undefined) {
    return `记录 ${item.recordNo} · 第 ${item.lineNo} 行`
  }
  return `第 ${item.lineNo} 行`
}
</script>

<template>
  <div class="file-import" data-file-import>
    <div class="file-import-row">
      <input
        ref="fileInput"
        class="file-input"
        type="file"
        accept=".txt,.csv,text/plain,text/csv"
        data-file-input
        :disabled="disabled || reading"
        @change="onFileChange"
      />
      <button
        type="button"
        class="file-import-button"
        data-file-import-button
        :disabled="disabled || reading"
        @click="openFilePicker"
      >
        <span class="file-icon">↑</span>
        {{ reading ? '正在读取…' : '导入 TXT/CSV' }}
      </button>
      <span class="file-import-hint">.txt / .csv · 最大 1 MiB · 浏览器本地解析，不上传</span>
    </div>
    <p v-if="fileError" class="file-error" role="alert" data-file-error>
      {{ fileError }}
    </p>

    <section
      v-if="previewOpen && baseResult"
      class="file-preview"
      data-file-preview
      aria-labelledby="file-preview-title"
    >
      <div class="file-preview-head">
        <div>
          <div class="file-preview-kicker">LOCAL IMPORT · PREVIEW</div>
          <h4 id="file-preview-title">{{ baseResult.format === 'csv' ? 'CSV' : 'TXT' }} 导入预览</h4>
          <p class="file-preview-meta" data-file-meta>
            {{ fileName }} · {{ formatLabel }} · {{ encodingLabel }}
            <span v-if="fileSize"> · {{ (fileSize / 1024).toFixed(1) }} KiB</span>
          </p>
        </div>
        <button type="button" class="file-close" data-file-preview-cancel @click="closePreview">
          取消
        </button>
      </div>

      <div class="file-stats" aria-live="polite">
        <span>{{ baseResult.format === 'txt' ? `总行数 ${totalLines}` : `数据记录 ${baseResult.recordCount}` }}</span>
        <span>标题 {{ baseResult.items.length }}（有效 {{ baseResult.validItemCount }}）</span>
        <span>空行/空白记录 {{ baseResult.blankRecordCount }}</span>
        <span v-if="baseResult.format === 'csv'">空标题 {{ baseResult.emptyTitleCount }}</span>
        <span v-if="baseResult.duplicateExtraCount">重复 {{ baseResult.duplicateExtraCount }} 条 / {{ baseResult.duplicateGroupCount }} 组</span>
        <span>总字符 {{ baseResult.totalChars }} / {{ BATCH_MAX_TOTAL_CHARS }}</span>
      </div>

      <div v-if="baseResult.format === 'csv'" class="csv-controls">
        <div class="csv-control-group" data-csv-header-controls>
          <span class="csv-control-label">首行表头</span>
          <span class="csv-header-state" data-csv-header-state>{{ headerLabel }}</span>
          <div class="csv-choice-buttons" role="group" aria-label="首行是否为表头">
            <button
              type="button"
              class="csv-choice"
              data-csv-header="header"
              :class="{ active: headerChoice === 'header' }"
              :aria-pressed="headerChoice === 'header'"
              @click="setHeaderChoice('header')"
            >
              首行是表头
            </button>
            <button
              type="button"
              class="csv-choice"
              data-csv-header="data"
              :class="{ active: headerChoice === 'data' }"
              :aria-pressed="headerChoice === 'data'"
              @click="setHeaderChoice('data')"
            >
              首行是数据
            </button>
          </div>
          <span v-if="baseResult.header.confirmationRequired" class="csv-hint">
            请明确选择首行是否表头后再导入。
          </span>
        </div>

        <div class="csv-control-group" data-csv-column-controls>
          <label class="csv-control-label" for="csv-title-column">标题列</label>
          <select
            id="csv-title-column"
            class="csv-column-select"
            data-csv-column-select
            :value="columnChoice ?? ''"
            @change="onColumnChange"
          >
            <option value="" disabled>请选择标题列</option>
            <option
              v-for="column in baseResult.columns"
              :key="column.index"
              :value="column.index"
            >
              第 {{ column.index + 1 }} 列 · {{ column.name }} · 样例：{{ column.sample || '（空）' }}
            </option>
          </select>
          <span v-if="baseResult.columnSelection.confirmationRequired" class="csv-hint">
            请确认标题列后再导入。
          </span>
          <label v-if="columnConfirmationNeeded" class="csv-confirm">
            <input v-model="columnConfirmed" type="checkbox" data-csv-column-confirm />
            我确认使用「{{ currentColumnName }}」作为标题列
          </label>
        </div>
      </div>

      <div v-if="currentErrors.length" class="file-errors" role="alert">
        <p class="file-errors-title">解析错误</p>
        <ul>
          <li v-for="(error, index) in currentErrors.slice(0, 20)" :key="`${error.code}-${index}`">
            {{ errorText(error) }}
          </li>
        </ul>
        <p v-if="currentErrors.length > 20" class="csv-hint">
          另有 {{ currentErrors.length - 20 }} 条错误未展开。
        </p>
      </div>

      <div v-if="canSkipBadRows" class="skip-errors">
        <label class="csv-confirm">
          <input v-model="skipBadRows" type="checkbox" data-skip-bad-rows />
          跳过 {{ recoverableErrors.length }} 条可恢复坏记录后导入（实际跳过 {{ skippedErrorCount }} 行）
        </label>
        <p class="csv-hint">
          只跳过字段数不一致或可恢复的引号错误；编码、整体结构、超限错误不可通过此选项绕过。
        </p>
      </div>
      <p
        v-else-if="baseResult.format === 'csv' && nonRecoverableErrors.length"
        class="csv-hint file-blocked"
        data-skip-unavailable
      >
        存在不可恢复的解析错误，不能通过“跳过错误行”绕过。
      </p>

      <div v-if="displayItems.length" class="file-items" data-file-items>
        <div class="file-items-head">
          <span>来源</span><span>标题</span><span>长度</span><span>重复</span>
        </div>
        <ul>
          <li
            v-for="item in displayItems"
            :key="item.itemId"
            class="file-item"
            :class="{ 'is-too-long': item.tooLong }"
            data-file-item
            :data-item-id="item.itemId"
            :data-line-no="item.lineNo"
            :data-record-no="item.recordNo ?? ''"
          >
            <span class="file-item-source">{{ itemSource(item) }}</span>
            <span class="file-item-title" :title="item.title">{{ item.title }}</span>
            <span class="file-item-length">{{ item.charCount }}/{{ BATCH_ITEM_MAX_CHARS }}</span>
            <span class="file-item-duplicate">{{ item.duplicateCount > 1 ? `重复 ×${item.duplicateCount}` : '—' }}</span>
          </li>
        </ul>
        <p v-if="hasMoreItems" class="csv-hint">
          仅展开前 100 条；文件共 {{ effectiveResult?.items.length }} 条有效标题。
        </p>
      </div>

      <div v-else-if="baseResult.format === 'csv' && baseResult.columns.length > 1" class="file-raw-preview" data-file-raw-preview>
        <p class="file-raw-title">选择标题列后可预览；下面是文件前几行原始列：</p>
        <ul>
          <li v-for="row in baseResult.previewRows" :key="row.recordNo">
            <span>记录 {{ row.recordNo }} · 第 {{ row.lineNo }} 行</span>
            <span v-for="(cell, index) in row.cells" :key="index" class="raw-cell">{{ cell || '（空）' }}</span>
          </li>
        </ul>
      </div>

      <div class="file-preview-actions">
        <button type="button" class="file-cancel" data-file-preview-cancel @click="closePreview">
          取消，不导入
        </button>
        <button
          type="button"
          class="file-confirm"
          data-file-confirm
          :disabled="!canImport"
          @click="confirmImport"
        >
          {{ canImport ? `确认导入 ${effectiveResult?.items.length ?? 0} 条` : '请先处理错误/确认选项' }}
        </button>
      </div>
      <p v-if="!canImport && baseResult.errors.length === 0 && !baseResult.header.confirmationRequired && !baseResult.columnSelection.confirmationRequired" class="csv-hint">
        请检查标题列、表头设置和标题数量/长度限制。
      </p>

      <div
        v-if="replacementOpen"
        class="replace-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="replace-dialog-title"
        data-replace-dialog
        @keydown.esc="cancelReplacement"
      >
        <div class="replace-card">
          <h5 id="replace-dialog-title">替换当前多条内容？</h5>
          <p>
            导入将替换当前标题列表并清空对应的批量结果。
            <span v-if="existingSummary">{{ existingSummary }}</span>
            当前手动草稿会保留，清除文件导入后可恢复。
          </p>
          <div class="replace-actions">
            <button
              ref="cancelReplacementRef"
              type="button"
              class="file-cancel"
              data-replace-cancel
              @click="cancelReplacement"
            >
              取消，保留当前内容
            </button>
            <button type="button" class="file-confirm" data-replace-confirm @click="confirmReplacement">
              替换为导入内容
            </button>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.file-import {
  margin-top: var(--space-12);
}

.file-input {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  clip-path: inset(50%);
}

.file-import-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-8);
}

.file-import-button,
.file-cancel,
.file-confirm,
.file-close {
  min-height: 32px;
  border-radius: var(--radius-pill);
  font: inherit;
  cursor: pointer;
}

.file-import-button {
  display: inline-flex;
  align-items: center;
  gap: var(--space-6);
  padding: 0 var(--space-12);
  border: 1px dashed var(--border-control);
  background: var(--surface-page);
  color: var(--text-secondary);
  font-size: 11px;
}

.file-import-button:hover:not(:disabled) {
  border-color: var(--brand-light);
  color: var(--brand);
}

.file-import-button:disabled,
.file-confirm:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}

.file-icon {
  color: var(--brand);
  font-size: 14px;
  line-height: 1;
}

.file-import-hint {
  color: var(--text-faint);
  font-size: 10px;
}

.file-error,
.file-blocked {
  margin: var(--space-8) 0 0;
  color: var(--danger-text);
  font-size: 11px;
  line-height: 1.6;
}

.file-preview {
  position: relative;
  margin-top: var(--space-12);
  padding: var(--space-16);
  border: 1px solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--surface-card);
}

.file-preview-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--space-10);
}

.file-preview-kicker {
  color: var(--text-faint);
  font-size: 9px;
  letter-spacing: 0.08em;
}

.file-preview h4 {
  margin: var(--space-4) 0 0;
  color: var(--text-primary);
  font-size: 16px;
}

.file-preview-meta {
  margin: var(--space-4) 0 0;
  color: var(--text-secondary);
  font-size: 10px;
  overflow-wrap: anywhere;
}

.file-close {
  flex: 0 0 auto;
  padding: 0 var(--space-10);
  border: 1px solid var(--border-control);
  background: transparent;
  color: var(--text-secondary);
  font-size: 11px;
}

.file-stats {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-6) var(--space-10);
  margin-top: var(--space-10);
  color: var(--text-secondary);
  font-size: 10px;
}

.csv-controls {
  display: grid;
  gap: var(--space-8);
  margin-top: var(--space-12);
  padding-top: var(--space-10);
  border-top: 1px solid var(--border);
}

.csv-control-group {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-6) var(--space-8);
}

.csv-control-label {
  color: var(--text-secondary);
  font-size: 11px;
  font-weight: 600;
}

.csv-header-state {
  color: var(--text-faint);
  font-size: 10px;
}

.csv-choice-buttons {
  display: flex;
  gap: var(--space-6);
}

.csv-choice {
  min-height: 28px;
  padding: 0 var(--space-10);
  border: 1px solid var(--border-control);
  border-radius: var(--radius-pill);
  background: transparent;
  color: var(--text-secondary);
  font: inherit;
  font-size: 10px;
  cursor: pointer;
}

.csv-choice.active {
  border-color: var(--brand-light);
  background: var(--brand-tint);
  color: var(--brand);
}

.csv-column-select {
  min-width: 0;
  max-width: 100%;
  min-height: 32px;
  padding: 0 var(--space-8);
  border: 1px solid var(--border-control);
  border-radius: var(--radius-sm);
  background: var(--surface-page);
  color: var(--text-primary);
  font: inherit;
  font-size: 11px;
}

.csv-hint {
  flex-basis: 100%;
  margin: 0;
  color: var(--text-faint);
  font-size: 10px;
  line-height: 1.6;
}

.csv-confirm {
  display: inline-flex;
  align-items: flex-start;
  gap: var(--space-6);
  color: var(--text-secondary);
  font-size: 10px;
  line-height: 1.5;
  cursor: pointer;
}

.csv-confirm input {
  margin-top: 2px;
  accent-color: var(--brand);
}

.file-errors {
  margin-top: var(--space-10);
  padding: var(--space-8) var(--space-10);
  border-left: 2px solid var(--danger);
  background: var(--danger-soft);
  color: var(--danger-text);
  font-size: 10px;
}

.file-errors-title {
  margin: 0 0 var(--space-4);
  font-weight: 700;
}

.file-errors ul {
  max-height: 120px;
  margin: 0;
  padding-left: var(--space-14);
  overflow: auto;
  line-height: 1.6;
}

.skip-errors {
  margin-top: var(--space-10);
}

.file-items {
  margin-top: var(--space-10);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  overflow: hidden;
}

.file-items-head,
.file-item {
  display: grid;
  grid-template-columns: minmax(100px, 0.9fr) minmax(0, 2fr) 52px 62px;
  gap: var(--space-8);
  align-items: center;
  padding: var(--space-6) var(--space-8);
  font-size: 10px;
}

.file-items-head {
  color: var(--text-faint);
  background: var(--surface-page);
}

.file-items ul {
  max-height: 230px;
  margin: 0;
  padding: 0;
  overflow: auto;
  list-style: none;
}

.file-item {
  border-top: 1px solid var(--border);
  color: var(--text-secondary);
}

.file-item.is-too-long {
  background: var(--danger-soft);
  color: var(--danger-text);
}

.file-item-source {
  color: var(--text-faint);
  font-variant-numeric: tabular-nums;
}

.file-item-title {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-item-length,
.file-item-duplicate {
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.file-item-duplicate {
  color: var(--warning-text);
}

.file-raw-preview {
  margin-top: var(--space-10);
  padding: var(--space-8);
  border: 1px solid var(--border);
  border-radius: var(--radius-md);
  color: var(--text-secondary);
  font-size: 10px;
}

.file-raw-title {
  margin: 0 0 var(--space-6);
}

.file-raw-preview ul {
  max-height: 150px;
  margin: 0;
  padding: 0;
  overflow: auto;
  list-style: none;
}

.file-raw-preview li {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-6);
  padding: var(--space-4) 0;
  border-bottom: 1px solid var(--border);
}

.raw-cell {
  padding: 1px var(--space-4);
  border-radius: var(--radius-sm);
  background: var(--surface-page);
  overflow-wrap: anywhere;
}

.file-preview-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--space-8);
  margin-top: var(--space-14);
}

.file-cancel,
.file-confirm {
  padding: 0 var(--space-14);
  font-size: 11px;
}

.file-cancel {
  border: 1px solid var(--border-control);
  background: transparent;
  color: var(--text-secondary);
}

.file-confirm {
  border: 1px solid var(--brand);
  background: var(--brand);
  color: #fff;
}

.file-confirm:hover:not(:disabled) {
  background: var(--brand-dark, var(--brand));
}

.replace-dialog {
  position: fixed;
  inset: 0;
  z-index: 80;
  display: grid;
  place-items: center;
  padding: var(--space-16);
  background: rgba(10, 18, 30, 0.46);
}

.replace-card {
  width: min(420px, 100%);
  padding: var(--space-20);
  border: 1px solid var(--border-card);
  border-radius: var(--radius-lg);
  background: var(--surface-card);
  box-shadow: var(--shadow-lg);
}

.replace-card h5 {
  margin: 0;
  color: var(--text-primary);
  font-size: 16px;
}

.replace-card p {
  margin: var(--space-8) 0 0;
  color: var(--text-secondary);
  font-size: 11px;
  line-height: 1.7;
}

.replace-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: var(--space-8);
  margin-top: var(--space-16);
}

@media (max-width: 760px) {
  .file-preview {
    padding: var(--space-12);
  }

  .file-items-head,
  .file-item {
    grid-template-columns: minmax(84px, 0.9fr) minmax(0, 1.8fr) 46px;
  }

  .file-items-head span:last-child,
  .file-item-duplicate {
    display: none;
  }

  .file-preview-actions {
    justify-content: stretch;
  }

  .file-cancel,
  .file-confirm {
    flex: 1 1 140px;
  }
}
</style>
