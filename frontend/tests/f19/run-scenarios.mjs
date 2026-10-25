/**
 * F19 阶段 3B：真实 ClassifyPanel + 内存 File + F17 mock API + Edge headless。
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdir, rm, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import os from 'node:os'
import { build } from 'vite'
import { createMockServer } from '../f17/mock-server.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const frontendRoot = resolve(here, '../..')
const distDir = join(here, 'dist')
const envDir = join(here, 'env')
const SERVER_PORT = 18194
const CDP_PORT = 9344
const HARNESS_URL = `http://127.0.0.1:${SERVER_PORT}/tests/f19/harness.html`
const MAX_FILE_BYTES = 1024 * 1024

const EDGE_CANDIDATES = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
]

const sleep = (ms) => new Promise((resolvePromise) => setTimeout(resolvePromise, ms))

class CdpClient {
  static async connect(wsUrl) {
    const socket = new WebSocket(wsUrl)
    await new Promise((resolvePromise, reject) => {
      socket.addEventListener('open', resolvePromise, { once: true })
      socket.addEventListener('error', () => reject(new Error('WebSocket 连接失败')), { once: true })
    })
    return new CdpClient(socket)
  }

  constructor(socket) {
    this.socket = socket
    this.nextId = 0
    this.pending = new Map()
    socket.addEventListener('message', (event) => {
      let message
      try {
        message = JSON.parse(String(event.data))
      } catch {
        return
      }
      if (message.id === undefined) return
      const pending = this.pending.get(message.id)
      if (!pending) return
      this.pending.delete(message.id)
      if (message.error) pending.reject(new Error(`${message.error.code}: ${message.error.message}`))
      else pending.resolve(message.result)
    })
  }

  send(method, params = {}) {
    const id = ++this.nextId
    return new Promise((resolvePromise, reject) => {
      this.pending.set(id, { resolve: resolvePromise, reject })
      this.socket.send(JSON.stringify({ id, method, params }))
    })
  }

  close() {
    try {
      this.socket.close()
    } catch {
      // ignore
    }
  }
}

async function evaluate(client, expression) {
  const response = await client.send('Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise: true,
  })
  if (response.exceptionDetails) {
    throw new Error(
      response.exceptionDetails.exception?.description
      || response.exceptionDetails.text
      || 'evaluate failed',
    )
  }
  return response.result?.value
}

async function waitFor(fn, { timeout = 12_000, interval = 80, label = 'condition' } = {}) {
  const started = Date.now()
  let lastError = null
  while (Date.now() - started < timeout) {
    try {
      const value = await fn()
      if (value) return value
    } catch (error) {
      lastError = error
    }
    await sleep(interval)
  }
  throw new Error(`waitFor timeout (${label})${lastError ? `: ${lastError.message}` : ''}`)
}

function buildChecks() {
  const checks = []
  return {
    checks,
    check(name, pass, detail = '') {
      checks.push({ name, pass: Boolean(pass), detail: String(detail) })
    },
    eq(name, actual, expected) {
      const left = JSON.stringify(actual)
      const right = JSON.stringify(expected)
      checks.push({
        name,
        pass: left === right,
        detail: `actual=${left} expected=${right}`,
      })
    },
  }
}

async function setScenario(name) {
  const response = await fetch(`http://127.0.0.1:${SERVER_PORT}/__scenario`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name }),
  })
  if (!response.ok) throw new Error(`scenario ${name} failed: ${response.status}`)
}

async function getLog() {
  return (await fetch(`http://127.0.0.1:${SERVER_PORT}/__log`)).json()
}

async function navigate(client) {
  await client.send('Page.navigate', { url: `${HARNESS_URL}?t=${Date.now()}` })
  await waitFor(
    async () => evaluate(client, 'Boolean(window.__f19 && window.__f19.ready)'),
    { timeout: 15_000, label: 'F19 harness ready' },
  )
}

const snap = (client) => evaluate(client, 'window.__f19.snapshot()')

async function waitSnap(client, predicate, label, timeout = 12_000) {
  return waitFor(async () => {
    const state = await snap(client)
    return predicate(state) ? state : null
  }, { timeout, label })
}

async function action(client, name, ...args) {
  const expression = `window.__f19.${name}(${args.map((arg) => JSON.stringify(arg)).join(',')})`
  const result = await evaluate(client, expression)
  await sleep(35)
  return result
}

async function selectText(client, name, text) {
  const ok = await action(client, 'selectText', name, text)
  if (!ok) throw new Error(`selectText(${name}) 未找到 file input`)
}

async function selectBytes(client, name, bytes) {
  const ok = await action(client, 'selectBytes', name, bytes)
  if (!ok) throw new Error(`selectBytes(${name}) 未找到 file input`)
}

async function selectLargeFile(client, name, size) {
  const ok = await action(client, 'selectLargeFile', name, size)
  if (!ok) throw new Error(`selectLargeFile(${name}) 未找到 file input`)
}

async function setViewport(client, width, height) {
  await client.send('Emulation.setDeviceMetricsOverride', {
    width,
    height,
    deviceScaleFactor: 1,
    mobile: false,
  })
}

async function clearViewport(client) {
  await client.send('Emulation.clearDeviceMetricsOverride')
}

function rowTitles(rows) {
  return rows.map((row) => row.title)
}

function batchBodies(log) {
  return log.requests.filter((item) => item.kind === 'batch')
}

async function main() {
  await mkdir(envDir, { recursive: true })
  await writeFile(
    join(envDir, '.env.local'),
    `VITE_API_BASE_URL=http://127.0.0.1:${SERVER_PORT}\n`,
  )
  process.env.VITE_API_BASE_URL = `http://127.0.0.1:${SERVER_PORT}`

  console.log(`[build] 构建 F19 harness（${distDir}）`)
  await build({ configFile: join(here, 'vite.harness.config.mjs'), logLevel: 'warn' })
  console.log('[build] OK')

  const mock = await createMockServer({ port: SERVER_PORT, staticDir: distDir })
  const profileDir = join(os.tmpdir(), `tmf-f19-edge-${process.pid}-${Date.now()}`)
  await mkdir(profileDir, { recursive: true })

  const scenarios = [
    {
      id: 'entry-local',
      name: 'S1 文件入口/TXT 本地预览/取消/重复选择',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        let state = await snap(client)
        ctx.check('S1 多条模式显示文件入口', state.fileInputPresent && state.fileButtonPresent, JSON.stringify(state))
        ctx.check('S1 accept 包含 txt/csv', state.fileInputAccept.includes('.txt') && state.fileInputAccept.includes('.csv'), state.fileInputAccept)
        ctx.check('S1 无预览/无批量请求', !state.previewVisible && (await getLog()).batchCalls === 0, JSON.stringify(state))

        await selectText(client, 'sample.txt', '  甲  \r\n\r\n乙\r甲\n')
        state = await waitSnap(client, (s) => s.previewVisible, 'TXT preview')
        ctx.check('S1 TXT 预览显示编码', state.fileMeta.includes('UTF-8'), state.fileMeta)
        ctx.eq('S1 TXT 预览标题', rowTitles(state.fileItems), ['甲', '乙', '甲'])
        ctx.eq('S1 TXT 原始物理行号', state.fileItems.map((item) => item.lineNo), [1, 3, 4])
        ctx.check('S1 TXT 空行统计', state.fileStats.includes('空行') && state.fileStats.includes('2'), state.fileStats)
        ctx.check('S1 TXT 重复统计', state.bodyText.includes('重复 1 条 / 1 组'), state.bodyText.slice(0, 500))
        ctx.check('S1 TXT 可确认导入', state.confirmDisabled === false, state.confirmText)
        ctx.check('S1 预览阶段无批量请求', (await getLog()).batchCalls === 0, String((await getLog()).batchCalls))

        await action(client, 'clickFileCancel')
        state = await snap(client)
        ctx.check('S1 取消预览不改变现有状态', !state.previewVisible && !state.importedVisible, JSON.stringify(state))

        await selectText(client, 'sample.txt', '  甲  \r\n\r\n乙\r甲\n')
        await waitSnap(client, (s) => s.previewVisible, 'TXT preview repeat')
        state = await snap(client)
        ctx.eq('S1 同一文件重复选择再次读取', state.arrayBufferCalls, 2)
        ctx.check('S1 重复选择后仍无批量请求', (await getLog()).batchCalls === 0, String((await getLog()).batchCalls))
        await action(client, 'clickFileCancel')
      },
    },
    {
      id: 'limits-no-read',
      name: 'S2 超限/扩展名在读取前拒绝，TXT 标题与批量上限',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await action(client, 'resetArrayBufferCalls')

        await selectBytes(client, 'bad.md', Array.from(new TextEncoder().encode('x')))
        let state = await waitSnap(client, (s) => Boolean(s.fileError), 'unsupported extension')
        ctx.check('S2 不支持扩展名报错', state.fileError.includes('.txt') && state.fileError.includes('.csv'), state.fileError)
        ctx.check('S2 不支持扩展名未读取字节', state.arrayBufferCalls === 0, String(state.arrayBufferCalls))
        ctx.check('S2 不支持扩展名无预览', !state.previewVisible, JSON.stringify(state))

        await action(client, 'resetArrayBufferCalls')
        await selectLargeFile(client, 'big.txt', MAX_FILE_BYTES + 1)
        state = await waitSnap(client, (s) => s.fileError.includes('1 MiB'), 'large file')
        ctx.check('S2 超过 1 MiB 明确报错', state.fileError.includes('未读取'), state.fileError)
        ctx.check('S2 超过 1 MiB 未调用 arrayBuffer', state.arrayBufferCalls === 0, String(state.arrayBufferCalls))

        await selectText(client, 'long.txt', '😀'.repeat(201))
        state = await waitSnap(client, (s) => s.previewVisible, 'long preview')
        ctx.check('S2 201 code point 标超长并禁用导入', state.confirmDisabled && state.fileItems[0]?.length === '201/200', JSON.stringify(state))

        await selectText(
          client,
          'many.txt',
          Array.from({ length: 101 }, (_, index) => `标题${index}`).join('\n'),
        )
        state = await waitSnap(client, (s) => s.previewVisible && s.fileItems.length > 0, '101 preview')
        ctx.check('S2 101 条拒绝导入', state.confirmDisabled, state.confirmText)

        await selectText(
          client,
          'max.txt',
          Array.from({ length: 100 }, () => 'x'.repeat(200)).join('\n'),
        )
        state = await waitSnap(client, (s) => s.previewVisible && s.fileItems.length === 100, '100 max preview')
        ctx.check('S2 100×200=20,000 可确认', state.confirmDisabled === false, state.confirmText)

        await selectText(client, 'empty.txt', '')
        state = await waitSnap(client, (s) => s.previewVisible, 'empty preview')
        ctx.check('S2 空文件无有效标题并禁用导入', state.confirmDisabled && state.bodyText.includes('未解析到有效标题'), JSON.stringify(state))
        ctx.check('S2 所有限制操作均无批量请求', (await getLog()).batchCalls === 0, String((await getLog()).batchCalls))
      },
    },
    {
      id: 'encoding',
      name: 'S3 TXT/CSV 编码显示（UTF-8 BOM / GB18030）',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')

        const bom = [0xef, 0xbb, 0xbf, ...Array.from(new TextEncoder().encode('甲\n乙'))]
        await selectBytes(client, 'bom.txt', bom)
        let state = await waitSnap(client, (s) => s.previewVisible, 'BOM preview')
        ctx.check('S3 UTF-8 BOM 显示', state.fileMeta.includes('UTF-8 BOM'), state.fileMeta)
        ctx.eq('S3 BOM 不残留在首条标题', rowTitles(state.fileItems), ['甲', '乙'])

        const gb = [0xb1, 0xea, 0xcc, 0xe2, 0x0a, 0xd6, 0xd0, 0xce, 0xc4]
        await selectBytes(client, 'gb.txt', gb)
        state = await waitSnap(client, (s) => s.previewVisible && s.fileMeta.includes('GB18030'), 'GB preview')
        ctx.check('S3 GB18030 显示', state.fileMeta.includes('GB18030'), state.fileMeta)
        ctx.eq('S3 GB18030 标题', rowTitles(state.fileItems), ['标题', '中文'])
        ctx.check('S3 编码预览阶段无批量请求', (await getLog()).batchCalls === 0, String((await getLog()).batchCalls))
      },
    },
    {
      id: 'csv-header-confirm',
      name: 'S4 CSV 表头待确认、确认后首行按数据处理',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await selectText(client, 'no-header.csv', '甲\n乙')
        let state = await waitSnap(client, (s) => s.previewVisible, 'no header preview')
        ctx.check('S4 无候选表头显示待确认', state.headerState.includes('未确认'), state.headerState)
        ctx.check('S4 表头未确认时不可导入', state.confirmDisabled, state.confirmText)

        await action(client, 'clickHeader', 'data')
        state = await waitSnap(client, (s) => s.headerChoice === 'data', 'data header chosen')
        ctx.check('S4 选择首行是数据后可导入', state.confirmDisabled === false, state.confirmText)
        ctx.eq('S4 首行作为第一条标题', rowTitles(state.fileItems), ['甲', '乙'])
        ctx.check('S4 预览阶段仍无批量请求', (await getLog()).batchCalls === 0, String((await getLog()).batchCalls))

        await action(client, 'clickFileConfirm')
        state = await waitSnap(client, (s) => s.importedVisible, 'imported no header')
        ctx.eq('S4 导入后只读标题', rowTitles(state.importedItems), ['甲', '乙'])
        ctx.check('S4 导入后仍未发批量请求', (await getLog()).batchCalls === 0, String((await getLog()).batchCalls))

        await action(client, 'clickSubmit')
        state = await waitSnap(client, (s) => s.batchPhase === 'done', 'batch done header')
        const log = await getLog()
        const calls = batchBodies(log)
        ctx.eq('S4 分类请求只有 1 个分片', calls.length, 1)
        ctx.eq('S4 请求标题', calls[0]?.body?.items?.map((item) => item.title), ['甲', '乙'])
        ctx.check('S4 请求体没有文件字段', calls[0]?.body && !('file' in calls[0].body) && !('fileName' in calls[0].body), JSON.stringify(calls[0]?.body))
        ctx.eq('S4 结果来源记录号', state.batchRows.map((row) => row.recordNo), [1, 2])
        ctx.eq('S4 结果来源物理行号', state.batchRows.map((row) => row.lineNo), [1, 2])
        ctx.check('S4 结果完成', state.batchPhase === 'done' && state.batchRows.length === 2, JSON.stringify(state.batchRows))
      },
    },
    {
      id: 'csv-auto-header',
      name: 'S5 CSV 自动识别表头',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await selectText(client, 'header.csv', '标题,标签\n甲,新闻\n乙,体育')
        const state = await waitSnap(client, (s) => s.previewVisible, 'auto header preview')
        ctx.check('S5 自动识别首行表头', state.headerState.includes('自动'), state.headerState)
        ctx.check('S5 自动识别后可直接导入', state.confirmDisabled === false, state.confirmText)
        ctx.eq('S5 数据行标题', rowTitles(state.fileItems), ['甲', '乙'])
        await action(client, 'clickFileConfirm')
        const imported = await waitSnap(client, (s) => s.importedVisible, 'auto header imported')
        ctx.eq('S5 导入标题', rowTitles(imported.importedItems), ['甲', '乙'])
      },
    },
    {
      id: 'csv-multi-column',
      name: 'S6 CSV 多候选列要求用户选择',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await selectText(client, 'multi.csv', '标题,title,标签\n甲,甲2,新闻')
        let state = await waitSnap(client, (s) => s.previewVisible, 'multi column preview')
        ctx.check('S6 多候选列未选择时不可导入', state.confirmDisabled, state.confirmText)
        ctx.check('S6 列选择为占位', state.columnValue === '', JSON.stringify(state.columnOptions))
        ctx.check('S6 返回候选列', state.columnOptions.length >= 3, JSON.stringify(state.columnOptions))

        await action(client, 'chooseColumn', 1)
        state = await waitSnap(client, (s) => s.columnValue === '1', 'column 1 chosen')
        ctx.check('S6 选择第二候选列后可导入', state.confirmDisabled === false, state.confirmText)
        ctx.eq('S6 选定列标题', rowTitles(state.fileItems), ['甲2'])
        await action(client, 'clickFileConfirm')
        const imported = await waitSnap(client, (s) => s.importedVisible, 'multi imported')
        ctx.eq('S6 导入选定列', rowTitles(imported.importedItems), ['甲2'])
      },
    },
    {
      id: 'csv-no-candidate',
      name: 'S7 CSV 无候选列默认第一列但必须确认',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await selectText(client, 'no-candidate.csv', '甲,乙\n丙,丁')
        let state = await waitSnap(client, (s) => s.previewVisible, 'no candidate preview')
        ctx.check('S7 表头待确认时不可导入', state.confirmDisabled, state.confirmText)
        await action(client, 'clickHeader', 'data')
        state = await waitSnap(client, (s) => s.headerChoice === 'data', 'no candidate header data')
        ctx.check('S7 无候选列默认第一列', state.columnValue === '0', String(state.columnValue))
        ctx.check('S7 无候选列需要确认', state.columnConfirmVisible && !state.columnConfirmChecked && state.confirmDisabled, JSON.stringify(state))
        ctx.eq('S7 默认列预览', rowTitles(state.fileItems), ['甲', '丙'])

        await action(client, 'toggleColumnConfirm')
        state = await waitSnap(client, (s) => s.columnConfirmChecked, 'column confirmed')
        ctx.check('S7 确认第一列后可导入', state.confirmDisabled === false, state.confirmText)
        await action(client, 'clickFileConfirm')
        const imported = await waitSnap(client, (s) => s.importedVisible, 'no candidate imported')
        ctx.eq('S7 导入默认列', rowTitles(imported.importedItems), ['甲', '丙'])
      },
    },
    {
      id: 'csv-skip',
      name: 'S8 CSV 可恢复坏行显式跳过后导入',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await selectText(client, 'bad.csv', '标题,标签\n甲,新闻,extra\n乙,体育\n丙')
        let state = await waitSnap(client, (s) => s.previewVisible, 'bad rows preview')
        ctx.check('S8 坏行默认禁止导入', state.confirmDisabled, state.confirmText)
        ctx.check('S8 可恢复错误提供跳过选项', state.skipVisible && !state.skipUnavailable, JSON.stringify(state))
        await action(client, 'toggleSkip')
        state = await waitSnap(client, (s) => s.skipChecked, 'skip checked')
        ctx.check('S8 勾选跳过后可导入', state.confirmDisabled === false, state.confirmText)
        ctx.check('S8 跳过数量显示为 2', state.skipText.includes('2'), state.skipText)
        ctx.eq('S8 跳过坏行后标题', rowTitles(state.fileItems), ['乙'])

        await action(client, 'clickFileConfirm')
        state = await waitSnap(client, (s) => s.importedVisible, 'skip imported')
        ctx.eq('S8 导入跳过后的标题', rowTitles(state.importedItems), ['乙'])
        await action(client, 'clickSubmit')
        await waitSnap(client, (s) => s.batchPhase === 'done', 'skip batch done')
        const log = await getLog()
        const calls = batchBodies(log)
        ctx.eq('S8 请求只含跳过后的标题', calls[0]?.body?.items?.map((item) => item.title), ['乙'])
        ctx.eq('S8 请求 item_id 保留来源行号', calls[0]?.body?.items?.[0]?.item_id, 'r000003')
      },
    },
    {
      id: 'csv-limits-empty',
      name: 'S8b CSV 空标题/100/101/200/201/总字符边界',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')

        await selectText(client, 'empty-title.csv', '标题,标签\n甲,新闻\n,体育\n乙,体育')
        let state = await waitSnap(client, (s) => s.previewVisible, 'empty title preview')
        ctx.check('S8b 空标题行跳过并统计', state.fileStats.includes('空标题 1') && rowTitles(state.fileItems).join('|') === '甲|乙', JSON.stringify(state.fileStats))
        ctx.check('S8b 空标题行不阻止导入', state.confirmDisabled === false, state.confirmText)

        await selectText(client, '100.csv', `标题,标签\n${Array.from({ length: 100 }, (_, index) => `标题${index},x`).join('\n')}`)
        state = await waitSnap(client, (s) => s.previewVisible && s.fileItems.length === 100, '100 csv preview')
        ctx.check('S8b CSV 100 条可确认', state.confirmDisabled === false, state.confirmText)

        await selectText(client, '101.csv', `标题,标签\n${Array.from({ length: 101 }, (_, index) => `标题${index},x`).join('\n')}`)
        state = await waitSnap(client, (s) => s.previewVisible && s.fileItems.length > 0, '101 csv preview')
        ctx.check('S8b CSV 101 条拒绝', state.confirmDisabled, state.confirmText)

        await selectText(client, '201.csv', `标题,标签\n${'😀'.repeat(201)},x`)
        state = await waitSnap(client, (s) => s.previewVisible, '201 csv preview')
        ctx.check('S8b CSV 201 字符拒绝', state.confirmDisabled && state.fileItems[0]?.length === '201/200', JSON.stringify(state))

        await selectText(client, '200.csv', `标题,标签\n${'😀'.repeat(200)},x`)
        state = await waitSnap(client, (s) => s.previewVisible, '200 csv preview')
        ctx.check('S8b CSV 200 字符可确认', state.confirmDisabled === false, state.confirmText)

        await selectText(client, 'total.csv', `标题,标签\n${Array.from({ length: 101 }, () => `${'x'.repeat(200)},y`).join('\n')}`)
        state = await waitSnap(client, (s) => s.previewVisible, 'total csv preview')
        ctx.check('S8b CSV 101×200 总字符/条数拒绝', state.confirmDisabled, state.confirmText)
        ctx.check('S8b CSV 边界操作无批量请求', (await getLog()).batchCalls === 0, String((await getLog()).batchCalls))
      },
    },
    {
      id: 'csv-unrecoverable',
      name: 'S9 不可恢复 CSV 结构错误不能跳过',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await selectText(client, 'unclosed.csv', '标题,标签\n"甲,新闻')
        const state = await waitSnap(client, (s) => s.previewVisible, 'unclosed preview')
        ctx.check('S9 不可恢复错误显示', state.fileErrors.includes('未闭合'), state.fileErrors)
        ctx.check('S9 不可恢复错误不能跳过', !state.skipVisible && state.skipUnavailable, JSON.stringify(state))
        ctx.check('S9 不可恢复错误禁止导入', state.confirmDisabled, state.confirmText)
        ctx.check('S9 未发批量请求', (await getLog()).batchCalls === 0, String((await getLog()).batchCalls))
      },
    },
    {
      id: 'race-cleanup',
      name: 'S9b 快速切换/重复选择/双击确认不产生过期覆盖',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await action(client, 'setReadDelay', 350)
        await selectText(client, 'slow.txt', '慢文件一\n慢文件二')
        // 文件读取尚未完成时卸载 FileImportPanel。
        await action(client, 'setTitleMode', 'single')
        await new Promise((resolve) => setTimeout(resolve, 500))
        let state = await snap(client)
        ctx.check('S9b 读取中切换模式不残留预览', state.titleMode === 'single' && !state.previewVisible && !state.fileInputPresent, JSON.stringify(state))
        await action(client, 'setReadDelay', 0)
        await action(client, 'setTitleMode', 'multiple')
        await action(client, 'resetArrayBufferCalls')

        await selectText(client, 'first.txt', '旧文件一\n旧文件二')
        await selectText(client, 'second.txt', '新文件一\n新文件二')
        state = await waitSnap(client, (s) => s.previewVisible && s.fileMeta.includes('second.txt'), 'latest file wins')
        ctx.eq('S9b 后选文件覆盖先选文件', rowTitles(state.fileItems), ['新文件一', '新文件二'])
        ctx.eq('S9b 两次文件读取计数', state.arrayBufferCalls, 2)

        await action(client, 'clickFileConfirm')
        await action(client, 'clickFileConfirm')
        state = await waitSnap(client, (s) => s.importedVisible, 'double confirm imported')
        ctx.eq('S9b 双击确认只导入一份标题', rowTitles(state.importedItems), ['新文件一', '新文件二'])
        ctx.check('S9b 双击确认未发批量请求', (await getLog()).batchCalls === 0, String((await getLog()).batchCalls))
      },
    },
    {
      id: 'csv-reparse',
      name: 'S9c CSV 表头/列切换复用同一字节并同步统计',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await selectText(client, 'reparse.csv', '甲,乙\n丙,丁\n戊,己')
        let state = await waitSnap(client, (s) => s.previewVisible, 'reparse preview')
        ctx.eq('S9c 初次读取次数', state.arrayBufferCalls, 1)
        ctx.check('S9c 初始数据记录 3', state.fileStats.includes('数据记录 3'), state.fileStats)

        await action(client, 'clickHeader', 'data')
        state = await waitSnap(client, (s) => s.headerChoice === 'data', 'reparse data header')
        ctx.check('S9c 首行数据后仍为 3 条', state.fileStats.includes('数据记录 3') && rowTitles(state.fileItems).join('|') === '甲|丙|戊', JSON.stringify(state.fileStats))
        ctx.eq('S9c 表头切换未重新读取文件', state.arrayBufferCalls, 1)

        await action(client, 'chooseColumn', 1)
        state = await waitSnap(client, (s) => s.columnValue === '1', 'reparse column 1')
        ctx.eq('S9c 列切换后的标题', rowTitles(state.fileItems), ['乙', '丁', '己'])
        ctx.eq('S9c 列切换后的记录号', state.fileItems.map((item) => item.recordNo), [1, 2, 3])
        ctx.eq('S9c 列切换未重新读取文件', state.arrayBufferCalls, 1)

        await action(client, 'clickHeader', 'header')
        state = await waitSnap(client, (s) => s.headerChoice === 'header', 'reparse header')
        ctx.check('S9c 首行表头后数据记录 2', state.fileStats.includes('数据记录 2'), state.fileStats)
        ctx.eq('S9c 首行表头后的默认列标题', rowTitles(state.fileItems), ['丙', '戊'])
        ctx.eq('S9c 首行表头后物理行号', state.fileItems.map((item) => item.lineNo), [2, 3])
        ctx.eq('S9c 表头切换仍未重新读取文件', state.arrayBufferCalls, 1)

        await action(client, 'chooseColumn', 1)
        state = await waitSnap(client, (s) => s.columnValue === '1', 'reparse header column 1')
        ctx.eq('S9c 表头+第二列标题', rowTitles(state.fileItems), ['丁', '己'])
        ctx.eq('S9c 表头+第二列记录号', state.fileItems.map((item) => item.recordNo), [1, 2])
      },
    },
    {
      id: 'state-preservation',
      name: 'S9d 无效文件/过大文件/取消预览保留现有草稿与结果',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await action(client, 'setBatchText', '保留一\n保留二')
        await waitSnap(client, (s) => s.manualValue.includes('保留一'), 'preserve draft')
        await action(client, 'clickSubmit')
        let state = await waitSnap(client, (s) => s.batchPhase === 'done', 'preserve results')
        ctx.check('S9d 先有结果', state.batchRows.length === 2, JSON.stringify(state.batchRows))
        const initialCalls = (await getLog()).batchCalls

        await selectBytes(client, 'bad.md', [0x41])
        state = await waitSnap(client, (s) => Boolean(s.fileError), 'invalid preserve')
        ctx.check('S9d 无效扩展名保留草稿/结果', state.manualValue.includes('保留一') && state.batchRows.length === 2 && state.batchPhase === 'done', JSON.stringify(state))
        ctx.check('S9d 无效扩展名不发请求', (await getLog()).batchCalls === initialCalls, String((await getLog()).batchCalls))

        await selectLargeFile(client, 'big.txt', MAX_FILE_BYTES + 1)
        state = await waitSnap(client, (s) => s.fileError.includes('1 MiB'), 'large preserve')
        ctx.check('S9d 过大文件保留草稿/结果', state.manualValue.includes('保留一') && state.batchRows.length === 2 && state.batchPhase === 'done', JSON.stringify(state))
        ctx.check('S9d 过大文件未读取字节', state.arrayBufferCalls === 0, String(state.arrayBufferCalls))

        await selectText(client, 'preview.txt', '新文件一\n新文件二')
        await waitSnap(client, (s) => s.previewVisible, 'cancel preserve preview')
        await action(client, 'clickFileCancel')
        state = await snap(client)
        ctx.check('S9d 取消预览保留草稿/结果', state.manualValue.includes('保留一') && state.batchRows.length === 2 && state.batchPhase === 'done', JSON.stringify(state))
      },
    },
    {
      id: 'replacement',
      name: 'S10 替换确认、取消保留、替换后恢复手动草稿',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await action(client, 'setBatchText', '手动一\n手动二')
        await waitSnap(client, (s) => s.manualValue.includes('手动一'), 'manual draft')
        await selectText(client, 'file.txt', '文件一\n文件二')
        await waitSnap(client, (s) => s.previewVisible, 'replacement preview')

        await action(client, 'clickFileConfirm')
        let state = await waitSnap(client, (s) => s.replacementVisible, 'replacement dialog')
        ctx.check('S10 有草稿时先显示替换确认', state.replacementVisible, JSON.stringify(state))
        ctx.check('S10 替换确认默认焦点在取消', state.replacementFocusCancel, JSON.stringify(state))
        await action(client, 'clickReplaceCancel')
        state = await snap(client)
        ctx.check('S10 取消替换保留手动草稿', !state.importedVisible && state.manualValue === '手动一\n手动二', JSON.stringify(state))
        ctx.check('S10 取消替换不清空预览/结果状态', state.previewVisible, JSON.stringify(state))

        await action(client, 'clickFileConfirm')
        await waitSnap(client, (s) => s.replacementVisible, 'replacement dialog again')
        await action(client, 'clickReplaceConfirm')
        state = await waitSnap(client, (s) => s.importedVisible, 'replaced imported')
        ctx.eq('S10 替换后导入标题', rowTitles(state.importedItems), ['文件一', '文件二'])
        ctx.check('S10 替换后未发批量请求', (await getLog()).batchCalls === 0, String((await getLog()).batchCalls))

        await action(client, 'clickClearImported')
        state = await waitSnap(client, (s) => !s.importedVisible && s.manualValue.includes('手动一'), 'manual restored')
        ctx.eq('S10 清除导入恢复手动草稿', state.manualValue, '手动一\n手动二')
      },
    },
    {
      id: 'replacement-results',
      name: 'S10b 已有结果时取消替换保留结果，替换后明确清空',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await action(client, 'setBatchText', '结果一\n结果二')
        await waitSnap(client, (s) => s.manualValue.includes('结果一'), 'result draft')
        await action(client, 'clickSubmit')
        let state = await waitSnap(client, (s) => s.batchPhase === 'done', 'manual results done')
        ctx.check('S10b 先有批量结果', state.batchRows.length === 2, JSON.stringify(state.batchRows))
        const beforeCalls = (await getLog()).batchCalls

        await selectText(client, 'replacement.txt', '文件一\n文件二')
        await waitSnap(client, (s) => s.previewVisible, 'replacement result preview')
        await action(client, 'clickFileConfirm')
        state = await waitSnap(client, (s) => s.replacementVisible, 'replacement result dialog')
        ctx.check('S10b 有结果时显示替换确认', state.replacementVisible, JSON.stringify(state))
        await action(client, 'clickReplaceCancel')
        state = await snap(client)
        ctx.check('S10b 取消替换保留结果', state.batchRows.length === 2 && state.batchPhase === 'done', JSON.stringify({ rows: state.batchRows, phase: state.batchPhase }))
        ctx.check('S10b 取消替换不发新请求', (await getLog()).batchCalls === beforeCalls, String((await getLog()).batchCalls))

        await action(client, 'clickFileConfirm')
        await waitSnap(client, (s) => s.replacementVisible, 'replacement result dialog again')
        await action(client, 'clickReplaceConfirm')
        state = await waitSnap(client, (s) => s.importedVisible, 'replacement results imported')
        ctx.check('S10b 替换后结果清空', state.batchRows.length === 0, JSON.stringify(state.batchRows))
        ctx.eq('S10b 替换后导入标题', rowTitles(state.importedItems), ['文件一', '文件二'])
        await action(client, 'clickClearImported')
        state = await waitSnap(client, (s) => !s.importedVisible, 'clear after result replacement')
        ctx.eq('S10b 清除导入恢复手动草稿', state.manualValue, '结果一\n结果二')
      },
    },
    {
      id: 'batch-25',
      name: 'S11 文件 25 条按 10/10/5 串行提交并映射来源',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        const titles = Array.from({ length: 25 }, (_, index) => `文件第${index + 1}条`)
        await selectText(client, '25.txt', titles.join('\n'))
        await waitSnap(client, (s) => s.previewVisible, '25 preview')
        await action(client, 'clickFileConfirm')
        await waitSnap(client, (s) => s.importedVisible, '25 imported')
        ctx.check('S11 导入后提交前无请求', (await getLog()).batchCalls === 0, String((await getLog()).batchCalls))
        await action(client, 'clickSubmit')
        const state = await waitSnap(client, (s) => s.batchPhase === 'done', '25 done', 15_000)
        const log = await getLog()
        const calls = batchBodies(log)
        ctx.eq('S11 分片数量', calls.length, 3)
        ctx.eq('S11 分片大小 10/10/5', calls.map((call) => call.body.items.length), [10, 10, 5])
        ctx.check('S11 串行峰值≤1', log.batchMaxActive <= 1, String(log.batchMaxActive))
        ctx.eq('S11 同一 batch_id', new Set(calls.map((call) => call.body.batch_id)).size, 1)
        ctx.check('S11 请求体无文件名/文件字节', calls.every((call) => {
          const keys = Object.keys(call.body)
          return keys.every((key) => ['model', 'batch_id', 'items'].includes(key))
        }), JSON.stringify(calls.map((call) => Object.keys(call.body))))
        ctx.eq('S11 结果顺序与标题', rowTitles(state.batchRows), titles)
        ctx.eq('S11 结果来源行号', state.batchRows.map((row) => row.lineNo), titles.map((_, index) => index + 1))
        ctx.check('S11 结果均成功', state.batchRows.every((row) => row.status === 'ok'), JSON.stringify(state.batchRows))
      },
    },
    {
      id: 'mapping-order',
      name: 'S11b 响应乱序/缺项仍按 item_id 映射，缺失项可重试',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await action(client, 'setResponseMode', 'reorder-omit')
        await selectText(client, 'mapping.txt', '映射一\n映射二\n映射三')
        await waitSnap(client, (s) => s.previewVisible, 'mapping preview')
        await action(client, 'clickFileConfirm')
        await waitSnap(client, (s) => s.importedVisible, 'mapping imported')
        await action(client, 'clickSubmit')
        let state = await waitSnap(client, (s) => s.batchPhase === 'partial', 'mapping partial')
        ctx.eq('S11b 结果顺序仍按输入', rowTitles(state.batchRows), ['映射一', '映射二', '映射三'])
        ctx.eq('S11b 乱序响应按 item_id 落位', state.batchRows.map((row) => row.status), ['ok', 'error', 'ok'])
        ctx.check('S11b 缺项不伪造成功', state.batchRows[1]?.error.includes('BATCH_RESPONSE_MISSING'), JSON.stringify(state.batchRows))
        ctx.check('S11b 只发一个分片', batchBodies(await getLog()).length === 1, String(batchBodies(await getLog()).length))

        await action(client, 'setResponseMode', 'normal')
        await setScenario('normal')
        const afterReset = await getLog()
        ctx.check('S11b 重试前 mock 日志已重置', afterReset.batchCalls === 0, String(afterReset.batchCalls))
        await action(client, 'clickRetry')
        state = await waitSnap(client, (s) => s.batchPhase === 'done', 'mapping retry')
        const log = await getLog()
        const retryCalls = batchBodies(log)
        ctx.eq('S11b 重试只有一个请求', retryCalls.length, 1)
        ctx.eq('S11b 重试只提交缺失项', retryCalls[0]?.body?.items?.map((item) => item.title), ['映射二'])
        ctx.check('S11b 重试后全部成功', state.batchRows.every((row) => row.status === 'ok'), JSON.stringify(state.batchRows))
      },
    },
    {
      id: 'partial-retry',
      name: 'S12 文件导入后部分失败/重试失败项',
      async run(client, ctx) {
        await setScenario('partial')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        const titles = Array.from({ length: 12 }, (_, index) => `第${index + 1}条`)
        titles[11] = 'FAIL 文件条目'
        await selectText(client, 'partial.txt', titles.join('\n'))
        await waitSnap(client, (s) => s.previewVisible, 'partial preview')
        await action(client, 'clickFileConfirm')
        await waitSnap(client, (s) => s.importedVisible, 'partial imported')
        await action(client, 'clickSubmit')
        let state = await waitSnap(client, (s) => s.batchPhase === 'partial', 'partial done', 15_000)
        ctx.check('S12 部分失败保留成功项', state.batchRows.filter((row) => row.status === 'ok').length === 11, JSON.stringify(state.batchRows))
        ctx.check('S12 失败项有错误', state.batchRows.filter((row) => row.status === 'error').length === 1, JSON.stringify(state.batchRows))

        await setScenario('normal')
        await action(client, 'clickRetry')
        state = await waitSnap(client, (s) => s.batchPhase === 'done', 'retry done', 10_000)
        const log = await getLog()
        const calls = batchBodies(log)
        ctx.eq('S12 重试只发一个分片', calls.length, 1)
        ctx.eq('S12 重试只提交失败项', calls[0]?.body?.items?.map((item) => item.title), ['FAIL 文件条目'])
        ctx.check('S12 重试后全部成功', state.batchRows.every((row) => row.status === 'ok'), JSON.stringify(state.batchRows))
      },
    },
    {
      id: 'csv-partial-source',
      name: 'S12b CSV 部分失败/重试保留记录号来源',
      async run(client, ctx) {
        await setScenario('partial')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await selectText(client, 'partial-source.csv', '标题,标签\nA,1\nB,2\nFAIL,3\nD,4')
        await waitSnap(client, (s) => s.previewVisible, 'csv partial preview')
        await action(client, 'clickFileConfirm')
        await waitSnap(client, (s) => s.importedVisible, 'csv partial imported')
        await action(client, 'clickSubmit')
        let state = await waitSnap(client, (s) => s.batchPhase === 'partial', 'csv partial result', 12_000)
        const failed = state.batchRows.find((row) => row.status === 'error')
        ctx.eq('S12b 失败项记录号', failed?.recordNo, 3)
        ctx.eq('S12b 失败项物理行号', failed?.lineNo, 4)
        ctx.check('S12b 成功项保留', state.batchRows.filter((row) => row.status === 'ok').length === 3, JSON.stringify(state.batchRows))

        await setScenario('normal')
        await action(client, 'clickRetry')
        state = await waitSnap(client, (s) => s.batchPhase === 'done', 'csv partial retry')
        const log = await getLog()
        const calls = batchBodies(log)
        ctx.eq('S12b 重试只提交失败项', calls[0]?.body?.items?.map((item) => item.title), ['FAIL'])
        ctx.check('S12b 重试后失败项保留记录号', state.batchRows.find((row) => row.title === 'FAIL')?.recordNo === 3, JSON.stringify(state.batchRows))
      },
    },
    {
      id: 'chunk-failure',
      name: 'S12c 后续分片失败保留完成项，重试仅失败片',
      async run(client, ctx) {
        await setScenario('chunk-fail')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await selectText(client, 'chunk.txt', Array.from({ length: 25 }, (_, index) => `分片${index + 1}`).join('\n'))
        await waitSnap(client, (s) => s.previewVisible, 'chunk preview')
        await action(client, 'clickFileConfirm')
        await waitSnap(client, (s) => s.importedVisible, 'chunk imported')
        await action(client, 'clickSubmit')
        let state = await waitSnap(client, (s) => s.batchPhase === 'partial', 'chunk partial', 12_000)
        ctx.check('S12c 第一片完成保留', state.batchRows.slice(0, 10).every((row) => row.status === 'ok'), JSON.stringify(state.batchRows.slice(0, 10)))
        ctx.check('S12c 第二片失败、后续未提交', state.batchRows.slice(10, 20).every((row) => row.status === 'error') && state.batchRows.slice(20).every((row) => row.status === 'skipped'), JSON.stringify(state.batchRows))
        ctx.eq('S12c 失败片来源行号', state.batchRows.slice(10, 20).map((row) => row.lineNo), Array.from({ length: 10 }, (_, index) => index + 11))

        await setScenario('normal')
        await action(client, 'clickRetry')
        state = await waitSnap(
          client,
          (s) => s.batchRows.slice(10, 20).every((row) => row.status === 'ok') && s.batchRows.slice(20).every((row) => row.status === 'skipped'),
          'chunk retry',
        )
        const log = await getLog()
        ctx.eq('S12c 重试只提交失败片 10 条', batchBodies(log)[0]?.body?.items?.length, 10)
        ctx.check('S12c 重试后失败片成功', state.batchRows.slice(10, 20).every((row) => row.status === 'ok'), JSON.stringify(state.batchRows.slice(10, 20)))
        ctx.check('S12c 未发送项仍为未提交', state.batchRows.slice(20).every((row) => row.status === 'skipped'), JSON.stringify(state.batchRows.slice(20)))
        ctx.check('S12c 存在未提交项时批次为 partial', state.batchPhase === 'partial', state.batchPhase)
      },
    },
    {
      id: 'cancel',
      name: 'S13 文件导入后取消后续分片',
      async run(client, ctx) {
        await setScenario('slow-second')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await selectText(client, 'cancel.txt', Array.from({ length: 25 }, (_, index) => `取消${index + 1}`).join('\n'))
        await waitSnap(client, (s) => s.previewVisible, 'cancel preview')
        await action(client, 'clickFileConfirm')
        await waitSnap(client, (s) => s.importedVisible, 'cancel imported')
        await action(client, 'clickSubmit')
        await waitSnap(
          client,
          (s) => s.batchRows.filter((row) => row.status === 'ok').length >= 10,
          'cancel first chunk',
          10_000,
        )
        await action(client, 'clickCancelBatch')
        const state = await waitSnap(client, (s) => s.batchPhase === 'cancelled', 'cancelled', 8000)
        const log = await getLog()
        ctx.check('S13 取消后不再发第三片', batchBodies(log).length <= 2, String(batchBodies(log).length))
        ctx.check('S13 已完成结果保留', state.batchRows.slice(0, 10).every((row) => row.status === 'ok'), JSON.stringify(state.batchRows.slice(0, 10)))
        ctx.check('S13 后续项取消/未提交', state.batchRows.slice(10).every((row) => row.status === 'cancelled' || row.status === 'skipped'), JSON.stringify(state.batchRows.slice(10)))
      },
    },
    {
      id: 'unmount-request',
      name: 'S13b 页面卸载中止后续分片，不遗留 pending 请求',
      async run(client, ctx) {
        await setScenario('slow-second')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await selectText(client, 'unmount.txt', Array.from({ length: 25 }, (_, index) => `卸载${index + 1}`).join('\n'))
        await waitSnap(client, (s) => s.previewVisible, 'unmount preview')
        await action(client, 'clickFileConfirm')
        await waitSnap(client, (s) => s.importedVisible, 'unmount imported')
        await action(client, 'clickSubmit')
        await waitSnap(
          client,
          (s) => s.batchRows.filter((row) => row.status === 'ok').length >= 10,
          'unmount first chunk',
          10_000,
        )
        await client.send('Page.reload', { ignoreCache: true })
        await waitFor(
          async () => evaluate(client, 'Boolean(window.__f19 && window.__f19.ready)'),
          { timeout: 15_000, label: 'reload ready' },
        )
        await new Promise((resolve) => setTimeout(resolve, 1200))
        const log = await getLog()
        ctx.check('S13b 卸载后不再发送后续分片', batchBodies(log).length <= 2, String(batchBodies(log).length))
      },
    },
    {
      id: 'xss',
      name: 'S14 文件标题按纯文本显示，不执行脚本',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await action(client, 'setTitleMode', 'multiple')
        await selectText(client, 'xss.txt', '<script>window.__xss=1</script>\n正常标题')
        const state = await waitSnap(client, (s) => s.previewVisible, 'xss preview')
        ctx.check('S14 预览显示脚本文本', state.bodyText.includes('<script>window.__xss=1</script>'), state.bodyText.slice(0, 300))
        ctx.eq('S14 预览未执行脚本', state.xss, 0)
      },
    },
    {
      id: 'mobile',
      name: 'S15 390px 文件预览/导入/结果无横向溢出',
      async run(client, ctx) {
        await setScenario('normal')
        await setViewport(client, 390, 844)
        try {
          await navigate(client)
          await action(client, 'setTitleMode', 'multiple')
          await selectText(client, 'mobile.txt', '第一条\n第二条\n第三条')
          let state = await waitSnap(client, (s) => s.previewVisible, 'mobile preview')
          ctx.check('S15 390 预览无横向溢出', state.pageScrollWidth <= state.pageClientWidth, JSON.stringify({ width: state.pageClientWidth, scroll: state.pageScrollWidth }))
          await action(client, 'clickFileConfirm')
          state = await waitSnap(client, (s) => s.importedVisible, 'mobile imported')
          await action(client, 'clickSubmit')
          state = await waitSnap(client, (s) => s.batchPhase === 'done', 'mobile done')
          ctx.check('S15 390 结果无横向溢出', state.pageScrollWidth <= state.pageClientWidth, JSON.stringify({ width: state.pageClientWidth, scroll: state.pageScrollWidth }))
          ctx.check('S15 390 结果 3 条', state.batchRows.length === 3 && state.batchRows.every((row) => row.status === 'ok'), JSON.stringify(state.batchRows))
        } finally {
          await clearViewport(client)
        }
      },
    },
  ]

  console.log('=== F19 阶段 3B 文件导入 UI 验收（ClassifyPanel + mock API + Edge headless） ===')
  let edge = null
  let client = null
  const summary = []
  let failures = 0
  let totalChecks = 0

  try {
    const edgePath = EDGE_CANDIDATES.find((candidate) => existsSync(candidate))
    if (!edgePath) throw new Error('未找到 Microsoft Edge（msedge.exe）')
    edge = spawn(edgePath, [
      '--headless=new',
      '--disable-gpu',
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-extensions',
      '--disable-background-networking',
      '--disable-sync',
      `--remote-debugging-port=${CDP_PORT}`,
      `--user-data-dir=${profileDir}`,
      '--window-size=1440,1400',
      'about:blank',
    ], { stdio: 'ignore', windowsHide: true })

    let version = null
    await waitFor(async () => {
      try {
        const response = await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`)
        if (!response.ok) return null
        version = await response.json()
        return version
      } catch {
        return null
      }
    }, { timeout: 20_000, interval: 250, label: 'Edge CDP ready' })

    const targets = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json()
    const pageTarget = targets.find((target) => target.type === 'page')
    if (!pageTarget?.webSocketDebuggerUrl) throw new Error('未找到 Edge page target')
    client = await CdpClient.connect(pageTarget.webSocketDebuggerUrl)
    await client.send('Runtime.enable')
    await client.send('Page.enable')
    console.log(`[edge] ${version?.Browser || 'Edge'}`)

    for (const scenario of scenarios) {
      const { checks, check, eq } = buildChecks()
      const context = { check, eq }
      let error = null
      try {
        await scenario.run(client, context)
      } catch (reason) {
        error = reason
        check('场景执行未抛异常', false, reason.stack || reason.message)
      }
      const failed = checks.filter((item) => !item.pass)
      failures += failed.length
      totalChecks += checks.length
      const pass = !error && failed.length === 0
      summary.push({ id: scenario.id, name: scenario.name, pass, checks })
      console.log(`\n[${pass ? 'PASS' : 'FAIL'}] ${scenario.name}`)
      for (const item of checks) {
        console.log(`  ${item.pass ? '✓' : '✗'} ${item.name}${item.detail ? ` — ${item.detail}` : ''}`)
      }
      if (error) console.log(`  ! ${error.stack || error.message}`)
    }

    console.log('\n=== 结果汇总 ===')
    for (const item of summary) {
      const passed = item.checks.filter((check) => check.pass).length
      console.log(`${item.pass ? 'PASS' : 'FAIL'}  ${item.id.padEnd(20)} ${passed}/${item.checks.length}`)
    }
    console.log(`\n总结果：${failures === 0 ? 'PASS' : 'FAIL'}（失败 ${failures} / ${totalChecks}）`)
    process.exitCode = failures === 0 ? 0 : 1
  } finally {
    client?.close()
    if (edge) {
      try {
        edge.kill()
      } catch {
        // ignore
      }
    }
    await mock.close()
    await sleep(300)
    await rm(profileDir, { recursive: true, force: true }).catch(() => {})
  }
}

main().catch((error) => {
  console.error('[F19] 运行失败：', error.stack || error.message)
  process.exitCode = 1
})
