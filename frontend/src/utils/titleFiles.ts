/**
 * 阶段 3A：浏览器本地 TXT/CSV 解码与解析纯函数。
 *
 * 约束：
 * - 不依赖 Vue / DOM UI / 网络 / 存储；输入是文件名字节数组；
 * - 文件最大 1 MiB；最多 1000 条数据记录；最多 100 列；
 * - 编码：UTF-8（含 BOM），严格 UTF-8 失败后回退 GB18030，不支持 UTF-16；
 * - CSV 使用 RFC4180 状态机，不使用 split(',')；
 * - 结果中的 items 只包含非空标题；超长标题仍保留在 items 中并标记，
 *   同时进入 errors，importable=false，不静默丢弃；
 * - 本模块不读取真实文件、不写日志、不持久化原始字节。
 */

import {
  BATCH_ITEM_MAX_CHARS,
  BATCH_MAX_ITEMS,
  BATCH_MAX_TOTAL_CHARS,
  countCodePoints,
  normalizeLineBreaks,
} from './batchTitles'

export const MAX_TITLE_FILE_BYTES = 1024 * 1024
export const MAX_TITLE_FILE_RECORDS = 1000
/** 单个 CSV 字段的硬保护（UTF-16 code unit）；远大于 200 的标题上限。 */
export const MAX_CSV_FIELD_UNITS = 10_000
/** 单个 CSV 记录允许的列数（硬保护；标题选择只需其中一列）。 */
export const MAX_CSV_COLUMNS = 100
/** CSV 预览最多返回的数据行数（供列选择/抽样展示；完整 items 在选择列后返回）。 */
export const MAX_CSV_PREVIEW_ROWS = 20

export type TitleFileFormat = 'txt' | 'csv'
export type TitleFileEncoding = 'utf-8' | 'gb18030'

export type TitleFileErrorCode =
  | 'UNSUPPORTED_EXTENSION'
  | 'FILE_TOO_LARGE'
  | 'UNSUPPORTED_ENCODING'
  | 'INVALID_ENCODING'
  | 'BINARY_CONTENT'
  | 'TOO_MANY_RECORDS'
  | 'TOO_MANY_COLUMNS'
  | 'FIELD_TOO_LARGE'
  | 'CSV_PARSE_ERROR'
  | 'CSV_UNCLOSED_QUOTE'
  | 'CSV_INVALID_QUOTE'
  | 'CSV_FIELD_COUNT_MISMATCH'
  | 'COLUMN_INVALID'
  | 'NO_VALID_TITLES'
  | 'TITLE_TOO_LONG'
  | 'TOO_MANY_TITLES'
  | 'TOTAL_CHARS_EXCEEDED'

export type TitleFileStatus =
  | 'ok'
  | TitleFileErrorCode
  | 'COLUMN_SELECTION_REQUIRED'
  | 'HEADER_CONFIRMATION_REQUIRED'

export interface TitleFileIssue {
  code: TitleFileErrorCode
  message: string
  /** 1 基物理行号（CSV 为记录起始物理行）。 */
  lineNo?: number
  /** 1 基 CSV 数据记录号（不含表头；TXT 不提供）。 */
  recordNo?: number
  columnIndex?: number
}

export interface TitleFileItem {
  itemId: string
  title: string
  charCount: number
  /** 1 基物理行号；CSV 为记录起始行。 */
  lineNo: number
  /** 1 基 CSV 数据记录号；TXT 不提供。 */
  recordNo?: number
  tooLong: boolean
  duplicateCount: number
}

export interface CsvColumnInfo {
  index: number
  name: string
  sample: string
  titleCandidate: boolean
}

export interface CsvPreviewRow {
  recordNo: number
  lineNo: number
  cells: string[]
}

export interface HeaderInfo {
  used: boolean
  /** 是否由候选列名自动识别为表头。 */
  detected: boolean
  /** 是否为“无法确定、需 UI 确认”的表头状态。 */
  confirmationRequired: boolean
}

export interface ColumnSelectionInfo {
  selectedIndex: number | null
  source: 'single' | 'candidate' | 'default' | 'explicit' | 'none'
  confirmationRequired: boolean
}

export interface TitleFileParseResult {
  ok: boolean
  importable: boolean
  format: TitleFileFormat | null
  encoding: TitleFileEncoding | null
  hadBom: boolean
  items: TitleFileItem[]
  validItemCount: number
  /** 解析到的数据记录数（CSV 不含表头/空白记录；TXT 为非空行数）。 */
  recordCount: number
  totalChars: number
  blankRecordCount: number
  emptyTitleCount: number
  duplicateExtraCount: number
  duplicateGroupCount: number
  tooLongCount: number
  columns: CsvColumnInfo[]
  header: HeaderInfo
  columnSelection: ColumnSelectionInfo
  errors: TitleFileIssue[]
  previewRows: CsvPreviewRow[]
  status: TitleFileStatus
}

export interface TitleFileInput {
  name: string
  bytes: ArrayBuffer | Uint8Array
}

export interface TitleFileOptions {
  /** CSV 显式表头选择；省略时自动识别，无法确定则要求 UI 确认。 */
  header?: boolean
  /** CSV 显式标题列；省略时按单列/候选列/默认列规则推断。 */
  columnIndex?: number
}

interface CsvRawRecord {
  recordNo: number
  startLineNo: number
  fields: string[]
  quoted: boolean
  blank: boolean
}

interface CsvRawParse {
  records: CsvRawRecord[]
  errors: TitleFileIssue[]
}

interface DecodeResult {
  ok: boolean
  encoding: TitleFileEncoding | null
  hadBom: boolean
  text: string
  error?: TitleFileIssue
}

const HEADER_NAMES = [
  /^标题$/,
  /^新闻标题$/,
  /^文章标题$/,
  /^题目$/,
  /^title$/i,
  /^headline$/i,
  /^head$/i,
  /^article[_-]?title$/i,
]

function isHeaderName(value: string): boolean {
  const normalized = value.trim()
  if (!normalized) return false
  return HEADER_NAMES.some((pattern) => pattern.test(normalized))
}

function emptyResult(format: TitleFileFormat | null): TitleFileParseResult {
  return {
    ok: false,
    importable: false,
    format,
    encoding: null,
    hadBom: false,
    items: [],
    validItemCount: 0,
    recordCount: 0,
    totalChars: 0,
    blankRecordCount: 0,
    emptyTitleCount: 0,
    duplicateExtraCount: 0,
    duplicateGroupCount: 0,
    tooLongCount: 0,
    columns: [],
    header: { used: false, detected: false, confirmationRequired: false },
    columnSelection: {
      selectedIndex: null,
      source: 'none',
      confirmationRequired: false,
    },
    errors: [],
    previewRows: [],
    status: 'ok',
  }
}

function failResult(
  format: TitleFileFormat | null,
  code: TitleFileErrorCode,
  message: string,
): TitleFileParseResult {
  const result = emptyResult(format)
  result.errors.push({ code, message })
  result.status = code
  return result
}

function asBytes(input: ArrayBuffer | Uint8Array): Uint8Array | null {
  try {
    if (input instanceof Uint8Array) {
      return new Uint8Array(input.buffer, input.byteOffset, input.byteLength)
    }
    if (input instanceof ArrayBuffer) {
      return new Uint8Array(input)
    }
  } catch {
    return null
  }
  return null
}

function decodeWith(
  bytes: Uint8Array,
  label: TitleFileEncoding,
): { text: string; error?: TitleFileIssue } {
  try {
    const decoder = new TextDecoder(label, { fatal: true })
    return { text: decoder.decode(bytes) }
  } catch (error) {
    if (error instanceof RangeError) {
      return {
        text: '',
        error: {
          code: 'UNSUPPORTED_ENCODING',
          message: `当前浏览器不支持 ${label} 解码`,
        },
      }
    }
    return {
      text: '',
      error: {
        code: 'INVALID_ENCODING',
        message: `${label} 解码失败，文件可能不是该编码或已损坏`,
      },
    }
  }
}

/** 检测并解码文件字节；不做 TXT/CSV 业务解析。 */
export function decodeTitleFileBytes(
  input: ArrayBuffer | Uint8Array,
): DecodeResult {
  const bytes = asBytes(input)
  if (!bytes) {
    return {
      ok: false,
      encoding: null,
      hadBom: false,
      text: '',
      error: {
        code: 'INVALID_ENCODING',
        message: '文件字节无法读取',
      },
    }
  }

  if (bytes.byteLength >= 2) {
    const first = bytes[0]
    const second = bytes[1]
    if (
      (first === 0xff && second === 0xfe)
      || (first === 0xfe && second === 0xff)
    ) {
      return {
        ok: false,
        encoding: null,
        hadBom: false,
        text: '',
        error: {
          code: 'UNSUPPORTED_ENCODING',
          message: '不支持 UTF-16 文件；请另存为 UTF-8 或 GB18030',
        },
      }
    }
  }

  const hasUtf8Bom =
    bytes.byteLength >= 3
    && bytes[0] === 0xef
    && bytes[1] === 0xbb
    && bytes[2] === 0xbf

  if (hasUtf8Bom) {
    const decoded = decodeWith(bytes.subarray(3), 'utf-8')
    if (decoded.error) {
      return {
        ok: false,
        encoding: null,
        hadBom: true,
        text: '',
        error: decoded.error,
      }
    }
    return {
      ok: true,
      encoding: 'utf-8',
      hadBom: true,
      text: decoded.text,
    }
  }

  const utf8 = decodeWith(bytes, 'utf-8')
  if (!utf8.error) {
    return {
      ok: true,
      encoding: 'utf-8',
      hadBom: false,
      text: utf8.text,
    }
  }

  const gb = decodeWith(bytes, 'gb18030')
  if (gb.error) {
    // 如果 GB18030 本身不可用，返回更明确的“不支持编码”。
    if (gb.error.code === 'UNSUPPORTED_ENCODING') {
      return {
        ok: false,
        encoding: null,
        hadBom: false,
        text: '',
        error: gb.error,
      }
    }
    return {
      ok: false,
      encoding: null,
      hadBom: false,
      text: '',
      error: {
        code: 'INVALID_ENCODING',
        message: '无法按 UTF-8 或 GB18030 解码文件',
      },
    }
  }
  return {
    ok: true,
    encoding: 'gb18030',
    hadBom: false,
    text: gb.text,
  }
}

function hasBinaryControls(text: string): boolean {
  for (let index = 0; index < text.length; index += 1) {
    const code = text.charCodeAt(index)
    if (code === 0 || (code >= 1 && code <= 8) || code === 11 || code === 12
      || (code >= 14 && code <= 31) || code === 127) {
      return true
    }
  }
  return false
}

export function fileFormatFromName(name: string): TitleFileFormat | null {
  const trimmed = name.trim()
  const dot = trimmed.lastIndexOf('.')
  if (dot < 0 || dot === trimmed.length - 1) return null
  const extension = trimmed.slice(dot + 1).toLowerCase()
  if (extension === 'txt') return 'txt'
  if (extension === 'csv') return 'csv'
  return null
}

function buildItem(
  title: string,
  lineNo: number,
  recordNo?: number,
): Omit<TitleFileItem, 'duplicateCount'> {
  const charCount = countCodePoints(title)
  return {
    itemId: `r${String(lineNo).padStart(6, '0')}`,
    title,
    charCount,
    lineNo,
    ...(recordNo === undefined ? {} : { recordNo }),
    tooLong: charCount > BATCH_ITEM_MAX_CHARS,
  }
}

function finalizeItems(
  rawItems: Array<Omit<TitleFileItem, 'duplicateCount'>>,
): {
  items: TitleFileItem[]
  totalChars: number
  validItemCount: number
  duplicateExtraCount: number
  duplicateGroupCount: number
  tooLongCount: number
} {
  const counts = new Map<string, number>()
  for (const item of rawItems) {
    counts.set(item.title, (counts.get(item.title) ?? 0) + 1)
  }
  const items: TitleFileItem[] = rawItems.map((item) => ({
    ...item,
    duplicateCount: counts.get(item.title) ?? 1,
  }))
  return {
    items,
    totalChars: items.reduce((total, item) => total + item.charCount, 0),
    validItemCount: items.filter((item) => !item.tooLong).length,
    duplicateExtraCount: Math.max(0, items.length - counts.size),
    duplicateGroupCount: [...counts.values()].filter((count) => count > 1).length,
    tooLongCount: items.filter((item) => item.tooLong).length,
  }
}

function addImportLimitErrors(
  result: TitleFileParseResult,
  items: TitleFileItem[],
  totalChars: number,
): void {
  if (items.length > BATCH_MAX_ITEMS) {
    result.errors.push({
      code: 'TOO_MANY_TITLES',
      message: `有效标题 ${items.length} 条，超过 ${BATCH_MAX_ITEMS} 条上限`,
    })
  }
  if (totalChars > BATCH_MAX_TOTAL_CHARS) {
    result.errors.push({
      code: 'TOTAL_CHARS_EXCEEDED',
      message: `标题总字符 ${totalChars} 超过 ${BATCH_MAX_TOTAL_CHARS} 上限`,
    })
  }
  for (const item of items) {
    if (!item.tooLong) continue
    result.errors.push({
      code: 'TITLE_TOO_LONG',
      message: `第 ${item.lineNo} 行标题超过 ${BATCH_ITEM_MAX_CHARS} 个字符`,
      lineNo: item.lineNo,
      recordNo: item.recordNo,
    })
  }
}

function finalizeResult(
  result: TitleFileParseResult,
  items: TitleFileItem[],
  totalChars: number,
  duplicateExtraCount: number,
  duplicateGroupCount: number,
  tooLongCount: number,
): TitleFileParseResult {
  result.items = items
  result.totalChars = totalChars
  result.validItemCount = items.filter((item) => !item.tooLong).length
  result.duplicateExtraCount = duplicateExtraCount
  result.duplicateGroupCount = duplicateGroupCount
  result.tooLongCount = tooLongCount
  addImportLimitErrors(result, items, totalChars)
  if (
    items.length === 0
    && !result.errors.some((error) => error.code === 'NO_VALID_TITLES')
  ) {
    result.errors.push({
      code: 'NO_VALID_TITLES',
      message: '未解析到有效标题',
    })
  }
  result.ok = result.errors.length === 0
  const hasBlockingErrors = result.errors.length > 0
  result.importable =
    result.ok
    && !result.header.confirmationRequired
    && !result.columnSelection.confirmationRequired
  if (hasBlockingErrors) {
    result.status = result.errors[0]?.code || 'CSV_PARSE_ERROR'
  } else if (result.columnSelection.confirmationRequired) {
    result.status = 'COLUMN_SELECTION_REQUIRED'
  } else if (result.header.confirmationRequired) {
    result.status = 'HEADER_CONFIRMATION_REQUIRED'
  } else {
    result.status = 'ok'
  }
  return result
}

function parseTxt(text: string): TitleFileParseResult {
  const result = emptyResult('txt')
  const lines = normalizeLineBreaks(text).split('\n')
  const nonEmpty: Array<{ title: string; lineNo: number }> = []
  let blankRecordCount = 0
  for (let index = 0; index < lines.length; index += 1) {
    const title = lines[index].trim()
    if (!title) {
      blankRecordCount += 1
      continue
    }
    nonEmpty.push({ title, lineNo: index + 1 })
  }
  result.blankRecordCount = blankRecordCount
  result.recordCount = nonEmpty.length
  if (nonEmpty.length > MAX_TITLE_FILE_RECORDS) {
    result.errors.push({
      code: 'TOO_MANY_RECORDS',
      message: `数据记录 ${nonEmpty.length} 条，超过 ${MAX_TITLE_FILE_RECORDS} 条解析上限`,
    })
    result.status = 'TOO_MANY_RECORDS'
    return result
  }
  const rawItems = nonEmpty.map((entry) => buildItem(entry.title, entry.lineNo))
  const stats = finalizeItems(rawItems)
  return finalizeResult(
    result,
    stats.items,
    stats.totalChars,
    stats.duplicateExtraCount,
    stats.duplicateGroupCount,
    stats.tooLongCount,
  )
}

function parseCsvRaw(text: string): CsvRawParse {
  const normalized = normalizeLineBreaks(text)
  const records: CsvRawRecord[] = []
  const errors: TitleFileIssue[] = []
  let lineNo = 1
  let startLineNo = 1
  let rawRecordNo = 1
  let field = ''
  let fields: string[] = []
  let inQuotes = false
  let afterQuote = false
  let quoted = false
  let fatal = false

  function pushField(): void {
    if (field.length > MAX_CSV_FIELD_UNITS) {
      errors.push({
        code: 'FIELD_TOO_LARGE',
        message: `第 ${lineNo} 行字段超过 ${MAX_CSV_FIELD_UNITS} 个字符的保护上限`,
        lineNo,
      })
      fatal = true
      return
    }
    fields.push(field)
    field = ''
  }

  function finishRecord(): void {
    pushField()
    if (fatal) return
    if (fields.length > MAX_CSV_COLUMNS) {
      errors.push({
        code: 'TOO_MANY_COLUMNS',
        message: `第 ${startLineNo} 行字段数 ${fields.length} 超过 ${MAX_CSV_COLUMNS} 列上限`,
        lineNo: startLineNo,
      })
      fatal = true
      return
    }
    const blank = fields.length === 1 && fields[0].trim() === '' && !quoted
    records.push({
      recordNo: rawRecordNo,
      startLineNo,
      fields,
      quoted,
      blank,
    })
    rawRecordNo += 1
    fields = []
    quoted = false
    if (records.length > MAX_TITLE_FILE_RECORDS + 1) {
      errors.push({
        code: 'TOO_MANY_RECORDS',
        message: `数据记录超过 ${MAX_TITLE_FILE_RECORDS} 条解析上限`,
        lineNo: startLineNo,
      })
      fatal = true
    }
  }

  for (let index = 0; index < normalized.length && !fatal; index += 1) {
    const char = normalized[index]
    if (inQuotes) {
      if (char === '"') {
        if (normalized[index + 1] === '"') {
          field += '"'
          index += 1
        } else {
          inQuotes = false
          afterQuote = true
        }
      } else {
        field += char
        if (char === '\n') lineNo += 1
      }
      if (field.length > MAX_CSV_FIELD_UNITS) {
        errors.push({
          code: 'FIELD_TOO_LARGE',
          message: `第 ${lineNo} 行字段超过 ${MAX_CSV_FIELD_UNITS} 个字符的保护上限`,
          lineNo,
        })
        fatal = true
        break
      }
      continue
    }
    if (afterQuote) {
      if (char === ',') {
        afterQuote = false
        pushField()
      } else if (char === '\n') {
        afterQuote = false
        finishRecord()
        lineNo += 1
        startLineNo = lineNo
      } else {
        errors.push({
          code: 'CSV_INVALID_QUOTE',
          message: `第 ${lineNo} 行结束引号后存在非法字符`,
          lineNo,
        })
        field += char
        afterQuote = false
      }
      continue
    }
    if (char === '"') {
      if (field.length === 0) {
        inQuotes = true
        quoted = true
      } else {
        errors.push({
          code: 'CSV_INVALID_QUOTE',
          message: `第 ${lineNo} 行字段中出现未转义的双引号`,
          lineNo,
        })
        field += char
      }
    } else if (char === ',') {
      pushField()
    } else if (char === '\n') {
      finishRecord()
      lineNo += 1
      startLineNo = lineNo
    } else {
      field += char
    }
    if (field.length > MAX_CSV_FIELD_UNITS) {
      errors.push({
        code: 'FIELD_TOO_LARGE',
        message: `第 ${lineNo} 行字段超过 ${MAX_CSV_FIELD_UNITS} 个字符的保护上限`,
        lineNo,
      })
      fatal = true
      break
    }
  }

  if (inQuotes && !fatal) {
    errors.push({
      code: 'CSV_UNCLOSED_QUOTE',
      message: `第 ${startLineNo} 行引号未闭合`,
      lineNo: startLineNo,
    })
  } else if (!fatal && (field.length > 0 || fields.length > 0 || afterQuote)) {
    finishRecord()
  }

  return { records, errors }
}

function truncate(value: string, max: number): string {
  if (value.length <= max) return value
  return `${value.slice(0, max)}…`
}

function parseCsv(
  text: string,
  options: TitleFileOptions,
): TitleFileParseResult {
  const result = emptyResult('csv')
  const raw = parseCsvRaw(text)
  result.errors.push(...raw.errors)

  const nonBlankRecords = raw.records.filter((record) => !record.blank)
  result.blankRecordCount = raw.records.length - nonBlankRecords.length
  if (nonBlankRecords.length === 0) {
    if (raw.errors.length > 0) {
      result.status = raw.errors[0]?.code ?? 'CSV_PARSE_ERROR'
      return result
    }
    result.errors.push({
      code: 'NO_VALID_TITLES',
      message: 'CSV 中没有可解析的数据记录',
    })
    result.status = 'NO_VALID_TITLES'
    return result
  }

  const automaticHeader = nonBlankRecords[0].fields.some((cell) =>
    isHeaderName(cell),
  )
  const headerUsed = options.header ?? automaticHeader
  const headerConfirmationRequired =
    options.header === undefined && !automaticHeader
  result.header = {
    used: headerUsed,
    detected: automaticHeader,
    confirmationRequired: headerConfirmationRequired,
  }

  const dataRecords = headerUsed ? nonBlankRecords.slice(1) : nonBlankRecords
  result.recordCount = dataRecords.length
  if (dataRecords.length > MAX_TITLE_FILE_RECORDS) {
    result.errors.push({
      code: 'TOO_MANY_RECORDS',
      message: `数据记录 ${dataRecords.length} 条，超过 ${MAX_TITLE_FILE_RECORDS} 条解析上限`,
    })
  }
  const firstData = dataRecords[0]
  const columnCount = headerUsed
    ? nonBlankRecords[0].fields.length
    : firstData?.fields.length ?? 0
  const columnNames = headerUsed
    ? nonBlankRecords[0].fields.map((cell) => cell.trim())
    : Array.from({ length: columnCount }, (_, index) => `第${index + 1}列`)
  const candidateIndices = columnNames
    .map((name, index) => (isHeaderName(name) ? index : -1))
    .filter((index) => index >= 0)

  result.columns = columnNames.map((name, index) => {
    const sampleCell = firstData?.fields[index] ?? ''
    return {
      index,
      name: name || `第${index + 1}列`,
      sample: truncate((sampleCell?.trim() ?? ''), 80),
      titleCandidate: isHeaderName(name),
    }
  })

  let selectedIndex: number | null = null
  let source: ColumnSelectionInfo['source'] = 'none'
  let columnConfirmationRequired = false
  if (options.columnIndex !== undefined) {
    if (
      Number.isInteger(options.columnIndex)
      && options.columnIndex >= 0
      && options.columnIndex < columnCount
    ) {
      selectedIndex = options.columnIndex
      source = 'explicit'
    } else {
      result.errors.push({
        code: 'COLUMN_INVALID',
        message: `标题列索引 ${options.columnIndex} 超出范围`,
        columnIndex: options.columnIndex,
      })
    }
  } else if (columnCount === 1) {
    selectedIndex = 0
    source = 'single'
  } else if (candidateIndices.length === 1) {
    selectedIndex = candidateIndices[0]
    source = 'candidate'
  } else if (candidateIndices.length > 1) {
    columnConfirmationRequired = true
  } else {
    selectedIndex = 0
    source = 'default'
    columnConfirmationRequired = true
  }
  result.columnSelection = {
    selectedIndex,
    source,
    confirmationRequired: columnConfirmationRequired,
  }

  result.previewRows = dataRecords.slice(0, MAX_CSV_PREVIEW_ROWS).map(
    (record, index) => ({
      recordNo: index + 1,
      lineNo: record.startLineNo,
      cells: record.fields.map((cell) => truncate(cell, 200)),
    }),
  )

  if (headerUsed && dataRecords.length === 0) {
    result.errors.push({
      code: 'NO_VALID_TITLES',
      message: 'CSV 只有表头，没有数据记录',
    })
    return finalizeResult(result, [], 0, 0, 0, 0)
  }

  const rawItems: Array<Omit<TitleFileItem, 'duplicateCount'>> = []
  let emptyTitleCount = 0
  let expectedFields = columnCount
  dataRecords.forEach((record, index) => {
    const recordNo = index + 1
    if (record.fields.length !== expectedFields) {
      result.errors.push({
        code: 'CSV_FIELD_COUNT_MISMATCH',
        message: `第 ${record.startLineNo} 行字段数 ${record.fields.length} 与预期 ${expectedFields} 不一致`,
        lineNo: record.startLineNo,
        recordNo,
      })
    }
    if (selectedIndex === null) return
    const title = (record.fields[selectedIndex] ?? '').trim()
    if (!title) {
      emptyTitleCount += 1
      return
    }
    rawItems.push(buildItem(title, record.startLineNo, recordNo))
  })
  result.emptyTitleCount = emptyTitleCount
  if (selectedIndex === null) {
    // 多候选列：不猜测；仍返回 columns/previewRows 供后续 UI 选择列。
    result.ok = result.errors.length === 0
    result.importable = false
    result.status = result.errors[0]?.code
      ?? 'COLUMN_SELECTION_REQUIRED'
    return result
  }

  const stats = finalizeItems(rawItems)
  return finalizeResult(
    result,
    stats.items,
    stats.totalChars,
    stats.duplicateExtraCount,
    stats.duplicateGroupCount,
    stats.tooLongCount,
  )
}

/**
 * 解析文件字节。
 *
 * - 扩展名不区分大小写；
 * - 先检查字节长度，再解码；
 * - 不读取文件对象、不保留原始字节。
 */
export function parseTitleFile(
  input: TitleFileInput,
  options: TitleFileOptions = {},
): TitleFileParseResult {
  const format = fileFormatFromName(input.name)
  if (!format) {
    return failResult(
      null,
      'UNSUPPORTED_EXTENSION',
      '仅支持 .txt 和 .csv 文件',
    )
  }
  const bytes = asBytes(input.bytes)
  if (!bytes) {
    return failResult(format, 'INVALID_ENCODING', '文件字节无法读取')
  }
  if (bytes.byteLength > MAX_TITLE_FILE_BYTES) {
    return failResult(
      format,
      'FILE_TOO_LARGE',
      `文件超过 ${MAX_TITLE_FILE_BYTES} 字节上限`,
    )
  }

  const decoded = decodeTitleFileBytes(bytes)
  if (!decoded.ok) {
    const result = emptyResult(format)
    result.errors.push(
      decoded.error ?? {
        code: 'INVALID_ENCODING',
        message: '文件解码失败',
      },
    )
    result.status = result.errors[0]?.code ?? 'INVALID_ENCODING'
    return result
  }
  if (hasBinaryControls(decoded.text)) {
    return failResult(
      format,
      'BINARY_CONTENT',
      '文件包含二进制控制字符，无法作为文本解析',
    )
  }

  const result =
    format === 'txt' ? parseTxt(decoded.text) : parseCsv(decoded.text, options)
  result.format = format
  result.encoding = decoded.encoding
  result.hadBom = decoded.hadBom
  return result
}
