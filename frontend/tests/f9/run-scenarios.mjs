/**
 * F9 验收运行器：本地 mock SSE server + Edge headless（CDP）。
 *
 * 用法（在 frontend 目录下）：
 *   node tests/f9/run-scenarios.mjs
 *
 * 做四件事：
 * 1) 用独立 Vite 配置构建 tests/f9/harness.html（产物在 tests/f9/dist，不碰 frontend/dist）；
 * 2) 启动本地 mock SSE server（127.0.0.1:18080）；
 * 3) 启动 Edge headless，通过 CDP 驱动真实浏览器并收集 Network/EventSource 日志；
 * 4) 跑六组场景（四类验收 + 两个附加错误路径），输出 PASS/FAIL、证据 JSON、截图，并以 0/1 退出。
 *
 * 说明：这里全部是 mock 数据（mock-server.mjs 显式构造），不访问真实 worker / 真实 LLM。
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
const artifactDir = join(here, 'artifacts')
const distDir = join(here, 'dist')
const SERVER_PORT = 18080
const CDP_PORT = 9334
const HARNESS_URL = `http://127.0.0.1:${SERVER_PORT}/tests/f9/harness.html`

const EDGE_CANDIDATES = [
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
  await client.send('Page.navigate', { url: HARNESS_URL })
  await waitFor(
    async () => {
      const state = await evaluate(
        client,
        'document.readyState === "complete" && Boolean(window.__f9 && window.__f9.ready)',
      )
      return state === true
    },
    { timeout: 20_000, interval: 100, label: 'harness ready' },
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

function networkSlice(network, mark) {
  return {
    requests: network.requests.slice(mark.requests).filter((item) => item.url.includes('/api/v1/')),
    responses: network.responses.slice(mark.responses).filter((item) => item.url.includes('/api/v1/')),
    sse: network.sse.slice(mark.sse),
    failed: network.failed.slice(mark.failed),
  }
}

function streamRequestsOf(evidence) {
  return evidence.requests.filter((item) => item.url.includes('/compare/stream'))
}

function postCompareRequestsOf(evidence) {
  return evidence.requests.filter(
    (item) =>
      item.method === 'POST' &&
      item.url.includes('/api/v1/compare') &&
      !item.url.includes('/compare/stream'),
  )
}

function contentTypesOf(evidence) {
  return evidence.responses.map((response) => {
    const headers = response.headers || {}
    return {
      url: response.url,
      status: response.status,
      contentType: headers['content-type'] || headers['Content-Type'] || response.mimeType || '',
    }
  })
}

function serverOrder(serverStreamEvents) {
  return serverStreamEvents
    .filter((event) => event.event === 'model')
    .map((event) => event.model_id)
}

function browserOrder(evidence) {
  return evidence.sse
    .filter((event) => event.eventName === 'model')
    .map((event) => {
      try {
        return JSON.parse(event.data).model_id
      } catch {
        // 无效 JSON 的 model 帧会被前端丢弃，不计入「有效事件顺序」。
        return null
      }
    })
    .filter((modelId) => modelId !== null)
}

function baseEvidence(context) {
  const { network, server, mark, scenarioStartedAt } = context
  const evidence = networkSlice(network, mark.network)
  const serverRequests = server.requests.slice(mark.serverRequests)
  const serverStreamEvents = server.streamEvents.slice(mark.serverStreamEvents)
  return {
    scenarioStartedAt,
    network: evidence,
    contentTypes: contentTypesOf(evidence),
    serverRequests,
    serverStreamEvents,
    streamRequest: serverRequests.find((item) => item.kind === 'stream') || null,
    postRequest: serverRequests.find((item) => item.kind === 'compare') || null,
  }
}

async function startScenario(client, title, models) {
  await gotoHarness(client)
  const startedAt = Date.now()
  await evaluate(client, `window.__f9.start(${JSON.stringify(title)}, ${JSON.stringify(models)})`)
  return startedAt
}

async function waitTerminalSnapshot(client, timeout = 12_000) {
  return waitFor(
    async () => {
      const snapshot = await evaluate(client, 'window.__f9.snapshot()')
      if (!snapshot) return null
      if (snapshot.status === 'done' || snapshot.status === 'error') return snapshot
      // fallback 状态先于 POST 结果出现：需等待回退结果落位（或回退失败）。
      if (
        snapshot.status === 'fallback' &&
        (Object.keys(snapshot.arrived || {}).length > 0 || snapshot.error)
      ) {
        return snapshot
      }
      return null
    },
    { timeout, interval: 80, label: 'terminal stream status' },
  )
}

const SCENARIOS = [
  {
    id: 'normal',
    name: 'S1 正常：多模型按 SSE 逐条落位（含心跳/无效 JSON/重复 model）',
    async run(client, context) {
      const startedAt = await startScenario(client, 'f9-normal', [
        'rf',
        'fasttext',
        'bert',
        'distill',
        'llm',
      ])
      // 首个事件到达后立刻截图：用于证明「首个事件前/LLM 未到」的骨架与等待中状态。
      await waitFor(
        async () => {
          const snapshot = await evaluate(client, 'window.__f9.snapshot()')
          return snapshot && Object.keys(snapshot.arrived || {}).length >= 1 ? snapshot : null
        },
        { timeout: 5000, interval: 30, label: 'first model event (normal)' },
      )
      await captureScreenshot(client, 'S1-normal-waiting')
      const snapshot = await waitTerminalSnapshot(client)
      await sleep(200)
      const dom = await evaluate(client, 'window.__f9.dom()')
      const evidence = baseEvidence({ ...context, scenarioStartedAt: startedAt })
      const { check } = context.checks
      const streams = streamRequestsOf(evidence.network)
      const posts = postCompareRequestsOf(evidence.network)

      check('只发起 1 条 SSE GET /compare/stream', streams.length === 1, `实际 ${streams.length}`)
      check('未发起回退 POST /compare', posts.length === 0, `实际 ${posts.length}`)
      check(
        'SSE 请求带 title/models query',
        streams.length > 0 &&
          streams[0].url.includes('title=f9-normal') &&
          streams[0].url.includes('models=rf,fasttext,bert,distill,llm'),
        streams[0]?.url || '',
      )
      const streamContentType =
        evidence.contentTypes.find((item) => item.url.includes('/compare/stream'))?.contentType || ''
      check(
        '响应 Content-Type 为 text/event-stream',
        streamContentType.includes('text/event-stream'),
        streamContentType,
      )
      check('summary.status=ok', snapshot.summary?.status === 'ok', JSON.stringify(snapshot.summary))
      check(
        'summary 计数 5/5/0',
        snapshot.summary?.total === 5 && snapshot.summary?.ok === 5 && snapshot.summary?.failed === 0,
        JSON.stringify(snapshot.summary),
      )
      check('done.status=ok', snapshot.done?.status === 'ok', JSON.stringify(snapshot.done))
      check(
        '5 个模型全部落位（model 事件各一次）',
        Object.keys(snapshot.arrived).length === 5,
        JSON.stringify(Object.keys(snapshot.arrived)),
      )
      check(
        '到达顺序 = 服务端完成顺序',
        JSON.stringify(snapshot.arrivalOrder) ===
          JSON.stringify(['rf', 'fasttext', 'bert', 'distill', 'llm']),
        JSON.stringify(snapshot.arrivalOrder),
      )
      check(
        '重复 model_id 被去重（rf 延迟保持 90ms，未被 999ms 覆盖）',
        snapshot.arrived.rf?.latency_ms === 90,
        JSON.stringify(snapshot.arrived.rf),
      )
      check(
        '无效 JSON 被丢弃且未触发回退',
        snapshot.fallback === false && Object.keys(snapshot.arrived).length === 5,
        `fallback=${snapshot.fallback}`,
      )
      check(
        '首事件 ≤ 1000ms',
        typeof snapshot.firstEventLatencyMs === 'number' && snapshot.firstEventLatencyMs < 1000,
        `${snapshot.firstEventLatencyMs} ms`,
      )
      check(
        '浏览器侧事件顺序 = 服务端 emit 顺序',
        JSON.stringify(browserOrder(evidence.network)) ===
          JSON.stringify(serverOrder(evidence.serverStreamEvents)),
        `browser=${JSON.stringify(browserOrder(evidence.network))} server=${JSON.stringify(serverOrder(evidence.serverStreamEvents))}`,
      )
      check('DOM 渲染 5 张模型卡片', dom.cards.length === 5, `${dom.cards.length}`)
      check('DOM 概览显示「全部模型已完成」', dom.overview.includes('全部模型已完成'), dom.overview)
      check(
        'DOM 显示流式徽标',
        dom.overview.includes('流式实时（SSE）'),
        dom.overview,
      )
      check(
        '分歧高亮：distill 与多数不同',
        dom.cards.some((card) => card.modelId === 'distill' && card.consistency === 'differ') &&
          dom.divergence === true,
        JSON.stringify(dom.cards.find((card) => card.modelId === 'distill')),
      )
      check(
        '「与多数一致」卡片存在',
        dom.cards.some((card) => card.consistency === 'match'),
        JSON.stringify(dom.cards.map((card) => `${card.modelId}:${card.consistency}`)),
      )

      await evaluate(client, "window.__f9.clickSort('consensus')")
      await sleep(120)
      const consensusDom = await evaluate(client, 'window.__f9.dom()')
      const consensusOrder = consensusDom.cards.map((card) => card.modelId)
      check(
        '按一致度排序：分歧项后置',
        consensusOrder.indexOf('distill') >
          Math.max(
            consensusOrder.indexOf('rf'),
            consensusOrder.indexOf('fasttext'),
            consensusOrder.indexOf('bert'),
            consensusOrder.indexOf('llm'),
          ),
        JSON.stringify(consensusOrder),
      )
      check(
        '一致度排序按钮激活',
        consensusDom.sortActive === 'consensus',
        String(consensusDom.sortActive),
      )

      await evaluate(client, "window.__f9.clickSort('latency')")
      await sleep(120)
      const latencyDom = await evaluate(client, 'window.__f9.dom()')
      const latencyOrder = latencyDom.cards.map((card) => card.modelId)
      check(
        '按延迟排序升序（90/110/220/300/1400）',
        JSON.stringify(latencyOrder) ===
          JSON.stringify(['rf', 'fasttext', 'bert', 'distill', 'llm']),
        JSON.stringify(latencyOrder),
      )
      await captureScreenshot(client, 'S1-normal')

      return { snapshot, dom, evidence }
    },
  },
  {
    id: 'llm-offline',
    name: 'S2 LLM 离线：单模型 error + 其余成功（summary=partial）',
    async run(client, context) {
      const startedAt = await startScenario(client, 'f9-llm-offline', [
        'rf',
        'fasttext',
        'bert',
        'distill',
        'llm',
      ])
      const snapshot = await waitTerminalSnapshot(client)
      await sleep(200)
      const dom = await evaluate(client, 'window.__f9.dom()')
      const evidence = baseEvidence({ ...context, scenarioStartedAt: startedAt })
      const { check } = context.checks
      const streams = streamRequestsOf(evidence.network)
      const posts = postCompareRequestsOf(evidence.network)

      check('只发起 1 条 SSE GET', streams.length === 1, `实际 ${streams.length}`)
      check('未回退 POST（LLM 单项失败不应整页回退）', posts.length === 0, `实际 ${posts.length}`)
      check('summary.status=partial', snapshot.summary?.status === 'partial', JSON.stringify(snapshot.summary))
      check(
        'summary 计数 5/4/1',
        snapshot.summary?.total === 5 && snapshot.summary?.ok === 4 && snapshot.summary?.failed === 1,
        JSON.stringify(snapshot.summary),
      )
      check('done.status=partial', snapshot.done?.status === 'partial', JSON.stringify(snapshot.done))
      check(
        'LLM 项 status=error 且错误码 WORKER_UNAVAILABLE',
        snapshot.arrived.llm?.status === 'error' && snapshot.arrived.llm?.error?.code === 'WORKER_UNAVAILABLE',
        JSON.stringify(snapshot.arrived.llm),
      )
      check(
        '其余 4 个模型保持成功（未被 error 覆盖）',
        ['rf', 'fasttext', 'bert', 'distill'].every((id) => snapshot.arrived[id]?.status === 'ok'),
        JSON.stringify(
          Object.fromEntries(
            ['rf', 'fasttext', 'bert', 'distill'].map((id) => [id, snapshot.arrived[id]?.status]),
          ),
        ),
      )
      check(
        'DOM 概览显示「部分结果已返回」',
        dom.overview.includes('部分结果已返回'),
        dom.overview,
      )
      const llmCard = dom.cards.find((card) => card.modelId === 'llm')
      check(
        'DOM LLM 卡片显示错误信息且不污染其他卡片',
        llmCard?.status === 'error' && llmCard.text.includes('LLM worker 不可达'),
        JSON.stringify(llmCard),
      )
      check(
        'DOM 其他 4 张卡片正常',
        dom.cards.filter((card) => card.status === 'ok').length === 4,
        JSON.stringify(dom.cards.map((card) => `${card.modelId}:${card.status}`)),
      )
      await captureScreenshot(client, 'S2-llm-offline')
      return { snapshot, dom, evidence }
    },
  },
  {
    id: 'all-failed',
    name: 'S2b 全失败：summary=all_failed 的完整/部分/全失败第三态',
    async run(client, context) {
      const startedAt = await startScenario(client, 'f9-all-failed', ['rf', 'bert'])
      const snapshot = await waitTerminalSnapshot(client)
      await sleep(200)
      const dom = await evaluate(client, 'window.__f9.dom()')
      const evidence = baseEvidence({ ...context, scenarioStartedAt: startedAt })
      const { check } = context.checks

      check('summary.status=all_failed', snapshot.summary?.status === 'all_failed', JSON.stringify(snapshot.summary))
      check(
        'summary 计数 2/0/2',
        snapshot.summary?.total === 2 && snapshot.summary?.ok === 0 && snapshot.summary?.failed === 2,
        JSON.stringify(snapshot.summary),
      )
      check('done.status=all_failed', snapshot.done?.status === 'all_failed', JSON.stringify(snapshot.done))
      check(
        '两个模型均为 error 且错误码不同',
        snapshot.arrived.rf?.error?.code === 'WORKER_TIMEOUT' &&
          snapshot.arrived.bert?.error?.code === 'WORKER_UNAVAILABLE',
        JSON.stringify({ rf: snapshot.arrived.rf?.error, bert: snapshot.arrived.bert?.error }),
      )
      check(
        'DOM 概览显示「模型暂时无法完成判断」',
        dom.overview.includes('模型暂时无法完成判断'),
        dom.overview,
      )
      check('无分歧徽标（没有成功预测）', dom.divergence === false, String(dom.divergence))
      await captureScreenshot(client, 'S2b-all-failed')
      return { snapshot, dom, evidence }
    },
  },
  {
    id: 'switch',
    name: 'S3 中途切面板：连接关闭、无后续更新、无重连',
    async run(client, context) {
      const startedAt = await startScenario(client, 'f9-switch', [
        'rf',
        'fasttext',
        'bert',
        'distill',
        'llm',
      ])
      await waitFor(
        async () => {
          const snapshot = await evaluate(client, 'window.__f9.snapshot()')
          return snapshot && Object.keys(snapshot.arrived).length >= 1 ? snapshot : null
        },
        { timeout: 8000, interval: 60, label: 'first model event' },
      )
      await evaluate(client, "window.__f9.switchMode('single')")
      const switchedAt = Date.now()
      const snapshotAtSwitch = await evaluate(client, 'window.__f9.snapshot()')
      await sleep(2200)
      const snapshotAfter = await evaluate(client, 'window.__f9.snapshot()')
      const domAfter = await evaluate(client, 'window.__f9.dom()')
      const evidence = baseEvidence({ ...context, scenarioStartedAt: startedAt })
      const { check } = context.checks
      const streamRequest = evidence.streamRequest
      const streams = streamRequestsOf(evidence.network)
      const posts = postCompareRequestsOf(evidence.network)

      check(
        '切换时已收到首个 model 事件',
        Object.keys(snapshotAtSwitch.arrived).length === 1,
        JSON.stringify(Object.keys(snapshotAtSwitch.arrived)),
      )
      check(
        '切换后不再收到后续 model（2.2s 内状态冻结）',
        Object.keys(snapshotAfter.arrived).length === 1,
        `切换前=${Object.keys(snapshotAtSwitch.arrived).length} 切换后=${Object.keys(snapshotAfter.arrived).length}`,
      )
      check('切换后状态回到 idle', snapshotAfter.status === 'idle', String(snapshotAfter.status))
      check('结果区已隐藏（无残留面板）', domAfter.resultMode === null, String(domAfter.resultMode))
      check(
        '服务端检测到客户端关闭连接',
        Boolean(streamRequest && streamRequest.closedAt),
        JSON.stringify(streamRequest && { openedAt: streamRequest.openedAt, closedAt: streamRequest.closedAt }),
      )
      const afterClose = evidence.serverStreamEvents.filter(
        (event) => streamRequest && streamRequest.closedAt && event.ts > streamRequest.closedAt,
      )
      check(
        '连接关闭后服务端未再 push 任何事件',
        afterClose.length === 0,
        JSON.stringify(afterClose),
      )
      check(
        '关闭发生在切换后 1.5s 内（未等待慢模型）',
        Boolean(
          streamRequest?.closedAt && streamRequest.closedAt - switchedAt < 1500,
        ),
        streamRequest ? `${streamRequest.closedAt - switchedAt} ms` : 'no stream request',
      )
      check('只有 1 条 SSE 连接（未自动重连）', streams.length === 1, `实际 ${streams.length}`)
      check('未发起回退 POST', posts.length === 0, `实际 ${posts.length}`)
      await captureScreenshot(client, 'S3-switch')
      return { snapshotAfter, domAfter, evidence }
    },
  },
  {
    id: 'fallback',
    name: 'S4 断网/无 SSE：3s 内无有效首事件 → 关闭 SSE 并回退 POST /compare',
    async run(client, context) {
      const startedAt = await startScenario(client, 'f9-fallback', [
        'rf',
        'fasttext',
        'bert',
        'distill',
        'llm',
      ])
      const snapshot = await waitTerminalSnapshot(client, 10_000)
      await sleep(200)
      const dom = await evaluate(client, 'window.__f9.dom()')
      const evidence = baseEvidence({ ...context, scenarioStartedAt: startedAt })
      const { check } = context.checks
      const streams = streamRequestsOf(evidence.network)
      const posts = postCompareRequestsOf(evidence.network)
      const streamRequest = evidence.streamRequest
      const postRequest = evidence.postRequest
      const firstEventDelay =
        snapshot.firstEventAt !== null && snapshot.startedAt !== null
          ? snapshot.firstEventAt - snapshot.startedAt
          : null
      const streamLifetime =
        streamRequest?.closedAt && streamRequest?.openedAt
          ? streamRequest.closedAt - streamRequest.openedAt
          : null

      check('fallback 标记为 true', snapshot.fallback === true, String(snapshot.fallback))
      check(
        '最终状态 fallback 且结果完整',
        snapshot.status === 'fallback' && Object.keys(snapshot.arrived).length === 5,
        `status=${snapshot.status} arrived=${Object.keys(snapshot.arrived).length}`,
      )
      check(
        '首事件来自 POST 回退，耗时 ≥ 3s',
        firstEventDelay !== null && firstEventDelay >= 2800 && firstEventDelay <= 5000,
        `${firstEventDelay} ms`,
      )
      check('只发起 1 条 SSE GET', streams.length === 1, `实际 ${streams.length}`)
      check(
        '只发起 1 次 POST /compare（无重复回退）',
        posts.length === 1,
        `实际 ${posts.length}: ${JSON.stringify(posts.map((item) => item.url))}`,
      )
      check(
        'SSE 约在 3s 被前端主动关闭',
        streamLifetime !== null && streamLifetime >= 2800 && streamLifetime <= 5000,
        `${streamLifetime} ms`,
      )
      check(
        'POST 在 SSE 关闭之后（客户端先 close 再 POST；服务端 close 日志允许 ±200ms 竞态）',
        Boolean(
          postRequest &&
            streamRequest?.closedAt &&
            postRequest.ts >= streamRequest.closedAt - 200,
        ),
        postRequest && streamRequest?.closedAt
          ? `post.ts=${postRequest.ts} stream.closedAt=${streamRequest.closedAt}`
          : 'missing request log',
      )
      check(
        '流内没有任何 model 事件（SSE 结果未混入 POST 结果）',
        evidence.serverStreamEvents.filter((event) => event.event === 'model').length === 0,
        JSON.stringify(evidence.serverStreamEvents.map((event) => event.event)),
      )
      check(
        'DOM 显示「已回退完整请求（POST）」',
        dom.overview.includes('已回退完整请求（POST）'),
        dom.overview,
      )
      check('DOM 概览显示「全部模型已完成」', dom.overview.includes('全部模型已完成'), dom.overview)
      check('DOM 渲染 5 张卡片', dom.cards.length === 5, `${dom.cards.length}`)
      await captureScreenshot(client, 'S4-fallback')
      return { snapshot, dom, evidence }
    },
  },
  {
    id: 'stream-error',
    name: 'S5 附加：流级 event:error → 面板错误态、不回退 POST',
    async run(client, context) {
      const startedAt = await startScenario(client, 'f9-stream-error', [
        'rf',
        'fasttext',
        'bert',
        'distill',
        'llm',
      ])
      const snapshot = await waitTerminalSnapshot(client)
      await sleep(200)
      const dom = await evaluate(client, 'window.__f9.dom()')
      const evidence = baseEvidence({ ...context, scenarioStartedAt: startedAt })
      const { check } = context.checks
      const posts = postCompareRequestsOf(evidence.network)

      check('status=error', snapshot.status === 'error', String(snapshot.status))
      check(
        '记录流级错误码 INTERNAL_ERROR',
        snapshot.streamError?.code === 'INTERNAL_ERROR',
        JSON.stringify(snapshot.streamError),
      )
      check(
        '已到达的 rf 结果保留（不被错误清空）',
        Object.keys(snapshot.arrived).length === 1 && snapshot.arrived.rf?.status === 'ok',
        JSON.stringify(Object.keys(snapshot.arrived)),
      )
      check('流级错误不触发 POST 回退', posts.length === 0, `实际 ${posts.length}`)
      check(
        'DOM 呈现错误态（AsyncState）',
        dom.text.includes('INTERNAL_ERROR') || dom.text.includes('模型服务暂时不可用') || dom.text.includes('对比'),
        dom.text.slice(0, 160),
      )
      await captureScreenshot(client, 'S5-stream-error')
      return { snapshot, dom, evidence }
    },
  },
  {
    id: 'connect-error',
    name: 'S6 附加：SSE 连接失败（503）→ 立即回退 POST /compare',
    async run(client, context) {
      const startedAt = await startScenario(client, 'f9-connect-error', ['rf', 'fasttext', 'bert'])
      const snapshot = await waitTerminalSnapshot(client)
      await sleep(200)
      const dom = await evaluate(client, 'window.__f9.dom()')
      const evidence = baseEvidence({ ...context, scenarioStartedAt: startedAt })
      const { check } = context.checks
      const streams = streamRequestsOf(evidence.network)
      const posts = postCompareRequestsOf(evidence.network)
      const streamResponse = evidence.network.responses.find((item) => item.url.includes('/compare/stream'))
      const fallbackDelay =
        snapshot.finishedAt !== null && snapshot.startedAt !== null
          ? snapshot.finishedAt - snapshot.startedAt
          : null

      check('SSE 端点返回 503', streamResponse?.status === 503, String(streamResponse?.status))
      check('连接失败后 fallback=true', snapshot.fallback === true, String(snapshot.fallback))
      check(
        '立即回退（<3s，不等首事件超时）',
        fallbackDelay !== null && fallbackDelay < 3000,
        `${fallbackDelay} ms`,
      )
      check('1 次 SSE GET + 1 次 POST', streams.length === 1 && posts.length === 1, `${streams.length}/${posts.length}`)
      check(
        '回退结果覆盖 3 个请求模型',
        Object.keys(snapshot.arrived).length === 3,
        JSON.stringify(Object.keys(snapshot.arrived)),
      )
      check(
        'DOM 显示回退徽标',
        dom.overview.includes('已回退完整请求（POST）'),
        dom.overview,
      )
      await captureScreenshot(client, 'S6-connect-error')
      return { snapshot, dom, evidence }
    },
  },
]

function findEdgePath() {
  return EDGE_CANDIDATES.find((candidate) => existsSync(candidate)) || null
}

async function launchEdge(profileDir) {
  const edgePath = findEdgePath()
  if (!edgePath) throw new Error('未找到 Microsoft Edge（msedge.exe）')
  const args = [
    '--headless=new',
    '--disable-gpu',
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-extensions',
    '--disable-background-networking',
    '--disable-sync',
    `--remote-debugging-port=${CDP_PORT}`,
    `--user-data-dir=${profileDir}`,
    '--window-size=1440,1600',
    'about:blank',
  ]
  const child = spawn(edgePath, args, { stdio: 'ignore', windowsHide: true })
  const version = await waitFor(
    async () => {
      try {
        const response = await fetch(`http://127.0.0.1:${CDP_PORT}/json/version`)
        return response.ok ? response.json() : null
      } catch {
        return null
      }
    },
    { timeout: 20_000, interval: 250, label: 'Edge CDP ready' },
  )
  const targets = await (await fetch(`http://127.0.0.1:${CDP_PORT}/json/list`)).json()
  const pageTarget = targets.find((target) => target.type === 'page')
  if (!pageTarget?.webSocketDebuggerUrl) {
    child.kill()
    throw new Error('未找到 Edge page target')
  }
  const client = await CdpClient.connect(pageTarget.webSocketDebuggerUrl)
  return { child, client, version }
}

async function main() {
  await mkdir(artifactDir, { recursive: true })
  // envDir 指向 tests/f9/env：每次运行写入本次 mock server 地址（该文件被 .gitignore 忽略）。
  await mkdir(join(here, 'env'), { recursive: true })
  await writeFile(
    join(here, 'env', '.env.local'),
    `VITE_API_BASE_URL=http://127.0.0.1:${SERVER_PORT}\n`,
  )

  console.log('=== F9 流式对比面板验收（mock SSE + Edge headless/CDP）===')
  console.log(`[build] 构建 harness（独立 outDir: ${distDir}）`)
  await build({
    configFile: join(here, 'vite.harness.config.mjs'),
    logLevel: 'warn',
  })
  console.log('[build] OK')

  const mock = await createMockServer({ port: SERVER_PORT, staticDir: distDir })
  console.log(`[mock] 本地 mock SSE server: http://127.0.0.1:${SERVER_PORT}`)

  const profileDir = join(os.tmpdir(), `tmf-f9-edge-${process.pid}-${Date.now()}`)
  await mkdir(profileDir, { recursive: true })
  let edge = null
  let client = null
  const summary = []
  let failures = 0

  try {
    console.log(`[edge] 启动 Edge headless（CDP ${CDP_PORT}）`)
    edge = await launchEdge(profileDir)
    client = edge.client
    console.log(`[edge] ${edge.version.Browser}`)

    await client.send('Network.enable', {
      maxTotalBufferSize: 100_000_000,
      maxResourceBufferSize: 10_000_000,
      maxPostDataSize: 1_000_000,
    })
    await client.send('Runtime.enable')
    await client.send('Page.enable')

    const network = { requests: [], responses: [], sse: [], failed: [] }
    client.on('Network.requestWillBeSent', (params) => {
      network.requests.push({
        ts: Date.now(),
        requestId: params.requestId,
        url: params.request.url,
        method: params.request.method,
        type: params.type,
        headers: params.request.headers,
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
    client.on('Network.eventSourceMessageReceived', (params) => {
      network.sse.push({
        ts: Date.now(),
        requestId: params.requestId,
        eventName: params.eventName,
        data: params.data,
      })
    })
    client.on('Network.loadingFailed', (params) => {
      network.failed.push({
        ts: Date.now(),
        requestId: params.requestId,
        errorText: params.errorText,
        canceled: params.canceled,
      })
    })

    for (const scenario of SCENARIOS) {
      const checks = buildChecks()
      const mark = {
        network: {
          requests: network.requests.length,
          responses: network.responses.length,
          sse: network.sse.length,
          failed: network.failed.length,
        },
        serverRequests: mock.requests.length,
        serverStreamEvents: mock.streamEvents.length,
      }
      const context = { checks, network, server: mock, mark }
      let evidence = null
      let scenarioError = null
      const scenarioStartedAt = Date.now()
      try {
        const result = await scenario.run(client, context)
        evidence = {
          ...result.evidence,
          snapshot: result.snapshot ?? result.snapshotAfter ?? null,
          dom: result.dom ?? result.domAfter ?? null,
        }
      } catch (error) {
        scenarioError = error
        checks.check('场景执行未抛异常', false, error.message)
      }
      const failedChecks = checks.checks.filter((check) => !check.pass)
      failures += failedChecks.length
      const pass = !scenarioError && failedChecks.length === 0
      summary.push({
        id: scenario.id,
        name: scenario.name,
        pass,
        checks: checks.checks,
        elapsedMs: Date.now() - scenarioStartedAt,
      })
      console.log(`\n[${pass ? 'PASS' : 'FAIL'}] ${scenario.name}`)
      for (const check of checks.checks) {
        console.log(`  ${check.pass ? '✓' : '✗'} ${check.name}${check.detail ? ` — ${check.detail}` : ''}`)
      }
      if (scenarioError) console.log(`  ! 异常: ${scenarioError.stack || scenarioError.message}`)
      summary[summary.length - 1].evidence = evidence
    }

    const report = {
      generatedAt: new Date().toISOString(),
      browser: edge.version.Browser,
      mode: 'mock-sse + Edge headless/CDP',
      serverPort: SERVER_PORT,
      exitCode: failures === 0 ? 0 : 1,
      summary: summary.map((item) => ({
        id: item.id,
        name: item.name,
        pass: item.pass,
        checks: item.checks,
        elapsedMs: item.elapsedMs,
      })),
      evidence: Object.fromEntries(summary.map((item) => [item.id, item.evidence])),
    }
    const reportPath = join(artifactDir, 'f9-report.json')
    await writeFile(reportPath, JSON.stringify(report, null, 2))
    console.log(`\n[artifact] ${reportPath}`)

    console.log('\n=== 结果汇总 ===')
    for (const item of summary) {
      const passed = item.checks.filter((check) => check.pass).length
      console.log(`${item.pass ? 'PASS' : 'FAIL'}  ${item.id.padEnd(14)} ${passed}/${item.checks.length}  ${item.name}`)
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

main().catch(async (error) => {
  console.error('[F9] 运行失败：', error.stack || error.message)
  process.exitCode = 1
})
