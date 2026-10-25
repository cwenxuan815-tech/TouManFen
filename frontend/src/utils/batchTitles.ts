/**
 * 阶段 2B：多条标题输入的纯解析/校验工具。
 *
 * - 一行一条标题；统一 CRLF/CR/LF；每行 trim；空行忽略；
 * - 重复标题保留；item_id 由原始行号生成，保证批内唯一且可映射；
 * - 字符统一按 Unicode code point 计数，与 Python len() 对齐；
 * - 单批 1–100 条、每条 ≤200 code point、总计 ≤20,000 code point。
 */

export const BATCH_ITEM_MAX_CHARS = 200
export const BATCH_MAX_ITEMS = 100
export const BATCH_MAX_TOTAL_CHARS = 20_000
export const BATCH_CHUNK_SIZE = 10
export const BATCH_LLM_MODEL_ID = 'llm'

export interface ParsedBatchItem {
  /** 稳定映射键：由原始行号生成，例如 r000003。 */
  itemId: string
  /** 原始输入中的 1 基行号（空行也占行号）。 */
  lineNo: number
  title: string
  charCount: number
  duplicateCount: number
  tooLong: boolean
}

export interface BatchParseResult {
  items: ParsedBatchItem[]
  totalChars: number
  emptyLineCount: number
  duplicateExtraCount: number
  duplicateGroupCount: number
  tooLongCount: number
}

export interface BatchValidation {
  ok: boolean
  code: '' | 'empty' | 'too_many' | 'too_long' | 'total_chars' | 'llm' | 'model' | 'models_error'
  message: string
}

/** 与 Python len() 对齐的 Unicode code point 计数（不是 UTF-16 .length）。 */
export function countCodePoints(value: string): number {
  return Array.from(value).length
}

export function normalizeLineBreaks(value: string): string {
  return value.replace(/\r\n?/g, '\n')
}

export function parseBatchTitles(input: string): BatchParseResult {
  const lines = normalizeLineBreaks(input).split('\n')
  const raw: Array<{ lineNo: number; title: string }> = []
  let emptyLineCount = 0

  lines.forEach((line, index) => {
    const title = line.trim()
    if (!title) {
      emptyLineCount += 1
      return
    }
    raw.push({ lineNo: index + 1, title })
  })

  const countByTitle = new Map<string, number>()
  for (const entry of raw) {
    countByTitle.set(entry.title, (countByTitle.get(entry.title) ?? 0) + 1)
  }

  const items: ParsedBatchItem[] = raw.map((entry) => {
    const charCount = countCodePoints(entry.title)
    return {
      itemId: `r${String(entry.lineNo).padStart(6, '0')}`,
      lineNo: entry.lineNo,
      title: entry.title,
      charCount,
      duplicateCount: countByTitle.get(entry.title) ?? 1,
      tooLong: charCount > BATCH_ITEM_MAX_CHARS,
    }
  })

  const duplicateExtraCount = Math.max(0, items.length - countByTitle.size)
  const duplicateGroupCount = [...countByTitle.values()].filter(
    (count) => count > 1,
  ).length

  return {
    items,
    totalChars: items.reduce((total, item) => total + item.charCount, 0),
    emptyLineCount,
    duplicateExtraCount,
    duplicateGroupCount,
    tooLongCount: items.filter((item) => item.tooLong).length,
  }
}

export function validateBatch(
  parsed: BatchParseResult,
  modelId: string,
  modelsError = '',
): BatchValidation {
  if (modelsError) {
    return { ok: false, code: 'models_error', message: modelsError }
  }
  if (!modelId) {
    return { ok: false, code: 'model', message: '请选择一个分类模型。' }
  }
  if (modelId === BATCH_LLM_MODEL_ID) {
    return {
      ok: false,
      code: 'llm',
      message: '多条分类暂不支持大模型，请选择本地模型。',
    }
  }
  if (parsed.items.length === 0) {
    return { ok: false, code: 'empty', message: '请至少输入一条标题。' }
  }
  if (parsed.items.length > BATCH_MAX_ITEMS) {
    return {
      ok: false,
      code: 'too_many',
      message: `一批最多 ${BATCH_MAX_ITEMS} 条标题，当前 ${parsed.items.length} 条。`,
    }
  }
  if (parsed.tooLongCount > 0) {
    return {
      ok: false,
      code: 'too_long',
      message: `有 ${parsed.tooLongCount} 条标题超过 ${BATCH_ITEM_MAX_CHARS} 个字符，请修改后再提交。`,
    }
  }
  if (parsed.totalChars > BATCH_MAX_TOTAL_CHARS) {
    return {
      ok: false,
      code: 'total_chars',
      message: `标题总字符数 ${parsed.totalChars} 超过上限 ${BATCH_MAX_TOTAL_CHARS}。`,
    }
  }
  return { ok: true, code: '', message: '' }
}

export function chunkBatchItems<T>(items: readonly T[], size = BATCH_CHUNK_SIZE): T[][] {
  const safeSize = Math.max(1, Math.floor(size))
  const chunks: T[][] = []
  for (let index = 0; index < items.length; index += safeSize) {
    chunks.push(items.slice(index, index + safeSize))
  }
  return chunks
}

/** 客户端批次关联 ID；只使用后端允许的 [A-Za-z0-9._-]，不持久化。 */
export function createBatchId(): string {
  const random = Math.random().toString(36).slice(2, 8)
  return `batch-${Date.now().toString(36)}-${random}`
}
