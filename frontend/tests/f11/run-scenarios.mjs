/**
 * F11 验收运行器：本地 mock bench API + Edge headless（CDP）。
 *
 * 用法（在 frontend 目录下）：
 *   node tests/f11/run-scenarios.mjs
 *
 * 覆盖：三种指标切换、缺失 LLM 不画条、meta 逐项对应、包装层兼容、空/错误/仅路径不造假、
 * 网络请求与 Content-Type、截图；输出 PASS/FAIL 与退出码 0/1。
 * 全部数据来自本地 mock（tests/f11/fixtures/bench_pk.mock.json），不访问真实后端/LLM。
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import os from 'node:os'
import { build } from 'vite'
import { createMockBenchServer } from './mock-server.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const artifactDir = join(here, 'artifacts')
const distDir = join(here, 'dist')
const fixturePath = join(here, 'fixtures', 'bench_pk.mock.json')
const serverPort = 18090
const cdpPort = 9335
const harnessUrl = `http://127.0.0.1:${serverPort}/tests/f11/harness.html`
const edgeCandidates = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
]

const sleep = (ms) => new Promise((resolvePromise) => setTimeout(resolvePromise, ms))

async function waitFor(fn, { timeout = 10_000, interval = 100, label = 'condition' } = {}) {
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
    this.listeners = new Map()
    socket.addEventListener('message', (event) => {
      const text = typeof event.data === 'string' ? event.data : String(event.data)
      let message
      try {
        message = JSON.parse(text)
      } catch {
        return
      }
      if (message.id !== undefined) {
        const pending = this.pending.get(message.id)
        if (!pending) return
        this.pending.delete(message.id)
        if (message.error) pending.reject(new Error(`${message.error.code}: ${message.error.message}`))
        else pending.resolve(message.result)
        return
      }
      if (message.method) {
        for (const listener of this.listeners.get(message.method) || []) listener(message.params)
      }
    })
  }

  send(method, params = {}) {
    const id = ++this.nextId
    return new Promise((resolvePromise, reject) => {
      this.pending.set(id, { resolve: resolvePromise, reject })
      this.socket.send(JSON.stringify({ id, method, params }))
    })
  }

  on(method, listener) {
    const listeners = this.listeners.get(method) || new Set()
    listeners.add(listener)
    this.listeners.set(method, listeners)
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
    const description =
      response.exceptionDetails.exception?.description ||
      response.exceptionDetails.text ||
      'evaluate failed'
    throw new Error(description)
  }
  return response.result?.value
}

async function gotoHarness(client) {
  await client.send('Page.navigate', { url: harnessUrl })
  await waitFor(
    async () => {
      const ready = await evaluate(
        client,
        'document.readyState === "complete" && Boolean(window.__f11 && window.__f11.ready)',
      )
      if (ready !== true) return false
      const state = await evaluate(client, 'window.__f11.dom().state')
      return state && state !== 'loading' ? state : false
    },
    { timeout: 20_000, interval: 100, label: 'bench page settled' },
  )
}

async function captureScreenshot(client, name) {
  const response = await client.send('Page.captureScreenshot', {
    format: 'png',
    captureBeyondViewport: true,
  })
  await writeFile(join(artifactDir, `${name}.png`), Buffer.from(response.data, 'base64'))
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

function findEdgePath() {
  return edgeCandidates.find((candidate) => existsSync(candidate)) || null
}

async function launchEdge(profileDir) {
  const edgePath = findEdgePath()
  if (!edgePath) throw new Error('未找到 Microsoft Edge（msedge.exe）')
  const child = spawn(
    edgePath,
    [
      '--headless=new',
      '--disable-gpu',
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-extensions',
      '--disable-background-networking',
      '--disable-sync',
      `--remote-debugging-port=${cdpPort}`,
      `--user-data-dir=${profileDir}`,
      '--window-size=1440,1700',
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  )
  const version = await waitFor(
    async () => {
      try {
        const response = await fetch(`http://127.0.0.1:${cdpPort}/json/version`)
        return response.ok ? response.json() : null
      } catch {
        return null
      }
    },
    { timeout: 20_000, interval: 250, label: 'Edge CDP ready' },
  )
  const targets = await (await fetch(`http://127.0.0.1:${cdpPort}/json/list`)).json()
  const pageTarget = targets.find((target) => target.type === 'page')
  if (!pageTarget?.webSocketDebuggerUrl) {
    child.kill()
    throw new Error('未找到 Edge page target')
  }
  const client = await CdpClient.connect(pageTarget.webSocketDebuggerUrl)
  return { child, client, version }
}

function expectedMax(fixture, key) {
  const values = []
  for (const model of fixture.models) {
    if (model.status !== 'ok') continue
    const value = model.metrics ? model.metrics[key] : null
    if (typeof value === 'number' && Number.isFinite(value) && value >= 0) values.push(value)
  }
  for (const derived of fixture.derived || []) {
    if (derived.status !== 'ok') continue
    const value = derived.metrics ? derived.metrics[key] : null
    if (typeof value === 'number' && Number.isFinite(value) && value >= 0) values.push(value)
  }
  return values.length ? Math.max(...values) : 0
}

function expectedValue(fixture, key, reference) {
  const metrics = reference.metrics || {}
  const value = metrics[key]
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

function expectedRows(fixture, key) {
  const rows = []
  for (const model of fixture.models) {
    rows.push({ key: `model:${model.model_id}`, displayName: model.display_name, status: model.status, value: expectedValue(fixture, key, model) })
  }
  for (const derived of fixture.derived || []) {
    rows.push({ key: `derived:${derived.kind}`, displayName: derived.display_name, status: derived.status, value: expectedValue(fixture, key, derived) })
  }
  return rows
}

async function main() {
  await mkdir(artifactDir, { recursive: true })
  await mkdir(join(here, 'env'), { recursive: true })
  await writeFile(join(here, 'env', '.env.local'), `VITE_API_BASE_URL=http://127.0.0.1:${serverPort}\n`)
  const fixture = JSON.parse(await readFile(fixturePath, 'utf8'))

  console.log('=== F11 PK 榜单页验收（mock bench API + Edge headless/CDP）===')
  console.log(`[build] 构建 harness（独立 outDir: ${distDir}）`)
  await build({ configFile: join(here, 'vite.harness.config.mjs'), logLevel: 'warn' })
  console.log('[build] OK')

  const mock = await createMockBenchServer({
    port: serverPort,
    staticDir: distDir,
    fixturePath,
  })
  mock.setScenario = async (name) => {
    const response = await fetch(`http://127.0.0.1:${serverPort}/__scenario?name=${encodeURIComponent(name)}`, {
      method: 'POST',
    })
    if (!response.ok) throw new Error(`scenario 切换失败：${name}`)
  }
  console.log(`[mock] 本地 mock bench API: http://127.0.0.1:${serverPort}`)

  const profileDir = join(os.tmpdir(), `tmf-f11-edge-${process.pid}-${Date.now()}`)
  await mkdir(profileDir, { recursive: true })
  let edge = null
  let client = null
  const summary = []
  let failures = 0

  try {
    console.log(`[edge] 启动 Edge headless（CDP ${cdpPort}）`)
    edge = await launchEdge(profileDir)
    client = edge.client
    console.log(`[edge] ${edge.version.Browser}`)
    await client.send('Network.enable', {
      maxTotalBufferSize: 50_000_000,
      maxResourceBufferSize: 5_000_000,
      maxPostDataSize: 500_000,
    })
    await client.send('Runtime.enable')
    await client.send('Page.enable')

    const network = { requests: [], responses: [] }
    client.on('Network.requestWillBeSent', (params) => {
      network.requests.push({
        ts: Date.now(),
        requestId: params.requestId,
        url: params.request.url,
        method: params.request.method,
      })
    })
    client.on('Network.responseReceived', (params) => {
      network.responses.push({
        ts: Date.now(),
        requestId: params.requestId,
        url: params.response.url,
        status: params.response.status,
        mimeType: params.response.mimeType,
        headers: params.response.headers,
      })
    })

    const scenarios = []

    // ---- S1 ready + 三种指标切换 ----
    scenarios.push({
      id: 'ready',
      name: 'S1 正常：5 模型 + 集成 + oracle，meta 逐项对应，指标切换',
      async run() {
        const checks = buildChecks()
        const mark = network.requests.length
        await mock.setScenario('ready')
        const startedAt = Date.now()
        await gotoHarness(client)
        let dom = await evaluate(client, 'window.__f11.dom()')
        const apiRequests = network.requests
          .slice(mark)
          .filter((item) => item.url.includes('/api/v1/bench/pk'))
        const apiResponses = network.responses.filter(
          (item) => item.url.includes('/api/v1/bench/pk'),
        )
        const response = apiResponses[apiResponses.length - 1]

        checks.check('发出且仅发出 1 次 GET /api/v1/bench/pk', apiRequests.length === 1, `${apiRequests.length}`)
        checks.check(
          '响应 Content-Type 为 application/json',
          Boolean(response && String(response.headers?.['content-type'] || response.mimeType || '').includes('application/json')),
          String(response && (response.headers?.['content-type'] || response.mimeType)),
        )
        checks.check('页面状态 ready', dom.state === 'ready', String(dom.state))

        // meta 逐项对应 JSON
        const metaByField = Object.fromEntries(dom.meta.map((item) => [item.field, item]))
        const expectedMeta = {
          dataset: fixture.meta.dataset,
          split: fixture.meta.split,
          sample_size: String(fixture.meta.sample_size),
          generated_at: fixture.meta.generated_at,
          code_version: fixture.meta.code_version,
          metric_def: JSON.stringify(fixture.meta.metric_def),
          data_kind: fixture.meta.data_kind,
          model_versions: JSON.stringify(fixture.meta.model_versions),
        }
        for (const [field, expected] of Object.entries(expectedMeta)) {
          const actual = metaByField[field]?.raw
          checks.check(`meta.${field} 与 JSON 逐字一致`, actual === expected, `DOM=${actual} JSON=${expected}`)
        }
        checks.check(
          'meta 样本量显示 n = 10000',
          (metaByField.sample_size?.display || '').includes('n = 10000'),
          metaByField.sample_size?.display || '',
        )
        checks.check(
          'meta 数据性质显示离线评估',
          (metaByField.data_kind?.display || '').includes('离线评估'),
          metaByField.data_kind?.display || '',
        )
        checks.check(
          'meta 产物路径可见',
          dom.text.includes(fixture.artifact_path),
          fixture.artifact_path,
        )

        // 行与顺序
        const expectedOrder = expectedRows(fixture, 'macro_f1').map((row) => row.key)
        const actualOrder = dom.rows.map((row) => row.key)
        checks.check(
          '7 行：5 模型 + 集成 + oracle（顺序=产物顺序）',
          JSON.stringify(actualOrder) === JSON.stringify(expectedOrder),
          JSON.stringify(actualOrder),
        )
        checks.check(
          '指标按钮为 F1 / 延迟 / 体积 三项',
          JSON.stringify(dom.metricButtons) ===
            JSON.stringify(['macro_f1', 'avg_latency_ms', 'size_bytes']),
          JSON.stringify(dom.metricButtons),
        )
        checks.check('默认指标为 macro_f1', dom.activeMetric === 'macro_f1', String(dom.activeMetric))

        // F1 数值/条形严格对应
        const f1Max = expectedMax(fixture, 'macro_f1')
        for (const expected of expectedRows(fixture, 'macro_f1')) {
          const row = dom.rows.find((item) => item.key === expected.key)
          if (!row) {
            checks.check(`F1 行 ${expected.key} 存在`, false)
            continue
          }
          if (expected.value === null || expected.status !== 'ok') {
            checks.check(`F1 缺失行 ${expected.key} 不画条`, row.hasBar === false && row.metricValue === '', `${row.metricValue}/${row.hasBar}`)
            continue
          }
          const expectedWidth = (expected.value / f1Max) * 100
          const widthDelta = Math.abs(Number(row.barWidth) - expectedWidth)
          checks.check(
            `F1 ${expected.key} 数值与条形宽度对应`,
            row.metricValue === String(expected.value) && widthDelta <= 0.05,
            `value=${row.metricValue} width=${row.barWidth} expectedWidth=${expectedWidth.toFixed(2)}`,
          )
        }

        // LLM 缺失
        const llmRow = dom.rows.find((row) => row.key === 'model:llm')
        checks.check(
          'LLM 缺失：状态 missing、显示未纳入 + 外部 Provider 未授权、不画条',
          Boolean(
            llmRow &&
              llmRow.status === 'missing' &&
              llmRow.metricValue === '' &&
              llmRow.hasBar === false &&
              llmRow.text.includes('未纳入') &&
              llmRow.text.includes('外部 Provider 未授权'),
          ),
          JSON.stringify(llmRow),
        )

        // 派生项标注
        const ensembleRow = dom.rows.find((row) => row.key === 'derived:weighted_vote')
        const oracleRow = dom.rows.find((row) => row.key === 'derived:oracle')
        checks.check(
          '加权投票集成标注「非独立模型」',
          Boolean(ensembleRow && ensembleRow.tag.includes('非独立模型') && ensembleRow.text.includes('派生指标，非独立模型')),
          JSON.stringify(ensembleRow?.tag),
        )
        checks.check(
          'oracle 标注「参考上界，非独立模型」',
          Boolean(oracleRow && oracleRow.tag.includes('参考上界') && oracleRow.tag.includes('非独立模型')),
          JSON.stringify(oracleRow?.tag),
        )
        checks.check(
          'oracle 参考线存在且为 F1 最大值 100%',
          dom.oracleLine !== null && Number(dom.oracleLine.percent) >= 99.9,
          JSON.stringify(dom.oracleLine),
        )

        // 红线文案
        checks.check(
          '不出现「实时准确率 / 线上准确率」',
          !dom.text.includes('实时准确率') && !dom.text.includes('线上准确率'),
          dom.text.includes('实时准确率') || dom.text.includes('线上准确率') ? '发现禁用词' : '',
        )
        checks.check(
          '明确标注离线口径与「不代表实时」',
          dom.text.includes('离线') && dom.text.includes('不代表实时或线上表现'),
          '',
        )

        // 指标切换：延迟
        await evaluate(client, "window.__f11.clickMetric('avg_latency_ms')")
        await sleep(180)
        dom = await evaluate(client, 'window.__f11.dom()')
        const latencyMax = expectedMax(fixture, 'avg_latency_ms')
        checks.check('切换后激活平均延迟', dom.activeMetric === 'avg_latency_ms', String(dom.activeMetric))
        checks.check(
          '延迟提示为「越小越好」且单位为毫秒',
          dom.metricHint.includes('越小越好') && dom.metricHint.includes('毫秒'),
          dom.metricHint,
        )
        const latencyMismatch = []
        for (const expected of expectedRows(fixture, 'avg_latency_ms')) {
          const row = dom.rows.find((item) => item.key === expected.key)
          if (!row) continue
          if (expected.status !== 'ok') {
            if (row.hasBar || row.metricValue !== '' || !row.missingText.includes('未纳入')) {
              latencyMismatch.push(`${expected.key}: expected missing-model got ${row.metricValue}/${row.hasBar}`)
            }
            continue
          }
          if (expected.value === null) {
            if (row.hasBar || row.metricValue !== '' || !row.missingText.includes('该指标未提供')) {
              latencyMismatch.push(`${expected.key}: expected no-metric got ${row.metricValue}/${row.hasBar}`)
            }
            continue
          }
          const expectedWidth = (expected.value / latencyMax) * 100
          const widthDelta = Math.abs(Number(row.barWidth) - expectedWidth)
          if (row.metricValue !== String(expected.value) || widthDelta > 0.05) {
            latencyMismatch.push(
              `${expected.key}: value=${row.metricValue} width=${row.barWidth} expected=${expectedWidth.toFixed(2)}`,
            )
          }
        }
        checks.check('延迟数值/条形严格对应、无 F1 残留', latencyMismatch.length === 0, latencyMismatch.join('; '))
        checks.check('延迟指标下 oracle 不画假条', dom.oracleLine === null, JSON.stringify(dom.oracleLine))
        await captureScreenshot(client, 'S1-ready-latency')

        // 指标切换：体积
        await evaluate(client, "window.__f11.clickMetric('size_bytes')")
        await sleep(180)
        dom = await evaluate(client, 'window.__f11.dom()')
        const sizeMax = expectedMax(fixture, 'size_bytes')
        checks.check('切换后激活模型体积', dom.activeMetric === 'size_bytes', String(dom.activeMetric))
        checks.check(
          '体积提示包含字节口径说明',
          dom.metricHint.includes('字节') && dom.metricHint.toLowerCase().includes('b'),
          dom.metricHint,
        )
        const sizeMismatch = []
        for (const expected of expectedRows(fixture, 'size_bytes')) {
          const row = dom.rows.find((item) => item.key === expected.key)
          if (!row) continue
          if (expected.value === null || expected.status !== 'ok') {
            if (row.hasBar || row.metricValue !== '') {
              sizeMismatch.push(`${expected.key}: expected missing got ${row.metricValue}`)
            }
            continue
          }
          const expectedWidth = (expected.value / sizeMax) * 100
          const widthDelta = Math.abs(Number(row.barWidth) - expectedWidth)
          if (row.metricValue !== String(expected.value) || widthDelta > 0.05) {
            sizeMismatch.push(
              `${expected.key}: value=${row.metricValue} width=${row.barWidth} expected=${expectedWidth.toFixed(2)}`,
            )
          }
          if (!/B|KiB|MiB|GiB/.test(row.valueText)) {
            sizeMismatch.push(`${expected.key}: 体积格式异常 ${row.valueText}`)
          }
        }
        checks.check('体积数值/条形严格对应、无前两指标残留', sizeMismatch.length === 0, sizeMismatch.join('; '))
        await captureScreenshot(client, 'S1-ready-size')

        // 切回 F1
        await evaluate(client, "window.__f11.clickMetric('macro_f1')")
        await sleep(180)
        dom = await evaluate(client, 'window.__f11.dom()')
        const backMismatch = expectedRows(fixture, 'macro_f1')
          .filter((row) => row.status === 'ok' && row.value !== null)
          .filter((row) => {
            const actual = dom.rows.find((item) => item.key === row.key)
            return !actual || actual.metricValue !== String(row.value)
          })
        checks.check('切回 F1 数值恢复、无体积残留', backMismatch.length === 0, JSON.stringify(backMismatch))
        await captureScreenshot(client, 'S1-ready-macro-f1')

        return {
          checks,
          evidence: {
            scenarioElapsedMs: Date.now() - startedAt,
            scenarios: scenarios.length,
            dom,
            apiRequests,
            apiResponses: apiResponses.map((item) => ({ status: item.status, contentType: item.headers?.['content-type'] || item.mimeType })),
          },
        }
      },
    })

    // ---- S2 包装层兼容 ----
    scenarios.push({
      id: 'wrapped',
      name: 'S2 包装层 {request_id, artifact_path, artifact} 兼容',
      async run() {
        const checks = buildChecks()
        await mock.setScenario('wrapped')
        await gotoHarness(client)
        const dom = await evaluate(client, 'window.__f11.dom()')
        checks.check('页面状态 ready', dom.state === 'ready', String(dom.state))
        checks.check('渲染 7 行', dom.rows.length === 7, String(dom.rows.length))
        checks.check(
          '包装层下 meta.dataset 仍逐字一致',
          (dom.meta.find((item) => item.field === 'dataset')?.raw || '') === fixture.meta.dataset,
          '',
        )
        checks.check(
          '包装层 request_id 显示（前 8 位）',
          dom.text.includes('REQUEST · mock-ben'),
          dom.text.slice(0, 120),
        )
        const requestCount = mock.requests.filter((item) => item.scenario === 'wrapped').length
        checks.check('包装层场景请求 1 次', requestCount === 1, String(requestCount))
        return { checks, evidence: { dom } }
      },
    })

    // ---- S3 空态 ----
    scenarios.push({
      id: 'empty',
      name: 'S3 空产物：AsyncState empty，不显示任何数值',
      async run() {
        const checks = buildChecks()
        await mock.setScenario('empty')
        await gotoHarness(client)
        const dom = await evaluate(client, 'window.__f11.dom()')
        checks.check('页面状态 empty', dom.state === 'empty', String(dom.state))
        checks.check('空态文案正确', dom.emptyText.includes('暂无基准产物'), dom.emptyText)
        checks.check('空态无数据行', dom.rows.length === 0, String(dom.rows.length))
        checks.check('空态无错误态', dom.errorText === '', dom.errorText)
        await captureScreenshot(client, 'S3-empty')
        return { checks, evidence: { dom } }
      },
    })

    // ---- S4 错误态 ----
    scenarios.push({
      id: 'error',
      name: 'S4 接口 500：AsyncState error + 重试，不显示假数据',
      async run() {
        const checks = buildChecks()
        await mock.setScenario('error')
        await gotoHarness(client)
        const dom = await evaluate(client, 'window.__f11.dom()')
        checks.check('页面状态 error', dom.state === 'error', String(dom.state))
        checks.check(
          '错误文案透传服务端信息',
          dom.errorText.includes('benchmark artifact unavailable'),
          dom.errorText,
        )
        checks.check('错误态提供重试按钮', dom.hasRetry === true, String(dom.hasRetry))
        checks.check('错误态无数据行', dom.rows.length === 0, String(dom.rows.length))
        await captureScreenshot(client, 'S4-error')
        return { checks, evidence: { dom } }
      },
    })

    // ---- S5 仅返回路径：必须报错，不得造数 ----
    scenarios.push({
      id: 'path-only',
      name: 'S5 仅返回 artifact_path：错误提示，不伪造指标',
      async run() {
        const checks = buildChecks()
        await mock.setScenario('path-only')
        await gotoHarness(client)
        const dom = await evaluate(client, 'window.__f11.dom()')
        checks.check('页面状态 error', dom.state === 'error', String(dom.state))
        checks.check(
          '提示仅返回产物路径',
          dom.errorText.includes('仅返回产物路径'),
          dom.errorText,
        )
        checks.check('错误信息包含产物路径', dom.errorText.includes(fixture.artifact_path), dom.errorText)
        checks.check('无数据行（不造假）', dom.rows.length === 0, String(dom.rows.length))
        return { checks, evidence: { dom } }
      },
    })

    for (const scenario of scenarios) {
      let result
      let scenarioError = null
      const started = Date.now()
      try {
        result = await scenario.run()
      } catch (error) {
        scenarioError = error
      }
      const checks = result?.checks ?? buildChecks()
      if (scenarioError) {
        checks.check('场景执行未抛异常', false, scenarioError.message)
      }
      const failed = checks.checks.filter((check) => !check.pass)
      failures += failed.length
      const pass = !scenarioError && failed.length === 0
      summary.push({
        id: scenario.id,
        name: scenario.name,
        pass,
        checks: checks.checks,
        elapsedMs: Date.now() - started,
        evidence: result?.evidence ?? null,
      })
      console.log(`\n[${pass ? 'PASS' : 'FAIL'}] ${scenario.name}`)
      for (const check of checks.checks) {
        console.log(`  ${check.pass ? '✓' : '✗'} ${check.name}${check.detail ? ` — ${check.detail}` : ''}`)
      }
      if (scenarioError) console.log(`  ! 异常: ${scenarioError.stack || scenarioError.message}`)
    }

    const report = {
      generatedAt: new Date().toISOString(),
      browser: edge.version.Browser,
      mode: 'mock bench API + Edge headless/CDP',
      fixture: fixturePath,
      serverPort,
      exitCode: failures === 0 ? 0 : 1,
      summary: summary.map((item) => ({
        id: item.id,
        name: item.name,
        pass: item.pass,
        elapsedMs: item.elapsedMs,
        checks: item.checks,
      })),
      evidence: Object.fromEntries(summary.map((item) => [item.id, item.evidence])),
    }
    const reportPath = join(artifactDir, 'f11-report.json')
    await writeFile(reportPath, JSON.stringify(report, null, 2))
    console.log(`\n[artifact] ${reportPath}`)

    console.log('\n=== 结果汇总 ===')
    for (const item of summary) {
      const passed = item.checks.filter((check) => check.pass).length
      console.log(
        `${item.pass ? 'PASS' : 'FAIL'}  ${item.id.padEnd(10)} ${passed}/${item.checks.length}  ${item.name}`,
      )
    }
    console.log(`\n总结果：${failures === 0 ? 'PASS' : 'FAIL'}（失败断言 ${failures} 条）`)
    console.log(`退出码：${failures === 0 ? 0 : 1}`)
  } finally {
    client?.close()
    if (edge) {
      try {
        edge.child.kill()
      } catch {
        // ignore
      }
    }
    await mock.close()
    await sleep(300)
    await rm(profileDir, { recursive: true, force: true }).catch(() => {})
  }

  process.exitCode = failures === 0 ? 0 : 1
}

main().catch((error) => {
  console.error('[F11] 运行失败：', error.stack || error.message)
  process.exitCode = 1
})
