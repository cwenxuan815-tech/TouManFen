/**
 * F17 阶段 2B 前端验收：真实 ClassifyPanel + 本地 mock API + Edge headless/CDP。
 *
 * 运行（在 frontend/ 下）：
 *   node tests/f17/run-scenarios.mjs
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdir, rm, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import os from 'node:os'
import { build } from 'vite'
import { createMockServer } from './mock-server.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const frontendRoot = resolve(here, '../..')
const distDir = join(here, 'dist')
const envDir = join(here, 'env')
const SERVER_PORT = 18191
const CDP_PORT = 9340
const HARNESS_URL = `http://127.0.0.1:${SERVER_PORT}/tests/f17/harness.html`

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
      const text = typeof event.data === 'string' ? event.data : String(event.data)
      let message
      try {
        message = JSON.parse(text)
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

async function waitFor(fn, { timeout = 10_000, interval = 80, label = 'condition' } = {}) {
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
  await client.send('Page.navigate', {
    url: `${HARNESS_URL}?t=${Date.now()}`,
  })
  await waitFor(
    async () => evaluate(client, 'Boolean(window.__f17 && window.__f17.ready)'),
    { timeout: 15_000, label: 'harness ready' },
  )
}

async function snap(client) {
  return evaluate(client, 'window.__f17.snapshot()')
}

async function waitSnap(client, predicate, label, timeout = 12_000) {
  return waitFor(async () => {
    const state = await snap(client)
    return predicate(state) ? state : null
  }, { timeout, label })
}

async function setSingleText(client, value) {
  const ok = await evaluate(
    client,
    `window.__f17.setSingleText(${JSON.stringify(value)})`,
  )
  if (!ok) throw new Error('single textarea not found')
  await sleep(50)
}

async function setBatchText(client, value) {
  const ok = await evaluate(
    client,
    `window.__f17.setBatchText(${JSON.stringify(value)})`,
  )
  if (!ok) throw new Error('batch textarea not found')
  await sleep(50)
}

async function click(client, method, arg = '') {
  const expression = arg
    ? `window.__f17.${method}(${JSON.stringify(arg)})`
    : `window.__f17.${method}()`
  const ok = await evaluate(client, expression)
  if (!ok) throw new Error(`${method} failed`)
  await sleep(30)
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

const viewportData = (state) => ({
  scrollWidth: state.pageScrollWidth,
  clientWidth: state.pageClientWidth,
})

async function main() {
  await mkdir(envDir, { recursive: true })
  await writeFile(
    join(envDir, '.env.local'),
    `VITE_API_BASE_URL=http://127.0.0.1:${SERVER_PORT}\n`,
  )
  process.env.VITE_API_BASE_URL = `http://127.0.0.1:${SERVER_PORT}`

  console.log(`[build] 构建 F17 harness（${distDir}）`)
  await build({ configFile: join(here, 'vite.harness.config.mjs'), logLevel: 'warn' })
  console.log('[build] OK')

  const mock = await createMockServer({ port: SERVER_PORT, staticDir: distDir })
  const profileDir = join(os.tmpdir(), `tmf-f17-edge-${process.pid}-${Date.now()}`)
  await mkdir(profileDir, { recursive: true })

  const scenarios = [
    {
      id: 'single-newline',
      name: 'S1 单条模式：内部换行不自动拆分，仍只发 /classify',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        let state = await snap(client)
        ctx.check('S1 默认单条模式', state.topMode === 'single' && state.titleMode === 'single', JSON.stringify(state))

        await setSingleText(client, 'A\nB')
        state = await snap(client)
        ctx.check('S1 检测到内部换行并提示，不自动拆分', state.singleNewlineHintVisible === true, JSON.stringify(state))
        ctx.check('S1 单条提交可用', state.submitDisabled === false, JSON.stringify(state))

        await click(client, 'clickSubmit')
        state = await waitSnap(client, (s) => s.resultCardCount > 0, 'single result')
        const log = await getLog()
        const classifyCalls = log.requests.filter((item) => item.kind === 'classify')
        ctx.check('S1 只调用 /classify 一次', classifyCalls.length === 1, JSON.stringify(classifyCalls))
        ctx.check('S1 /classify 收到完整换行文本', classifyCalls[0]?.body?.title === 'A\nB', JSON.stringify(classifyCalls[0]?.body))
        ctx.check('S1 未调用批量端点', log.batchCalls === 0, String(log.batchCalls))
        ctx.check('S1 单条结果卡出现', state.resultCardCount === 1, String(state.resultCardCount))
      },
    },
    {
      id: 'batch-parse',
      name: 'S2 多条解析：CRLF/CR/LF、trim、空行、重复与映射',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await click(client, 'clickTitleMode', 'multiple')
        await setBatchText(client, '  A  \r\n\r\nB\rC\nA\n😀')
        const state = await waitSnap(
          client,
          (s) => s.preview.length === 5,
          'batch preview 5',
        )
        ctx.check('S2 标题模式为多条', state.titleMode === 'multiple', JSON.stringify(state))
        ctx.check('S2 空行忽略，保留原始行号', JSON.stringify(state.preview.map((row) => row.lineNo)) === JSON.stringify([1, 3, 4, 5, 6]), JSON.stringify(state.preview))
        ctx.check('S2 每行 trim', JSON.stringify(state.preview.map((row) => row.title)) === JSON.stringify(['A', 'B', 'C', 'A', '😀']), JSON.stringify(state.preview))
        ctx.check('S2 重复标题保留并提示', state.batchDuplicate.includes('1 条') && state.preview.filter((row) => row.duplicate).length === 2, JSON.stringify({ duplicate: state.batchDuplicate, preview: state.preview }))
        ctx.check('S2 显示条数与总字符数', state.batchCount.includes('5') && state.bodyText.includes('5 条 · 5 / 20000 字符'), JSON.stringify({ count: state.batchCount }))

        await click(client, 'clickSubmit')
        const done = await waitSnap(client, (s) => s.batchPhase === 'ok' || s.batchPhase === 'done', 'batch done')
        const log = await getLog()
        ctx.check('S2 只发一个批量分片', log.batchCalls === 1, String(log.batchCalls))
        ctx.check('S2 请求 items 与输入映射一致', JSON.stringify(log.requests.find((item) => item.kind === 'batch')?.body?.items?.map((item) => item.item_id)) === JSON.stringify(['r000001', 'r000003', 'r000004', 'r000005', 'r000006']), JSON.stringify(log.requests.find((item) => item.kind === 'batch')?.body))
        ctx.check('S2 结果顺序/数量正确', done.batchRows.length === 5 && done.batchRows.every((row) => row.status === 'ok') && done.progressDone === 5 && done.progressTotal === 5, JSON.stringify(done))
        ctx.check('S2 指标来源 mock，不伪造 live', done.batchRows.every((row) => row.category !== '—'), JSON.stringify(done.batchRows))
      },
    },
    {
      id: 'limits',
      name: 'S3 前端限制：空、101 条、超长、Unicode code point',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await click(client, 'clickTitleMode', 'multiple')

        await setBatchText(client, '')
        let state = await snap(client)
        ctx.check('S3 空输入禁用提交', state.submitDisabled === true && state.preview.length === 0, JSON.stringify(state))

        await setBatchText(client, Array.from({ length: 101 }, (_, index) => `标题${index}`).join('\n'))
        state = await waitSnap(client, (s) => s.preview.length === 101, '101 preview')
        ctx.check('S3 101 条显示上限错误并禁用提交', state.submitDisabled === true && state.batchTooMany.length > 0, JSON.stringify(state))

        await setBatchText(client, '甲'.repeat(201))
        state = await waitSnap(client, (s) => s.batchTooLong.length > 0, '201 preview')
        ctx.check('S3 单条 201 字符标记超长并禁用', state.submitDisabled === true && state.preview[0]?.charCount === 201, JSON.stringify(state))

        await setBatchText(client, '😀'.repeat(200))
        state = await waitSnap(client, (s) => s.preview[0]?.charCount === 200, '200 emoji')
        ctx.check('S3 200 个 emoji 按 code point 计为 200 且可提交', state.submitDisabled === false && state.preview[0]?.charCount === 200, JSON.stringify(state))

        await setBatchText(client, '😀'.repeat(201))
        state = await waitSnap(client, (s) => s.batchTooLong.length > 0 && s.preview[0]?.charCount === 201, '201 emoji')
        ctx.check('S3 201 个 emoji 按 code point 计为 201 并禁用', state.submitDisabled === true, JSON.stringify(state))

        const maxText = Array.from({ length: 100 }, (_, index) => `${index}`.padEnd(200, 'x')).join('\n')
        await setBatchText(client, maxText)
        state = await waitSnap(client, (s) => s.preview.length === 100, '100 max titles')
        ctx.check('S3 100×200=20,000 code point 可提交', state.submitDisabled === false, JSON.stringify({ preview: state.preview.length, validation: state.batchValidation }))
        const log = await getLog()
        ctx.check('S3 前端校验失败时不发请求', log.batchCalls === 0, String(log.batchCalls))
      },
    },
    {
      id: 'chunking',
      name: 'S4 分片：每 10 条串行、进度、顺序',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await click(client, 'clickTitleMode', 'multiple')
        const titles = Array.from({ length: 25 }, (_, index) => `第${index + 1}条标题`)
        await setBatchText(client, titles.join('\n'))
        await waitSnap(client, (s) => s.preview.length === 25, '25 preview')
        await click(client, 'clickSubmit')
        const state = await waitSnap(client, (s) => s.batchPhase === 'ok' || s.batchPhase === 'done', 'chunk done', 15_000)
        const log = await getLog()
        const calls = log.requests.filter((item) => item.kind === 'batch')
        ctx.check('S4 25 条严格分 3 片', calls.length === 3 && JSON.stringify(calls.map((item) => item.body.items.length)) === JSON.stringify([10, 10, 5]), JSON.stringify(calls.map((item) => item.body.items.length)))
        ctx.check('S4 分片串行，峰值 ≤1', log.batchMaxActive <= 1, String(log.batchMaxActive))
        ctx.check('S4 同一 batch_id 关联三片', new Set(calls.map((item) => item.body.batch_id)).size === 1, JSON.stringify(calls.map((item) => item.body.batch_id)))
        ctx.check('S4 进度 25/25', state.progressDone === 25 && state.progressTotal === 25, JSON.stringify(state))
        ctx.check('S4 结果顺序与输入一致', JSON.stringify(state.batchRows.map((row) => row.title)) === JSON.stringify(titles), JSON.stringify(state.batchRows.map((row) => row.title)))
        ctx.check('S4 全部成功', state.batchRows.every((row) => row.status === 'ok'), JSON.stringify(state.batchRows))
      },
    },
    {
      id: 'partial-retry',
      name: 'S5 部分失败保留成功项，重试只提交失败项',
      async run(client, ctx) {
        await setScenario('partial')
        await navigate(client)
        await click(client, 'clickTitleMode', 'multiple')
        const titles = Array.from({ length: 12 }, (_, index) => `第${index + 1}条`)
        titles[11] = 'FAIL 条目'
        await setBatchText(client, titles.join('\n'))
        await waitSnap(client, (s) => s.preview.length === 12, '12 preview')
        await click(client, 'clickSubmit')
        let state = await waitSnap(client, (s) => s.batchPhase === 'partial' || s.batchPhase === 'error', 'partial result', 15_000)
        ctx.check('S5 部分失败显示 partial/error', state.batchPhase === 'partial' && state.batchRows.filter((row) => row.status === 'ok').length === 11 && state.batchRows.filter((row) => row.status === 'error').length === 1, JSON.stringify(state))
        ctx.check('S5 失败项不伪造类别', state.batchRows.find((row) => row.status === 'error')?.category === '—', JSON.stringify(state.batchRows))

        await setScenario('normal')
        await click(client, 'clickRetry')
        state = await waitSnap(client, (s) => s.batchPhase === 'ok' || s.batchPhase === 'done', 'retry done', 10_000)
        const log = await getLog()
        const calls = log.requests.filter((item) => item.kind === 'batch')
        ctx.check('S5 重试只发一个分片', calls.length === 1, String(calls.length))
        ctx.check('S5 重试请求只包含失败项', calls[0]?.body?.items?.length === 1 && calls[0]?.body?.items?.[0]?.title === 'FAIL 条目', JSON.stringify(calls[0]?.body))
        ctx.check('S5 重试后全部成功', state.batchRows.length === 12 && state.batchRows.every((row) => row.status === 'ok'), JSON.stringify(state.batchRows))
      },
    },
    {
      id: 'chunk-failure',
      name: 'S6 后续分片失败：保留已完成结果，未开始不提交',
      async run(client, ctx) {
        await setScenario('chunk-fail')
        await navigate(client)
        await click(client, 'clickTitleMode', 'multiple')
        await setBatchText(client, Array.from({ length: 25 }, (_, index) => `第${index + 1}条`).join('\n'))
        await waitSnap(client, (s) => s.preview.length === 25, '25 preview')
        await click(client, 'clickSubmit')
        const state = await waitSnap(client, (s) => s.batchPhase === 'partial' || s.batchPhase === 'error', 'chunk fail', 15_000)
        const log = await getLog()
        const calls = log.requests.filter((item) => item.kind === 'batch')
        ctx.check('S6 第二片失败后不再发第三片', calls.length === 2, String(calls.length))
        ctx.check('S6 第一片成功保留', state.batchRows.slice(0, 10).every((row) => row.status === 'ok'), JSON.stringify(state.batchRows.slice(0, 10)))
        ctx.check('S6 当前失败片标错、后续标未提交', state.batchRows.slice(10, 20).every((row) => row.status === 'error') && state.batchRows.slice(20).every((row) => row.status === 'skipped'), JSON.stringify(state.batchRows))
        ctx.check('S6 显示批次错误', state.batchError.length > 0 || state.bodyText.includes('暂时不可用'), state.batchError)
      },
    },
    {
      id: 'all-fail',
      name: 'S6b 全部条目失败：逐项错误、无伪造类别',
      async run(client, ctx) {
        await setScenario('all-fail')
        await navigate(client)
        await click(client, 'clickTitleMode', 'multiple')
        await setBatchText(client, '第一条\n第二条\n第三条')
        await waitSnap(client, (s) => s.preview.length === 3, 'all fail preview')
        await click(client, 'clickSubmit')
        const state = await waitSnap(client, (s) => s.batchPhase === 'error', 'all fail state', 10_000)
        ctx.check('S6b 全部失败显示 error 状态', state.batchPhase === 'error', JSON.stringify(state))
        ctx.check('S6b 三条均为失败且无伪造类别', state.batchRows.length === 3 && state.batchRows.every((row) => row.status === 'error' && row.category === '—'), JSON.stringify(state.batchRows))
        ctx.check('S6b 显示失败计数', state.batchSummary.includes('0 成功') && state.batchSummary.includes('3 失败'), state.batchSummary)
      },
    },
    {
      id: 'network-error',
      name: 'S6c 批量网络错误：可理解提示，不伪造结果',
      async run(client, ctx) {
        await setScenario('network')
        await navigate(client)
        await click(client, 'clickTitleMode', 'multiple')
        await setBatchText(client, '第一条\n第二条')
        await waitSnap(client, (s) => s.preview.length === 2, 'network preview')
        await click(client, 'clickSubmit')
        const state = await waitSnap(client, (s) => s.batchPhase === 'error' || s.batchPhase === 'partial', 'network error state', 10_000)
        ctx.check('S6c 网络错误显示错误态', state.batchPhase === 'error', JSON.stringify(state))
        ctx.check('S6c 条目无伪造类别', state.batchRows.every((row) => row.status === 'error' && row.category === '—'), JSON.stringify(state.batchRows))
        ctx.check('S6c 提示包含可理解错误信息', state.batchError.includes('无法连接') || state.bodyText.includes('无法连接'), JSON.stringify({ error: state.batchError }))
      },
    },
    {
      id: 'cancel',
      name: 'S7 取消：保留第一片，不继续第三片',
      async run(client, ctx) {
        await setScenario('slow-second')
        await navigate(client)
        await click(client, 'clickTitleMode', 'multiple')
        await setBatchText(client, Array.from({ length: 25 }, (_, index) => `第${index + 1}条`).join('\n'))
        await waitSnap(client, (s) => s.preview.length === 25, '25 preview')
        await click(client, 'clickSubmit')
        const started = await waitSnap(
          client,
          (s) => s.batchRows.filter((row) => row.status === 'ok').length >= 10 && s.batchPanelVisible,
          'first chunk done',
          10_000,
        )
        ctx.check('S7 第一片已完成', started.batchRows.slice(0, 10).every((row) => row.status === 'ok'), JSON.stringify(started.batchRows.slice(0, 10)))
        await click(client, 'clickCancel')
        const cancelled = await waitSnap(client, (s) => s.batchPhase === 'cancelled', 'cancel state', 8000)
        await sleep(150)
        const log = await getLog()
        const calls = log.requests.filter((item) => item.kind === 'batch')
        ctx.check('S7 取消后不再发第三片', calls.length <= 2, String(calls.length))
        ctx.check('S7 已完成结果保留', cancelled.batchRows.slice(0, 10).every((row) => row.status === 'ok'), JSON.stringify(cancelled.batchRows.slice(0, 10)))
        ctx.check('S7 未完成项标取消/未提交', cancelled.batchRows.slice(10).every((row) => row.status === 'cancelled' || row.status === 'skipped'), JSON.stringify(cancelled.batchRows.slice(10)))
        ctx.check('S7 取消说明可见', cancelled.bodyText.includes('已取消'), cancelled.bodyText.slice(0, 300))
      },
    },
    {
      id: 'llm-disabled',
      name: 'S8 批量模式隐藏/禁用 LLM，且不发 model=llm',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await click(client, 'toggleLlmConsent')
        await click(client, 'setModel', 'llm')
        let state = await snap(client)
        ctx.check('S8 单条模式可选中 LLM（前置条件）', state.modelValue === 'llm', JSON.stringify(state))
        await click(client, 'clickTitleMode', 'multiple')
        state = await snap(client)
        ctx.check('S8 切多条后自动回到非 LLM 模型', state.modelValue !== 'llm' && state.modelValue.length > 0, JSON.stringify(state))
        ctx.check('S8 LLM 选项禁用', state.llmOptionDisabled === true, String(state.llmOptionDisabled))
        ctx.check('S8 显示批量不支持 LLM 说明', state.batchLlmNote === true, JSON.stringify(state))
        await setBatchText(client, '一条标题')
        await waitSnap(client, (s) => s.preview.length === 1, 'one title')
        await click(client, 'clickSubmit')
        await waitSnap(client, (s) => s.batchPhase === 'ok' || s.batchPhase === 'done', 'llm guard batch done')
        const log = await getLog()
        ctx.check('S8 批量请求模型不是 llm', log.requests.find((item) => item.kind === 'batch')?.body?.model !== 'llm', JSON.stringify(log.requests.find((item) => item.kind === 'batch')?.body))
      },
    },
    {
      id: 'draft-preservation',
      name: 'S11 单条/多条切换保留草稿，单条结果与批量结果不混淆',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await setSingleText(client, '单条草稿')
        await click(client, 'clickTitleMode', 'multiple')
        await setBatchText(client, '多条甲\n多条乙')
        await waitSnap(client, (s) => s.preview.length === 2, 'draft batch preview')
        await click(client, 'clickTitleMode', 'single')
        let state = await snap(client)
        ctx.check('S11 切回单条保留单条草稿', state.singleValue === '单条草稿', JSON.stringify(state))
        await click(client, 'clickTitleMode', 'multiple')
        state = await snap(client)
        ctx.check('S11 切回多条保留多条草稿与预览', state.batchValue === '多条甲\n多条乙' && state.preview.length === 2, JSON.stringify(state))
        await click(client, 'clickSubmit')
        state = await waitSnap(client, (s) => s.batchPhase === 'ok' || s.batchPhase === 'done', 'draft batch done')
        ctx.check('S11 提交后批量结果可见', state.batchPanelVisible === true && state.batchRows.length === 2, JSON.stringify(state))
        await click(client, 'clickTitleMode', 'single')
        state = await snap(client)
        ctx.check('S11 切回单条时批量结果面板隐藏', state.batchPanelVisible === false, JSON.stringify(state))
        ctx.check('S11 单条模式无单条结果时结果区为空', state.resultCardCount === 0, JSON.stringify(state))
        await click(client, 'clickTitleMode', 'multiple')
        state = await snap(client)
        ctx.check('S11 再切回多条时批量结果仍保留', state.batchPanelVisible === true && state.batchRows.length === 2 && state.batchRows.every((row) => row.status === 'ok'), JSON.stringify(state))
      },
    },
    {
      id: 'compare-separation',
      name: 'S9 compare 仍为单标题模式，批量开关不出现',
      async run(client, ctx) {
        await setScenario('normal')
        await navigate(client)
        await click(client, 'clickTopMode', 'compare')
        const state = await snap(client)
        ctx.check('S9 顶层进入 compare', state.topMode === 'compare', JSON.stringify(state))
        ctx.check('S9 compare 不显示单条/多条开关', state.titleModeSwitchVisible === false, JSON.stringify(state))
        ctx.check('S9 compare 未触发批量请求', (await getLog()).batchCalls === 0, String((await getLog()).batchCalls))
      },
    },
    {
      id: 'mobile',
      name: 'S10 390px 批量输入/结果不横向溢出',
      async run(client, ctx) {
        await setScenario('normal')
        await setViewport(client, 390, 844)
        try {
          await navigate(client)
          await click(client, 'clickTitleMode', 'multiple')
          await setBatchText(client, '第一条新闻标题\n第二条新闻标题\n第三条新闻标题')
          const before = await waitSnap(client, (s) => s.preview.length === 3, 'mobile preview')
          ctx.check('S10 390 输入页无横向滚动', before.pageScrollWidth <= before.pageClientWidth, JSON.stringify(viewportData(before)))
          await click(client, 'clickSubmit')
          const state = await waitSnap(client, (s) => s.batchPhase === 'ok' || s.batchPhase === 'done', 'mobile done')
          ctx.check('S10 390 结果页无横向滚动', state.pageScrollWidth <= state.pageClientWidth, JSON.stringify(viewportData(state)))
          ctx.check('S10 390 结果 3 条', state.batchRows.length === 3 && state.batchRows.every((row) => row.status === 'ok'), JSON.stringify(state.batchRows))
        } finally {
          await clearViewport(client)
        }
      },
    },
  ]

  console.log('=== F17 阶段 2B 前端验收（ClassifyPanel + mock API + Edge headless） ===')
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
    const cdpReady = await waitFor(async () => {
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
    await client.send('Network.enable')
    console.log(`[edge] ${version?.Browser || cdpReady?.Browser || 'Edge'}`)

    for (const scenario of scenarios) {
      const { checks: checkList, check } = buildChecks()
      const context = { check }
      let error = null
      try {
        await scenario.run(client, context)
      } catch (reason) {
        error = reason
        check('场景执行未抛异常', false, reason.stack || reason.message)
      }
      const failed = checkList.filter((item) => !item.pass)
      failures += failed.length
      totalChecks += checkList.length
      const pass = !error && failed.length === 0
      summary.push({ id: scenario.id, name: scenario.name, pass, checks: checkList })
      console.log(`\n[${pass ? 'PASS' : 'FAIL'}] ${scenario.name}`)
      for (const item of checkList) {
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
  console.error('[F17] 运行失败：', error.stack || error.message)
  process.exitCode = 1
})
