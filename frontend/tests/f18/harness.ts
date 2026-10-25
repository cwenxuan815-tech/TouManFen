/**
 * F18 阶段 3A harness：在真实浏览器中验证 titleFiles 纯函数。
 * 不读真实文件、不访问网络、不依赖 UI。
 */
import {
  MAX_CSV_COLUMNS,
  MAX_CSV_FIELD_UNITS,
  MAX_TITLE_FILE_BYTES,
  decodeTitleFileBytes,
  parseTitleFile,
  type TitleFileOptions,
  type TitleFileParseResult,
} from '../../src/utils/titleFiles'

interface Check {
  name: string
  pass: boolean
  detail: string
}

const checks: Check[] = []
const encoder = new TextEncoder()

function preview(value: unknown, max = 360): string {
  let text: string
  try {
    text = typeof value === 'string' ? value : JSON.stringify(value)
  } catch {
    text = String(value)
  }
  return text.length > max ? `${text.slice(0, max)}…` : text
}

function check(name: string, pass: boolean, detail: unknown = ''): void {
  checks.push({ name, pass: Boolean(pass), detail: preview(detail) })
}

function eq(name: string, actual: unknown, expected: unknown): void {
  const left = JSON.stringify(actual)
  const right = JSON.stringify(expected)
  check(name, left === right, `actual=${left} expected=${right}`)
}

function utf8(value: string): Uint8Array {
  return encoder.encode(value)
}

function join(...parts: Uint8Array[]): Uint8Array {
  const total = parts.reduce((sum, part) => sum + part.length, 0)
  const result = new Uint8Array(total)
  let offset = 0
  for (const part of parts) {
    result.set(part, offset)
    offset += part.length
  }
  return result
}

function parse(
  name: string,
  bytes: Uint8Array,
  options: TitleFileOptions = {},
): TitleFileParseResult {
  return parseTitleFile({ name, bytes }, options)
}

function codes(result: TitleFileParseResult): string[] {
  return result.errors.map((error) => error.code)
}

function hasCode(result: TitleFileParseResult, code: string): boolean {
  return codes(result).includes(code)
}

function csv(value: string): Uint8Array {
  return utf8(value)
}

/** 已知 GB18030/GBK 字节："标题"=B1EA CCE2，"中文"=D6D0 CEC4。 */
const GB_TITLE = Uint8Array.from([0xb1, 0xea, 0xcc, 0xe2])
const GB_CHINESE = Uint8Array.from([0xd6, 0xd0, 0xce, 0xc4])

function runEncodingTests(): void {
  const gbSupported = (() => {
    try {
      return new TextDecoder('gb18030').decode(GB_CHINESE) === '中文'
    } catch {
      return false
    }
  })()
  check('浏览器原生 TextDecoder(gb18030) 可用', gbSupported, 'Edge/Chromium 目标')

  const plain = parse('a.txt', utf8('甲\n乙'))
  eq('UTF-8 无 BOM 编码', plain.encoding, 'utf-8')
  eq('UTF-8 无 BOM hadBom=false', plain.hadBom, false)
  eq('UTF-8 无 BOM 标题', plain.items.map((item) => item.title), ['甲', '乙'])

  const bom = parse('a.txt', join(Uint8Array.from([0xef, 0xbb, 0xbf]), utf8('甲\n乙')))
  eq('UTF-8 BOM 编码', bom.encoding, 'utf-8')
  eq('UTF-8 BOM hadBom=true', bom.hadBom, true)
  eq('UTF-8 BOM 首条无 BOM 残留', bom.items[0]?.title, '甲')

  const gb = parse('gb.txt', join(GB_TITLE, Uint8Array.from([0x0a]), GB_CHINESE))
  eq('GB18030 回退编码', gb.encoding, 'gb18030')
  eq('GB18030 标题解码', gb.items.map((item) => item.title), ['标题', '中文'])

  const invalid = parse('bad.txt', Uint8Array.from([0xff, 0xff]))
  eq('UTF-8/GB18030 均失败状态', invalid.status, 'INVALID_ENCODING')
  eq('无效编码 importable=false', invalid.importable, false)
  eq('无效编码无 items', invalid.items.length, 0)

  const utf16 = parse('u16.txt', Uint8Array.from([0xff, 0xfe, 0x41, 0x00]))
  eq('UTF-16 BOM 被拒绝', utf16.status, 'UNSUPPORTED_ENCODING')

  const binary = parse('bin.txt', utf8('A\u0000B'))
  eq('二进制控制字符被拒绝', binary.status, 'BINARY_CONTENT')

  const upper = parse('A.TXT', utf8('甲'))
  eq('扩展名大小写不敏感', upper.status, 'ok')
  eq('大写扩展名格式', upper.format, 'txt')

  const unsupported = parse('a.md', utf8('甲'))
  eq('其他扩展名拒绝', unsupported.status, 'UNSUPPORTED_EXTENSION')

  const arrayBufferInput = parse('buffer.txt', utf8('甲').slice().buffer as ArrayBuffer)
  eq('ArrayBuffer 输入可解析', arrayBufferInput.items[0]?.title, '甲')

  const offsetBytes = join(Uint8Array.from([0x62]), utf8('甲'))
  const offsetView = parse('offset.txt', offsetBytes.subarray(1))
  eq('Uint8Array byteOffset 输入可解析', offsetView.items[0]?.title, '甲')

  const tooLarge = parse('big.txt', new Uint8Array(MAX_TITLE_FILE_BYTES + 1))
  eq('超过 1MiB 在读取前拒绝', tooLarge.status, 'FILE_TOO_LARGE')

  const atLimit = parse('limit.txt', new Uint8Array(MAX_TITLE_FILE_BYTES).fill(0x61))
  check('恰好 1MiB 不被文件大小拒绝', !hasCode(atLimit, 'FILE_TOO_LARGE'), codes(atLimit))
}

function runTxtTests(): void {
  const mixed = parse(
    'mixed.txt',
    utf8('  甲  \r\n\r\n乙\r丙\n甲\n'),
  )
  eq('TXT 混合换行/trim/重复', mixed.items.map((item) => item.title), ['甲', '乙', '丙', '甲'])
  eq('TXT 物理行号保留', mixed.items.map((item) => item.lineNo), [1, 3, 4, 5])
  eq('TXT 记录数', mixed.recordCount, 4)
  eq('TXT 空行计数', mixed.blankRecordCount, 2)
  eq('TXT 重复额外计数', mixed.duplicateExtraCount, 1)
  eq('TXT 重复组数', mixed.duplicateGroupCount, 1)
  eq('TXT itemId 稳定', mixed.items.map((item) => item.itemId), ['r000001', 'r000003', 'r000004', 'r000005'])

  const internal = parse('internal.txt', utf8('甲  乙\n丙'))
  eq('TXT 保留内部空格', internal.items[0]?.title, '甲  乙')

  const tab = parse('tab.txt', utf8('甲\t标签\n乙'))
  eq('TXT 不剥离 Tab 后标签', tab.items[0]?.title, '甲\t标签')

  const exactly200 = parse('200.txt', utf8('😀'.repeat(200)))
  eq('TXT 200 个 emoji 计为 200', exactly200.items[0]?.charCount, 200)
  eq('TXT 200 个 emoji 可导入', exactly200.importable, true)

  const over200 = parse('201.txt', utf8('😀'.repeat(201)))
  eq('TXT 201 个 emoji 标超长', over200.items[0]?.tooLong, true)
  check('TXT 201 个 emoji 产生可定位错误', hasCode(over200, 'TITLE_TOO_LONG'), codes(over200))
  eq('TXT 201 个 emoji importable=false', over200.importable, false)

  const empty = parse('empty.txt', new Uint8Array())
  eq('TXT 空文件无有效标题', empty.status, 'NO_VALID_TITLES')
  eq('TXT 空文件 importable=false', empty.importable, false)

  const blankOnly = parse('blank.txt', utf8('\n  \n\t\n'))
  eq('TXT 全空行无有效标题', blankOnly.status, 'NO_VALID_TITLES')
  eq('TXT 全空行统计空白行', blankOnly.blankRecordCount, 4)

  const hundred = parse('100.txt', utf8(
    Array.from({ length: 100 }, (_, index) => `标题${index}`).join('\n'),
  ))
  eq('TXT 100 条可导入', hundred.importable, true)

  const hundredOne = parse('101.txt', utf8(
    Array.from({ length: 101 }, (_, index) => `标题${index}`).join('\n'),
  ))
  check('TXT 101 条拒绝', hasCode(hundredOne, 'TOO_MANY_TITLES'), codes(hundredOne))
  eq('TXT 101 条不静默截断', hundredOne.items.length, 101)
  eq('TXT 101 条 importable=false', hundredOne.importable, false)

  const exactlyTotal = parse('total-ok.txt', utf8(
    Array.from({ length: 100 }, () => 'x'.repeat(200)).join('\n'),
  ))
  eq('TXT 100×200=20,000 总字符', exactlyTotal.totalChars, 20_000)
  eq('TXT 20,000 总字符可导入', exactlyTotal.importable, true)

  const total = parse('total.txt', utf8(
    Array.from({ length: 101 }, () => 'x'.repeat(200)).join('\n'),
  ))
  check('TXT 总字符超限被记录', hasCode(total, 'TOTAL_CHARS_EXCEEDED'), codes(total))
  eq('TXT 总字符超限 importable=false', total.importable, false)

  const tooManyRecords = parse('many.txt', utf8(
    Array.from({ length: 1001 }, (_, index) => `标题${index}`).join('\n'),
  ))
  eq('TXT 1001 条记录被解析上限拒绝', tooManyRecords.status, 'TOO_MANY_RECORDS')
  eq('TXT 1001 条无部分导入', tooManyRecords.items.length, 0)
}

function runCsvTests(): void {
  const basic = parse('basic.csv', csv('标题,标签\n甲,新闻\n乙,体育'))
  eq('CSV 有表头 header.used', basic.header.used, true)
  eq('CSV 有表头 header.detected', basic.header.detected, true)
  eq('CSV 有表头无需确认', basic.header.confirmationRequired, false)
  eq('CSV 标题列自动选择', basic.columnSelection.selectedIndex, 0)
  eq('CSV 标题列来源 candidate', basic.columnSelection.source, 'candidate')
  eq('CSV 基本标题', basic.items.map((item) => item.title), ['甲', '乙'])
  eq('CSV 基本行号', basic.items.map((item) => item.lineNo), [2, 3])
  eq('CSV 数据记录号', basic.items.map((item) => item.recordNo), [1, 2])
  eq('CSV 基本 importable', basic.importable, true)
  eq('CSV 数据记录数', basic.recordCount, 2)
  eq('CSV 预览行数', basic.previewRows.length, 2)

  const noHeader = parse('no-header.csv', csv('甲,新闻\n乙,体育'))
  eq('CSV 无表头默认 header.used=false', noHeader.header.used, false)
  eq('CSV 无表头要求确认', noHeader.header.confirmationRequired, true)
  eq('CSV 无候选列默认第一列', noHeader.columnSelection.selectedIndex, 0)
  eq('CSV 无候选列来源 default', noHeader.columnSelection.source, 'default')
  eq('CSV 无候选列要求确认', noHeader.columnSelection.confirmationRequired, true)
  eq('CSV 无表头 importable=false', noHeader.importable, false)

  const noHeaderExplicit = parse('no-header.csv', csv('甲,新闻\n乙,体育'), {
    header: false,
    columnIndex: 0,
  })
  eq('CSV 显式无表头可导入', noHeaderExplicit.importable, true)
  eq('CSV 显式无表头首条', noHeaderExplicit.items[0]?.title, '甲')

  const oneColumn = parse('one.csv', csv('甲\n乙'))
  eq('CSV 单列自动选择', oneColumn.columnSelection.source, 'single')
  eq('CSV 单列要求表头确认', oneColumn.header.confirmationRequired, true)
  eq('CSV 单列显式无表头可导入', parse('one.csv', csv('甲\n乙'), { header: false }).importable, true)

  const multiCandidate = parse('multi-candidate.csv', csv('标题,title,标签\n甲,甲2,新闻'))
  eq('CSV 多候选列 header.used', multiCandidate.header.used, true)
  eq('CSV 多候选列 selectedIndex=null', multiCandidate.columnSelection.selectedIndex, null)
  eq('CSV 多候选列要求确认', multiCandidate.columnSelection.confirmationRequired, true)
  eq('CSV 多候选列不猜标题', multiCandidate.items.length, 0)
  eq('CSV 多候选列状态', multiCandidate.status, 'COLUMN_SELECTION_REQUIRED')
  eq('CSV 多候选列 columns 数量', multiCandidate.columns.length, 3)

  const multiChosen = parse('multi-candidate.csv', csv('标题,title,标签\n甲,甲2,新闻'), {
    columnIndex: 1,
  })
  eq('CSV 显式选择第二候选列', multiChosen.items[0]?.title, '甲2')
  eq('CSV 显式选择候选列可导入', multiChosen.importable, true)

  const noCandidate = parse('no-candidate.csv', csv('甲,乙\n丙,丁'))
  eq('CSV 无候选列不静默选列', noCandidate.columnSelection.source, 'default')
  eq('CSV 无候选列要求确认', noCandidate.columnSelection.confirmationRequired, true)
  eq('CSV 无候选列初始标题', noCandidate.items[0]?.title, '甲')
  eq('CSV 无候选列 importable=false', noCandidate.importable, false)
  const noCandidateChosen = parse('no-candidate.csv', csv('甲,乙\n丙,丁'), {
    header: false,
    columnIndex: 1,
  })
  eq('CSV 无候选列显式选择第二列', noCandidateChosen.items[0]?.title, '乙')
  eq('CSV 无候选列显式选择可导入', noCandidateChosen.importable, true)

  const quoted = parse('quoted.csv', csv('标题,标签\n"甲,乙",新闻\n"A""B",体育'))
  eq('CSV 引号内逗号', quoted.items[0]?.title, '甲,乙')
  eq('CSV 双引号转义', quoted.items[1]?.title, 'A"B')
  eq('CSV 引号记录可导入', quoted.importable, true)

  const embeddedNewline = parse(
    'newline.csv',
    csv('标题,标签\n"甲\n乙",新闻\n丙,体育'),
  )
  eq('CSV 引号内换行保留为一条标题', embeddedNewline.items[0]?.title, '甲\n乙')
  eq('CSV 引号内换行记录号', embeddedNewline.items[0]?.recordNo, 1)
  eq('CSV 引号内换行起始行号', embeddedNewline.items[0]?.lineNo, 2)
  eq('CSV 后续记录起始行号', embeddedNewline.items[1]?.lineNo, 4)
  eq('CSV 后续记录号', embeddedNewline.items[1]?.recordNo, 2)

  const embeddedCrlf = parse(
    'newline-crlf.csv',
    csv('标题,标签\r\n"甲\r\n乙",新闻\r\n丙,体育'),
  )
  eq('CSV 引号内 CRLF 保留', embeddedCrlf.items[0]?.title, '甲\n乙')
  eq('CSV 引号内 CRLF 后续行号', embeddedCrlf.items[1]?.lineNo, 4)

  const mismatch = parse('mismatch.csv', csv('标题,标签\n甲,新闻,extra\n乙,体育'))
  check('CSV 字段数不一致错误', hasCode(mismatch, 'CSV_FIELD_COUNT_MISMATCH'), codes(mismatch))
  eq('CSV 字段数不一致 importable=false', mismatch.importable, false)

  const unclosed = parse('unclosed.csv', csv('标题,标签\n"甲,新闻'))
  check('CSV 引号未闭合错误', hasCode(unclosed, 'CSV_UNCLOSED_QUOTE'), codes(unclosed))
  eq('CSV 引号未闭合 importable=false', unclosed.importable, false)

  const invalidQuote = parse('bad-quote.csv', csv('标题,标签\n甲"乙,新闻'))
  check('CSV 非法引号错误', hasCode(invalidQuote, 'CSV_INVALID_QUOTE'), codes(invalidQuote))
  eq('CSV 非法引号 importable=false', invalidQuote.importable, false)

  const invalidAfterQuote = parse('bad-after-quote.csv', csv('标题,标签\n"甲"x,新闻'))
  check('CSV 结束引号后非法字符错误', hasCode(invalidAfterQuote, 'CSV_INVALID_QUOTE'), codes(invalidAfterQuote))

  const blankAndEmpty = parse('blank-empty.csv', csv('标题,标签\n\n甲,新闻\n,体育\n乙,'))
  eq('CSV 空白记录计数', blankAndEmpty.blankRecordCount, 1)
  eq('CSV 空标题单元格计数', blankAndEmpty.emptyTitleCount, 1)
  eq('CSV 空标题跳过后的标题', blankAndEmpty.items.map((item) => item.title), ['甲', '乙'])
  eq('CSV 空白/空标题可导入', blankAndEmpty.importable, true)

  const duplicate = parse('duplicate.csv', csv('标题,标签\n甲,1\n甲,2'))
  eq('CSV 重复标题保留', duplicate.items.length, 2)
  eq('CSV 重复额外计数', duplicate.duplicateExtraCount, 1)
  eq('CSV 重复组数', duplicate.duplicateGroupCount, 1)
  eq('CSV 重复标题可导入', duplicate.importable, true)

  const long200 = parse('long.csv', csv(`标题,标签\n${'😀'.repeat(200)},x`))
  eq('CSV 200 个 emoji 计数', long200.items[0]?.charCount, 200)
  eq('CSV 200 个 emoji 可导入', long200.importable, true)
  const long201 = parse('long.csv', csv(`标题,标签\n${'😀'.repeat(201)},x`))
  check('CSV 201 个 emoji 错误', hasCode(long201, 'TITLE_TOO_LONG'), codes(long201))
  eq('CSV 201 个 emoji importable=false', long201.importable, false)

  const hundred = parse('100.csv', csv(
    `标题,标签\n${Array.from({ length: 100 }, (_, index) => `标题${index},x`).join('\n')}`,
  ))
  eq('CSV 100 条可导入', hundred.importable, true)
  const hundredOne = parse('101.csv', csv(
    `标题,标签\n${Array.from({ length: 101 }, (_, index) => `标题${index},x`).join('\n')}`,
  ))
  check('CSV 101 条拒绝', hasCode(hundredOne, 'TOO_MANY_TITLES'), codes(hundredOne))
  eq('CSV 101 条不静默截断', hundredOne.items.length, 101)

  const exactlyTotal = parse('total-ok.csv', csv(
    `标题,标签\n${Array.from({ length: 100 }, () => `${'x'.repeat(200)},y`).join('\n')}`,
  ))
  eq('CSV 100×200=20,000 总字符', exactlyTotal.totalChars, 20_000)
  eq('CSV 20,000 总字符可导入', exactlyTotal.importable, true)

  const total = parse('total.csv', csv(
    `标题,标签\n${Array.from({ length: 101 }, () => `${'x'.repeat(200)},y`).join('\n')}`,
  ))
  check('CSV 总字符超限被记录', hasCode(total, 'TOTAL_CHARS_EXCEEDED'), codes(total))
  eq('CSV 总字符超限 importable=false', total.importable, false)

  const semicolon = parse('semicolon.csv', csv('标题;标签\n甲;新闻'))
  eq('CSV 非逗号不拆分', semicolon.items.map((item) => item.title), ['标题;标签', '甲;新闻'])
  check('CSV 非逗号状态可理解', semicolon.status === 'HEADER_CONFIRMATION_REQUIRED', semicolon.status)
  eq('CSV 非逗号显式无表头可导入', parse('semicolon.csv', csv('标题;标签\n甲;新闻'), { header: false }).importable, true)

  const bomCsv = parse('bom.csv', join(Uint8Array.from([0xef, 0xbb, 0xbf]), csv('标题,标签\n甲,新闻')))
  eq('CSV UTF-8 BOM 编码', bomCsv.encoding, 'utf-8')
  eq('CSV UTF-8 BOM 表头识别', bomCsv.header.detected, true)
  eq('CSV UTF-8 BOM 可导入', bomCsv.importable, true)

  const gbCsv = parse('gb.csv', join(GB_TITLE, Uint8Array.from([0x0a]), GB_CHINESE))
  eq('CSV GB18030 编码', gbCsv.encoding, 'gb18030')
  eq('CSV GB18030 单列表头', gbCsv.header.detected, true)
  eq('CSV GB18030 标题', gbCsv.items[0]?.title, '中文')

  const tooManyColumns = parse('columns.csv', csv(
    Array.from({ length: MAX_CSV_COLUMNS + 1 }, (_, index) => `c${index}`).join(','),
  ), { header: false })
  check('CSV 超过列数硬保护', hasCode(tooManyColumns, 'TOO_MANY_COLUMNS'), codes(tooManyColumns))

  const fieldTooLarge = parse('field.csv', csv(`h\n${'a'.repeat(MAX_CSV_FIELD_UNITS + 1)}`))
  check('CSV 单字段超过硬保护', hasCode(fieldTooLarge, 'FIELD_TOO_LARGE'), codes(fieldTooLarge))

  const manyCsv = parse('many.csv', csv(
    Array.from({ length: 1001 }, (_, index) => `标题${index}`).join('\n'),
  ))
  check('CSV 1001 条数据记录被拒绝', hasCode(manyCsv, 'TOO_MANY_RECORDS'), codes(manyCsv))

  const headerOnly = parse('header-only.csv', csv('标题,标签\n'))
  eq('CSV 只有表头无有效标题', headerOnly.status, 'NO_VALID_TITLES')

  const preview = parse('preview.csv', csv('标题,标签\n甲,新闻\n乙,体育'))
  eq('CSV preview recordNo', preview.previewRows.map((row) => row.recordNo), [1, 2])
  eq('CSV preview lineNo', preview.previewRows.map((row) => row.lineNo), [2, 3])
  eq('CSV preview cells', preview.previewRows[0]?.cells, ['甲', '新闻'])

  const previewMany = parse('preview-many.csv', csv(
    `标题,标签\n${Array.from({ length: 25 }, (_, index) => `标题${index},x`).join('\n')}`,
  ))
  eq('CSV 预览行数硬上限 20', previewMany.previewRows.length, 20)
  eq('CSV 完整 items 仍为 25', previewMany.items.length, 25)

  const upperCsv = parse('A.CSV', csv('标题,标签\n甲,新闻'))
  eq('CSV 扩展名大小写不敏感', upperCsv.format, 'csv')
  eq('CSV 大写扩展名可导入', upperCsv.importable, true)

  const decoded = decodeTitleFileBytes(join(GB_TITLE, Uint8Array.from([0x0a]), GB_CHINESE))
  eq('decodeTitleFileBytes 返回 GB18030', decoded.encoding, 'gb18030')
  eq('decodeTitleFileBytes 文本', decoded.text, '标题\n中文')
}

function runAll(): { checks: Check[]; browser: Record<string, unknown> } {
  try {
    runEncodingTests()
    runTxtTests()
    runCsvTests()
  } catch (error) {
    checks.push({
      name: 'harness 执行未抛异常',
      pass: false,
      detail: preview(error instanceof Error ? error.stack || error.message : error),
    })
  }
  return {
    checks,
    browser: {
      userAgent: navigator.userAgent,
      gb18030: (() => {
        try {
          return new TextDecoder('gb18030').decode(GB_CHINESE)
        } catch {
          return 'unsupported'
        }
      })(),
    },
  }
}

declare global {
  interface Window {
    __f18: {
      run: typeof runAll
    }
  }
}

window.__f18 = { run: runAll }
document.querySelector('#app')!.textContent = 'F18 ready'
