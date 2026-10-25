/**
 * F10 竞技场 UI 验收运行器：本地 mock B7 API + Edge headless（CDP）。
 *
 * 用法（在 frontend 目录下）：
 *   node tests/f10/run-scenarios.mjs
 *
 * 流程：
 * 1) 用独立 Vite 配置构建 tests/f10/harness.html（产物在 tests/f10/dist，不碰 frontend/dist）；
 * 2) 启动本地 mock 竞技场 API server（127.0.0.1:18181，显式 mock）；
 * 3) 启动 Edge headless，通过 CDP 驱动真实浏览器并采集 Network 日志；
 * 4) 跑 10 组场景，输出 PASS/FAIL、证据 JSON、截图，并以 0/1 退出。
 *
 * 全部请求都指向本地 mock（不访问真实网关 / worker / 真实 LLM）。
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
const envDir = join(here, 'env')
const SERVER_PORT = 18181
const CDP_PORT = 9335
const HARNESS_URL = `http://127.0.0.1:${SERVER_PORT}/tests/f10/harness.html`

const MODEL_IDS = ['rf', 'fasttext', 'bert', 'distill', 'llm']
const DISPLAY_NAMES = ['随机森林', 'fastText', 'BERT', '蒸馏模型', 'LLM']

const EDGE_CANDIDATES = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
]

const sleep = (ms) => new Promise((resolvePromise) => setTimeout(resolvePromise, ms))

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
    return () => listeners.delete(listener)
  }

  once(method, timeoutMs = 15_000) {
    return new Promise((resolvePromise, reject) => {
      let unsubscribe = () => {}
      const timer = setTimeout(() => {
        unsubscribe()
        reject(new Error(`CDP event timeout: ${method}`))
      }, timeoutMs)
      unsubscribe = this.on(method, (params) => {
        clearTimeout(timer)
        unsubscribe()
        resolvePromise(params)
      })
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
    const description =
      response.exceptionDetails.exception?.description ||
      response.exceptionDetails.text ||
      'evaluate failed'
    throw new Error(description)
  }
  return response.result?.value
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

/** 检测自由文本中是否泄露模型身份（id 按词边界匹配，避免误伤普通英文）。 */
function identityLeaks(text) {
  if (!text) return []
  const found = new Set()
  for (const modelId of MODEL_IDS) {
    const pattern = new RegExp(`(^|[^a-z0-9])${modelId}([^a-z0-9]|$)`, 'i')
    if (pattern.test(text)) found.add(modelId)
  }
  for (const name of DISPLAY_NAMES) {
    if (text.includes(name)) found.add(name)
  }
  return [...found]
}

/** 把 getComputedStyle 的时长（"0.2s" / "0.01ms" / "1e-05s"）换算成毫秒。 */
function durationMs(value) {
  if (typeof value !== 'string' || !value) return Number.NaN
  if (value.endsWith('ms')) return Number.parseFloat(value.slice(0, -2))
  if (value.endsWith('s')) return Number.parseFloat(value.slice(0, -1)) * 1000
  return Number.parseFloat(value)
}

function parseRgb(value) {
  if (typeof value !== 'string') return null
  const match = value.match(/rgba?\(([^)]+)\)/i)
  if (!match) return null
  const parts = match[1]
    .split(/[,\s/]+/)
    .filter(Boolean)
    .map((part) => Number.parseFloat(part))
  if (parts.length < 3 || parts.slice(0, 3).some((part) => !Number.isFinite(part))) return null
  return {
    r: parts[0],
    g: parts[1],
    b: parts[2],
    a: parts.length > 3 && Number.isFinite(parts[3]) ? parts[3] : 1,
  }
}

function relativeLuminance({ r, g, b }) {
  const channel = (value) => {
    const normalized = value / 255
    return normalized <= 0.03928
      ? normalized / 12.92
      : ((normalized + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

/** WCAG 2.x 相对亮度对比度；解析失败返回 null，断言按失败处理。 */
function contrastRatio(foreground, background) {
  const fg = parseRgb(foreground)
  const bg = parseRgb(background)
  if (!fg || !bg) return null
  const lighter = Math.max(relativeLuminance(fg), relativeLuminance(bg))
  const darker = Math.min(relativeLuminance(fg), relativeLuminance(bg))
  return (lighter + 0.05) / (darker + 0.05)
}

function rectsOverlap(first, second) {
  if (!first || !second) return false
  return !(
    first.right <= second.left ||
    first.left >= second.right ||
    first.bottom <= second.top ||
    first.top >= second.bottom
  )
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

async function setTheme(client, theme) {
  await evaluate(
    client,
    `document.documentElement.dataset.theme = ${JSON.stringify(theme)}`,
  )
}

async function setLargeMode(client, enabled) {
  await evaluate(
    client,
    `(() => {
      const harness = document.querySelector('.f10-harness')
      if (harness) harness.classList.toggle('site-shell', ${enabled})
      if (${enabled}) document.documentElement.dataset.presentation = 'large'
      else delete document.documentElement.dataset.presentation
    })()`,
  )
}

function arenaRequests(requests) {
  return requests.filter((item) => item.url.includes('/api/v1/arena/'))
}

function pathsOf(requests) {
  return requests.map((item) => {
    try {
      return new URL(item.url).pathname
    } catch {
      return item.url
    }
  })
}

function findRequest(requests, needle) {
  return requests.find((item) => item.url.includes(needle)) || null
}

function latestItem(mock) {
  const items = Object.values(mock.log.items)
  return items.length ? items[items.length - 1] : null
}

function modelOfAnon(item, anonId) {
  return item?.candidates.find((candidate) => candidate.anon_id === anonId) || null
}

async function domOf(client) {
  return evaluate(client, 'window.__f10.dom()')
}

async function waitPageState(client, states, timeout = 12_000) {
  return waitFor(
    async () => {
      const dom = await domOf(client)
      if (dom && states.includes(dom.pageState)) return dom
      return null
    },
    { timeout, label: `page state in ${JSON.stringify(states)}` },
  )
}

async function waitRevealed(client, timeout = 10_000) {
  return waitFor(
    async () => {
      const dom = await domOf(client)
      if (dom?.pageState === 'revealed') return dom
      return null
    },
    { timeout, label: 'revealed state' },
  )
}

async function waitToast(client, predicate, timeout = 6000) {
  return waitFor(
    async () => {
      const dom = await domOf(client)
      const messages = dom?.toastMessages || []
      const hit = messages.find((message) => predicate(message))
      return hit ? dom : null
    },
    { timeout, label: 'toast' },
  )
}

/** 等待投票后的榜单真正落地（rows 非空）；榜单只在投票成功后请求。 */
async function waitBoardRows(client, timeout = 8000) {
  return waitFor(
    async () => {
      const dom = await domOf(client)
      if (dom?.board?.available === 'true' && dom.board.rows.length > 0) return dom
      return null
    },
    { timeout, label: 'leaderboard rows' },
  )
}

async function navigate(client, url) {
  const loaded = client.once('Page.loadEventFired', 20_000)
  await client.send('Page.navigate', { url })
  await loaded
  await waitFor(
    async () => evaluate(client, 'Boolean(window.__f10 && window.__f10.ready)'),
    { timeout: 15_000, label: 'harness ready' },
  )
}

async function reload(client) {
  const loaded = client.once('Page.loadEventFired', 20_000)
  await client.send('Page.reload', { ignoreCache: true })
  await loaded
  await waitFor(
    async () => evaluate(client, 'Boolean(window.__f10 && window.__f10.ready)'),
    { timeout: 15_000, label: 'harness ready (reload)' },
  )
}

async function freshPage(client, mock, scenario, network) {
  await mock.setScenario(scenario)
  network.requests.length = 0
  network.responses.length = 0
  network.failed.length = 0
  await navigate(client, `${HARNESS_URL}?scenario=${encodeURIComponent(scenario)}&t=${Date.now()}`)
  const dom = await waitPageState(client, ['ready', 'empty', 'error'])
  return { dom, mock }
}

function logEvidence(mock, network) {
  return {
    mockScenario: mock.log.scenario,
    serverRequests: mock.log.requests,
    browserRequests: network.requests,
    browserResponses: network.responses,
    browserFailed: network.failed,
    items: mock.log.items,
    votes: mock.log.votes,
  }
}

const SCENARIOS = [
  {
    id: 'anonymity-vote-reveal',
    name: 'S1 正常（预热）：投票前匿名、投票后翻牌揭晓、预热榜显示区间不显示名次',
    async run(client, context) {
      const { checks, network, mock } = context
      await freshPage(client, mock, 'normal', network)
      const preVoteDom = await domOf(client)
      const item = latestItem(mock)
      const preVoteRequests = arenaRequests(network.requests)
      const preVotePaths = pathsOf(preVoteRequests)
      const preVoteLeaderboardRequests = preVoteRequests.filter((request) =>
        request.url.includes('/arena/leaderboard'),
      )
      const nextServerRequest = mock.log.requests.find((request) => request.kind === 'next')

      checks.check('页面进入可投票状态', preVoteDom.pageState === 'ready', String(preVoteDom.pageState))
      checks.check('渲染 4 张匿名卡片 A–D', preVoteDom.cards.length === 4, `${preVoteDom.cards.length}`)
      checks.check(
        '字母按服务端候选顺序映射 A/B/C/D',
        JSON.stringify(preVoteDom.cards.map((card) => card.letter)) ===
          JSON.stringify(['A', 'B', 'C', 'D']),
        JSON.stringify(preVoteDom.cards.map((card) => card.letter)),
      )
      const candidateLayout = await evaluate(client, `(() => {
        const grid = document.querySelector('[data-arena-candidates]');
        if (!grid) return null;
        const cards = Array.from(grid.querySelectorAll('[data-arena-card]'));
        const widths = cards.map((card) => card.getBoundingClientRect().width);
        const columns = getComputedStyle(grid).gridTemplateColumns
          .split(/\\s+/)
          .filter(Boolean);
        return { count: cards.length, columns: columns.length, widths };
      })()`)
      checks.check(
        '四个候选卡片等宽、四列等间距',
        candidateLayout?.count === 4 &&
          candidateLayout.columns === 4 &&
          Math.max(...candidateLayout.widths) - Math.min(...candidateLayout.widths) <= 1,
        JSON.stringify(candidateLayout),
      )
      checks.check(
        '投票前卡片均为 idle 且无背面/无 model_id 属性',
        preVoteDom.cards.every(
          (card) =>
            card.state === 'idle' &&
            card.hasBackFace === false &&
            card.revealedModelId === null,
        ),
        JSON.stringify(preVoteDom.cards.map((card) => `${card.letter}:${card.state}`)),
      )
      checks.check(
        '投票前候选区 DOM 不含 model_id / 真实模型名',
        identityLeaks(preVoteDom.candidatesText).length === 0,
        JSON.stringify(identityLeaks(preVoteDom.candidatesText)),
      )
      checks.check(
        '投票前整页 DOM 不含 model_id / 真实模型名',
        identityLeaks(preVoteDom.bodyText).length === 0,
        JSON.stringify(identityLeaks(preVoteDom.bodyText)),
      )
      checks.check(
        '投票前只请求 /arena/next（无 leaderboard 请求）',
        preVotePaths.length === 1 &&
          preVotePaths[0] === '/api/v1/arena/next' &&
          preVoteLeaderboardRequests.length === 0,
        JSON.stringify(preVotePaths),
      )
      const nextBodyText = JSON.stringify(nextServerRequest?.response ?? {})
      checks.check(
        '/arena/next 响应体不含 model_id / display_name / 真实模型名',
        nextServerRequest?.response &&
          nextBodyText.includes('anon_id') &&
          !nextBodyText.includes('"model_id"') &&
          !nextBodyText.includes('"display_name"') &&
          identityLeaks(nextBodyText).length === 0,
        nextBodyText.slice(0, 300),
      )
      checks.check(
        '投票前榜单为占位且标记 available=false',
        preVoteDom.board.hasPlaceholder === true && preVoteDom.board.available === 'false',
        JSON.stringify({ available: preVoteDom.board.available, placeholder: preVoteDom.board.hasPlaceholder }),
      )
      checks.check(
        '榜单固定说明在投票前即存在',
        preVoteDom.board.disclaimer.includes('人类偏好投票强度，仅供演示参考'),
        preVoteDom.board.disclaimer,
      )
      checks.check(
        '投票前候选区不出现名次/票数（榜单未加载）',
        preVoteDom.board.rows.length === 0,
        `${preVoteDom.board.rows.length}`,
      )

      await captureScreenshot(client, 'S1-pre-vote-anonymous')

      const anonOfC = preVoteDom.cards.find((card) => card.letter === 'C')
      const expected = modelOfAnon(item, anonOfC.anonId)
      checks.check('本地字母锚点可映射回服务端 anon_id', Boolean(anonOfC.anonId && expected), anonOfC.anonId)

      await evaluate(client, "window.__f10.clickCard('C')")
      await waitRevealed(client)
      await waitBoardRows(client)
      await sleep(120)
      const revealedDom = await domOf(client)
      const cardC = revealedDom.cards.find((card) => card.letter === 'C')
      const otherCards = revealedDom.cards.filter((card) => card.letter !== 'C')
      const voteRequest = findRequest(network.requests, '/arena/vote')
      const voteBody = voteRequest?.postData ? JSON.parse(voteRequest.postData) : null
      const voteServerRequest = mock.log.requests.find((request) => request.kind === 'vote')
      const leaderboardRequests = arenaRequests(network.requests).filter((request) =>
        request.url.includes('/arena/leaderboard'),
      )
      const board = revealedDom.board

      checks.check('投票请求为 POST /arena/vote', voteRequest?.method === 'POST', voteRequest?.method || 'missing')
      checks.check(
        '投票请求体只含 client_id/item_id/anon_id',
        voteBody !== null &&
          JSON.stringify(Object.keys(voteBody).sort()) ===
            JSON.stringify(['anon_id', 'client_id', 'item_id']) &&
          voteBody.anon_id === anonOfC.anonId &&
          voteBody.item_id === item.item_id,
        JSON.stringify(voteBody),
      )
      checks.check(
        '投票前 DOM/请求未携带 model_id（投票请求体无 model_id/display_name）',
        voteRequest?.postData && !voteRequest.postData.includes('model_id') && !voteRequest.postData.includes('display_name'),
        voteRequest?.postData || '',
      )
      checks.check('翻牌后卡片 C 状态为 revealed', cardC?.state === 'revealed', String(cardC?.state))
      checks.check(
        '翻牌背面正确显示被选中模型（model_id/display_name/pred_class）',
        cardC?.hasBackFace === true &&
          cardC.revealedModelId === expected.model_id &&
          cardC.text.includes(expected.display_name) &&
          cardC.text.includes(expected.model_id) &&
          cardC.text.includes('财经') === (expected.pred_class === 'finance'),
        JSON.stringify({ cardC, expected }),
      )
      checks.check(
        '其他 3 张卡片保持匿名（无背面/无 model_id）',
        otherCards.length === 3 &&
          otherCards.every((card) => card.hasBackFace === false && card.revealedModelId === null),
        JSON.stringify(otherCards.map((card) => `${card.letter}:${card.state}`)),
      )
      checks.check(
        '投票后其他卡片置灰、全部卡片禁用',
        otherCards.every((card) => card.disabled === true) &&
          revealedDom.cards.every((card) => card.disabled === true),
        JSON.stringify(revealedDom.cards.map((card) => `${card.letter}:${card.disabled}`)),
      )
      checks.check(
        '验收环境以 no-preference 运行（normal 模式未被强制减少动效）',
        revealedDom.reducedMotion === false,
        String(revealedDom.reducedMotion),
      )
      checks.check(
        '翻牌动画约 200ms（arena-flip 动画，duration≈200ms）',
        typeof cardC?.animationName === 'string' &&
          cardC.animationName.startsWith('arena-flip') &&
          Math.abs(durationMs(cardC.animationDuration) - 200) <= 5,
        `${cardC?.animationName} / ${cardC?.animationDuration}`,
      )
      checks.check(
        '无障碍 live 区播报已揭晓模型',
        revealedDom.revealedNote.includes(expected.display_name) &&
          revealedDom.revealedNote.includes(expected.model_id),
        revealedDom.revealedNote,
      )
      checks.check(
        '投票后请求 1 次 /arena/leaderboard',
        leaderboardRequests.length === 1,
        pathsOf(leaderboardRequests).join(','),
      )
      checks.check(
        '榜单 available=true 且 status=warming_up',
        board.available === 'true' && board.status === 'warming_up',
        JSON.stringify({ available: board.available, status: board.status }),
      )
      checks.check('预热态显示 4 行且参与样本 n < 50', board.rows.length === 4 && board.rows.every((row) => row.sampleSize < 50), JSON.stringify(board.rows.map((row) => row.sampleSize)))
      checks.check(
        '预热态不显示名次（无 rank 属性、表头无名次列）',
        board.rows.every((row) => row.rank === null) && !board.headers.includes('名次'),
        JSON.stringify({ ranks: board.rows.map((row) => row.rank), headers: board.headers }),
      )
      checks.check(
        '预热态每行显示 Wilson 区间（lower ≤ upper）',
        board.rows.every(
          (row) => Number.isFinite(row.wilsonLower) && Number.isFinite(row.wilsonUpper) && row.wilsonLower <= row.wilsonUpper,
        ),
        JSON.stringify(board.rows.map((row) => [row.wilsonLower, row.wilsonUpper])),
      )
      checks.check(
        '预热态徽标为「预热中」且规则说明不显示名次',
        board.statusText === '预热中' && board.ruleText.includes('不显示名次'),
        JSON.stringify({ statusText: board.statusText, ruleText: board.ruleText }),
      )
      checks.check(
        '榜单固定说明「人类偏好投票强度，仅供演示参考」',
        board.disclaimer.includes('人类偏好投票强度，仅供演示参考'),
        board.disclaimer,
      )
      checks.check(
        '榜单不含「准确率」「客观排名」表述',
        !board.text.includes('准确率') && !board.text.includes('客观排名'),
        board.text.slice(0, 160),
      )
      checks.check(
        '投票成功有成功提示（Toaster）',
        revealedDom.toastMessages.some((message) => message.includes('投票已记录')),
        JSON.stringify(revealedDom.toastMessages),
      )
      const storedClientId = await evaluate(client, 'window.__f10.clientId()')
      checks.check(
        'client_id 持久化到 localStorage 固定 key 并用于投票',
        typeof storedClientId === 'string' &&
          /^[A-Za-z0-9._-]{1,64}$/.test(storedClientId) &&
          voteServerRequest?.body?.client_id === storedClientId,
        JSON.stringify({ storedClientId, voteClientId: voteServerRequest?.body?.client_id }),
      )

      await captureScreenshot(client, 'S1-revealed')

      // 换下一题：榜单回到占位，不产生新的 leaderboard 请求。
      await evaluate(client, 'window.__f10.clickNext()')
      await waitFor(
        async () => {
          const dom = await domOf(client)
          return dom.pageState === 'ready' && dom.cards.length === 4 ? dom : null
        },
        { timeout: 10_000, label: 'next item ready' },
      )
      const nextDom = await domOf(client)
      const leaderboardAfterNext = arenaRequests(network.requests).filter((request) =>
        request.url.includes('/arena/leaderboard'),
      )
      checks.check(
        '换下一题后榜单回到占位（投票前不展示真实模型名）',
        nextDom.board.available === 'false' && nextDom.board.hasPlaceholder === true,
        JSON.stringify({ available: nextDom.board.available, placeholder: nextDom.board.hasPlaceholder }),
      )
      checks.check(
        '换下一题不重复请求榜单（仍为 1 次）',
        leaderboardAfterNext.length === 1,
        `${leaderboardAfterNext.length}`,
      )
      checks.check(
        '换下一题后候选区仍无 model_id / 真实模型名',
        identityLeaks(nextDom.candidatesText).length === 0,
        JSON.stringify(identityLeaks(nextDom.candidatesText)),
      )

      return { evidence: logEvidence(mock, network), dom: revealedDom, nextDom }
    },
  },
  {
    id: 'ready-ranking',
    name: 'S2 就绪：票数 ≥ 50 时显示正式名次（按名次排序）',
    async run(client, context) {
      const { checks, network, mock } = context
      await freshPage(client, mock, 'ready', network)
      const preVoteDom = await domOf(client)
      checks.check('就绪场景投票前也不请求榜单', !arenaRequests(network.requests).some((request) => request.url.includes('/arena/leaderboard')), pathsOf(arenaRequests(network.requests)).join(','))
      checks.check('投票前整体 DOM 无真实模型名', identityLeaks(preVoteDom.bodyText).length === 0, JSON.stringify(identityLeaks(preVoteDom.bodyText)))

      await evaluate(client, "window.__f10.clickCard('A')")
      await waitRevealed(client)
      await waitBoardRows(client)
      const revealedDom = await domOf(client)
      const board = revealedDom.board
      const ranks = board.rows.map((row) => Number(row.rank))
      checks.check('ready 状态徽标为「已就绪」', board.status === 'ready' && board.statusText === '已就绪', JSON.stringify({ status: board.status, statusText: board.statusText }))
      checks.check('ready 表头包含名次列', board.headers.includes('名次'), JSON.stringify(board.headers))
      checks.check('ready 每行都显示名次 1..4', ranks.length === 4 && [...ranks].sort((a, b) => a - b).join(',') === '1,2,3,4' && ranks.every((rank) => rank >= 1), JSON.stringify(ranks))
      checks.check('ready 行按名次升序排列', ranks.every((rank, index) => index === 0 || rank > ranks[index - 1]), JSON.stringify(ranks))
      checks.check('ready 每行参与样本 n ≥ 50', board.rows.every((row) => row.sampleSize >= 50), JSON.stringify(board.rows.map((row) => row.sampleSize)))
      checks.check('ready 显示模型名（允许的聚合榜身份）', board.rows.every((row) => Boolean(row.displayName)), JSON.stringify(board.rows.map((row) => row.displayName)))
      checks.check('ready 仍包含固定「人类偏好…」说明', board.disclaimer.includes('人类偏好投票强度，仅供演示参考'), board.disclaimer)
      checks.check('ready 文字不含「准确率」「客观排名」', !board.text.includes('准确率') && !board.text.includes('客观排名'), board.text.slice(0, 160))
      checks.check('ready 规则说明为显示正式名次', board.ruleText.includes('显示正式名次'), board.ruleText)
      await captureScreenshot(client, 'S2-ready-ranking')
      return { evidence: logEvidence(mock, network), dom: revealedDom }
    },
  },
  {
    id: 'partial-candidates',
    name: 'S3 部分候选：partial=true 显示部分结果提示，仍可投票揭晓',
    async run(client, context) {
      const { checks, network, mock } = context
      const { dom: readyDom } = await freshPage(client, mock, 'partial', network)
      const item = latestItem(mock)
      checks.check('partial 场景只下发 3 个匿名候选', readyDom.cards.length === 3, `${readyDom.cards.length}`)
      checks.check('页面显示部分候选提示（复用 AsyncState partial）', Boolean(readyDom.hasPartial) || readyDom.bodyText.includes('部分候选已返回'), JSON.stringify({ hasPartial: readyDom.hasPartial }))
      checks.check('partial 响应标记 partial=true', item && mock.log.requests.find((request) => request.kind === 'next')?.response?.partial === true, JSON.stringify(mock.log.requests.find((request) => request.kind === 'next')?.response?.partial))
      await evaluate(client, "window.__f10.clickCard('B')")
      await waitRevealed(client)
      await waitBoardRows(client)
      const revealedDom = await domOf(client)
      const cardB = revealedDom.cards.find((card) => card.letter === 'B')
      const expected = modelOfAnon(item, cardB?.anonId)
      checks.check('partial 场景投票后可正确揭晓', cardB?.state === 'revealed' && cardB?.revealedModelId === expected?.model_id, JSON.stringify({ state: cardB?.state, expected }))
      checks.check('partial 场景投票后榜单已加载', revealedDom.board.available === 'true' && revealedDom.board.rows.length === 4, JSON.stringify({ available: revealedDom.board.available, rows: revealedDom.board.rows.length }))
      await captureScreenshot(client, 'S3-partial')
      return { evidence: logEvidence(mock, network), dom: revealedDom }
    },
  },
  {
    id: 'duplicate-vote',
    name: 'S4 重复投票：409 ARENA_DUPLICATE_VOTE → 中文提示，不伪造揭晓',
    async run(client, context) {
      const { checks, network, mock } = context
      await freshPage(client, mock, 'duplicate', network)
      await evaluate(client, "window.__f10.clickCard('A')")
      const toastDom = await waitToast(client, (message) => message.includes('已经对这道题投过票'))
      await sleep(120)
      const dom = await domOf(client)
      const voteRequest = findRequest(network.requests, '/arena/vote')
      checks.check('重复投票提示来自 Toaster（F4）', toastDom.toastMessages.some((message) => message === '你已经对这道题投过票了。'), JSON.stringify(toastDom.toastMessages))
      checks.check('提示附带「只计 1 票」说明', toastDom.toastDetails.some((detail) => detail.includes('只计 1 票')), JSON.stringify(toastDom.toastDetails))
      checks.check('失败后不进入揭晓态、不显示 model_id', dom.pageState === 'ready' && dom.cards.every((card) => card.state !== 'revealed' && card.revealedModelId === null), JSON.stringify(dom.cards.map((card) => card.state)))
      checks.check('失败后卡片回到可投票（未被禁用）', dom.cards.every((card) => card.disabled === false), JSON.stringify(dom.cards.map((card) => card.disabled)))
      checks.check('失败后不请求榜单', !arenaRequests(network.requests).some((request) => request.url.includes('/arena/leaderboard')), pathsOf(arenaRequests(network.requests)).join(','))
      checks.check('错误响应码为 409', mock.log.requests.find((request) => request.kind === 'vote')?.status === 409, String(mock.log.requests.find((request) => request.kind === 'vote')?.status))
      checks.check('投票请求仍只含匿名载荷', voteRequest?.postData && !voteRequest.postData.includes('model_id'), voteRequest?.postData || '')
      await captureScreenshot(client, 'S4-duplicate-vote')
      return { evidence: logEvidence(mock, network), dom }
    },
  },
  {
    id: 'rate-limited',
    name: 'S5 限速：429 ARENA_RATE_LIMITED → 可读提示',
    async run(client, context) {
      const { checks, network, mock } = context
      await freshPage(client, mock, 'rate-limited', network)
      await evaluate(client, "window.__f10.clickCard('D')")
      const toastDom = await waitToast(client, (message) => message.includes('投票太快'))
      const dom = await domOf(client)
      checks.check('限速提示为「投票太快了，请稍后再试。」', toastDom.toastMessages.includes('投票太快了，请稍后再试。'), JSON.stringify(toastDom.toastMessages))
      checks.check('限速提示附带每分钟 20 票说明', toastDom.toastDetails.some((detail) => detail.includes('20 票')), JSON.stringify(toastDom.toastDetails))
      checks.check('限速后不揭晓、不请求榜单', dom.pageState === 'ready' && !arenaRequests(network.requests).some((request) => request.url.includes('/arena/leaderboard')), `${dom.pageState}`)
      checks.check('限速响应码为 429', mock.log.requests.find((request) => request.kind === 'vote')?.status === 429, String(mock.log.requests.find((request) => request.kind === 'vote')?.status))
      await captureScreenshot(client, 'S5-rate-limited')
      return { evidence: logEvidence(mock, network), dom }
    },
  },
  {
    id: 'network-error',
    name: 'S6 网络错误：连接被断开 → NETWORK_ERROR 提示，不伪造揭晓',
    async run(client, context) {
      const { checks, network, mock } = context
      await freshPage(client, mock, 'network', network)
      await evaluate(client, "window.__f10.clickCard('D')")
      const toastDom = await waitToast(client, (message) => message.includes('无法连接') || message.includes('请求没有完成'))
      const dom = await domOf(client)
      checks.check('网络错误提示可读（NETWORK_ERROR 映射）', toastDom.toastMessages.some((message) => message === '无法连接分类服务，请确认后端已启动。' || message.includes('请求没有完成')), JSON.stringify(toastDom.toastMessages))
      checks.check('网络错误后不进入揭晓态', dom.pageState === 'ready' && dom.cards.every((card) => card.state !== 'revealed'), String(dom.pageState))
      checks.check('网络错误后卡片仍可重试', dom.cards.every((card) => card.disabled === false), JSON.stringify(dom.cards.map((card) => card.disabled)))
      checks.check('网络错误不请求榜单', !arenaRequests(network.requests).some((request) => request.url.includes('/arena/leaderboard')), pathsOf(arenaRequests(network.requests)).join(','))
      await captureScreenshot(client, 'S6-network-error')
      return { evidence: logEvidence(mock, network), dom }
    },
  },
  {
    id: 'empty-bank',
    name: 'S7 空题库：ARENA_NO_ITEMS → 空状态 + 可重新抽取',
    async run(client, context) {
      const { checks, network, mock } = context
      const { dom: emptyDom } = await freshPage(client, mock, 'empty', network)
      checks.check('页面进入 empty 状态', emptyDom.pageState === 'empty', String(emptyDom.pageState))
      checks.check('空题库使用 AsyncState 空状态（非自写横幅）', emptyDom.hasEmpty === true && emptyDom.emptyText.includes('暂无可用题目'), emptyDom.emptyText)
      checks.check('空题库不渲染候选卡片', emptyDom.cards.length === 0, `${emptyDom.cards.length}`)
      checks.check('空题库不请求榜单', !arenaRequests(network.requests).some((request) => request.url.includes('/arena/leaderboard')), pathsOf(arenaRequests(network.requests)).join(','))
      checks.check('空题库错误码为 ARENA_NO_ITEMS', mock.log.requests.find((request) => request.kind === 'next')?.response?.error?.code === 'ARENA_NO_ITEMS', JSON.stringify(mock.log.requests.find((request) => request.kind === 'next')?.response?.error))
      checks.check('空状态提供「重新抽取」按钮', (await evaluate(client, "Boolean(document.querySelector('[data-arena-empty-retry]'))")) === true, 'data-arena-empty-retry')
      await captureScreenshot(client, 'S7-empty-bank')

      // 切换回正常场景后点击「重新抽取」，应恢复可投票。
      await mock.setScenario('normal')
      await evaluate(client, 'window.__f10.clickEmptyRetry()')
      const recoveredDom = await waitPageState(client, ['ready'])
      checks.check('空题库后重新抽取可恢复候选', recoveredDom.cards.length === 4, `${recoveredDom.cards.length}`)
      return { evidence: logEvidence(mock, network), dom: emptyDom }
    },
  },
  {
    id: 'reduced-motion',
    name: 'S8 减少动效：prefers-reduced-motion: reduce 时立即揭晓且无动画',
    async run(client, context) {
      const { checks, network, mock } = context
      await client.send('Emulation.setEmulatedMedia', {
        features: [{ name: 'prefers-reduced-motion', value: 'reduce' }],
      })
      try {
        await freshPage(client, mock, 'normal', network)
        const beforeDom = await domOf(client)
        checks.check('浏览器已应用 prefers-reduced-motion: reduce', beforeDom.reducedMotion === true, String(beforeDom.reducedMotion))
        const clickAt = Date.now()
        await evaluate(client, "window.__f10.clickCard('A')")
        const revealedDom = await waitRevealed(client, 6000)
        const revealDelayMs = Date.now() - clickAt
        const cardA = revealedDom.cards.find((card) => card.letter === 'A')
        checks.check('reduce 下投票后进入揭示态', cardA?.state === 'revealed', String(cardA?.state))
        checks.check('reduce 下揭晓无 200ms 动画等待（≤1500ms）', revealDelayMs <= 1500, `${revealDelayMs} ms`)
        checks.check('reduce 下翻牌无动画（animation-name=none）', cardA?.animationName === 'none', String(cardA?.animationName))
        checks.check('reduce 下动画时长为 0（≤1ms，跳过 200ms 翻牌）', durationMs(cardA?.animationDuration) <= 1, String(cardA?.animationDuration))
        checks.check('reduce 下过渡时长为 0（≤1ms）', durationMs(cardA?.transitionDuration) <= 1, String(cardA?.transitionDuration))
        await captureScreenshot(client, 'S8-reduced-motion')
        return { evidence: logEvidence(mock, network), dom: revealedDom }
      } finally {
        await client.send('Emulation.setEmulatedMedia', {
          features: [{ name: 'prefers-reduced-motion', value: 'no-preference' }],
        })
      }
    },
  },
  {
    id: 'client-id-persist',
    name: 'S9 client_id 刷新保持：localStorage 固定 key，刷新后同一投票身份',
    async run(client, context) {
      const { checks, network, mock } = context
      await freshPage(client, mock, 'normal', network)
      const clientId1 = await evaluate(client, 'window.__f10.clientId()')
      checks.check('首次加载即写入 localStorage 固定 key', typeof clientId1 === 'string' && /^[A-Za-z0-9._-]{1,64}$/.test(clientId1), String(clientId1))
      checks.check('localStorage 可用', (await evaluate(client, 'window.__f10.storageAvailable()')) === true, 'available')
      await evaluate(client, "window.__f10.clickCard('B')")
      await waitRevealed(client)
      await waitBoardRows(client)
      const firstVote = mock.log.requests.find((request) => request.kind === 'vote')
      checks.check('首次投票使用客户端持久化 client_id', firstVote?.body?.client_id === clientId1, JSON.stringify({ stored: clientId1, sent: firstVote?.body?.client_id }))

      await reload(client)
      const afterReloadDom = await waitPageState(client, ['ready'])
      const clientId2 = await evaluate(client, 'window.__f10.clientId()')
      checks.check('刷新后 client_id 保持不变', clientId2 === clientId1, JSON.stringify({ before: clientId1, after: clientId2 }))
      checks.check('刷新后榜单重新回到占位（先投票才加载）', afterReloadDom.board.available === 'false' && afterReloadDom.board.hasPlaceholder === true, JSON.stringify({ available: afterReloadDom.board.available }))
      const requestsAfterReload = arenaRequests(network.requests).filter((request) => request.url.includes('/arena/leaderboard'))
      checks.check('刷新后投票前不请求榜单', requestsAfterReload.length === 1, `${requestsAfterReload.length}（刷新前 1 次）`)

      mock.log.requests.length = 0
      await evaluate(client, "window.__f10.clickCard('C')")
      await waitRevealed(client)
      const secondVote = mock.log.requests.find((request) => request.kind === 'vote')
      checks.check('刷新后再次投票仍使用同一 client_id', secondVote?.body?.client_id === clientId1, JSON.stringify({ stored: clientId1, sent: secondVote?.body?.client_id }))
      checks.check('刷新后投票成功揭晓', (await domOf(client)).pageState === 'revealed', (await domOf(client)).pageState)
      return { evidence: logEvidence(mock, network), dom: await domOf(client) }
    },
  },
  {
    id: 'title-readability',
    name: 'S10 题目标题：独立整行、响应式字号、对比度、换行与无溢出（1440/760/390，亮/暗/大屏）',
    async run(client, context) {
      const { checks, network, mock } = context
      const viewports = [
        { width: 1440, height: 900, label: '1440', expectedFont: 16 },
        { width: 760, height: 900, label: '760', expectedFont: 14 },
        { width: 390, height: 844, label: '390', expectedFont: 14 },
      ]

      const assertTitle = (dom, item, label, expectedFont) => {
        const title = dom.title
        const page = dom.page
        const contrast = contrastRatio(title.color, title.backgroundColor)
        const lineHeightRatio = title.fontSize > 0 ? title.lineHeight / title.fontSize : 0

        checks.check(`${label}：标题存在且文本等于 /arena/next 返回 title`, title.present && title.text === item.title, JSON.stringify({ text: title.text, expected: item.title }))
        checks.check(`${label}：标题 title 属性保留完整文本`, title.titleAttr === item.title, JSON.stringify({ titleAttr: title.titleAttr, expected: item.title }))
        checks.check(`${label}：标题字号为 ${expectedFont}px`, Math.abs(title.fontSize - expectedFont) < 0.1, `${title.fontSize}px`)
        checks.check(`${label}：标题行高比约 1.5–1.6`, lineHeightRatio >= 1.45 && lineHeightRatio <= 1.65, `${lineHeightRatio.toFixed(3)}（${title.lineHeight}px / ${title.fontSize}px）`)
        checks.check(`${label}：标题颜色对比度 ≥ 4.5:1`, contrast !== null && contrast >= 4.5, `${contrast === null ? 'unparsed' : contrast.toFixed(2)}:1（${title.color} on ${title.backgroundColor}）`)
        checks.check(`${label}：标题允许换行且连续字符可断行`, title.whiteSpace === 'normal' && title.overflowWrap === 'anywhere', JSON.stringify({ whiteSpace: title.whiteSpace, overflowWrap: title.overflowWrap }))
        checks.check(`${label}：标题最多 3 行`, title.lineClamp === '3' && title.visibleLines >= 1 && title.visibleLines <= 3, JSON.stringify({ lineClamp: title.lineClamp, visibleLines: title.visibleLines }))
        checks.check(`${label}：长标题发生合理换行（≥2 行）`, title.visibleLines >= 2, `${title.visibleLines} 行`)
        checks.check(`${label}：页面无横向滚动`, page.docScrollWidth <= page.innerWidth && page.bodyScrollWidth <= page.innerWidth, JSON.stringify(page))
        checks.check(`${label}：标题不覆盖「换一题/下一题」按钮`, !rectsOverlap(title.rect, dom.nextButtonRect), JSON.stringify({ title: title.rect, next: dom.nextButtonRect }))
        checks.check(`${label}：标题在视口与面板内`, Boolean(title.rect) && title.rect.left >= -1 && title.rect.right <= page.innerWidth + 1, JSON.stringify(title.rect))
        checks.check(`${label}：候选卡数量与接口一致且保持可点击`, dom.cards.length === item.candidates.length && dom.cards.every((card) => card.disabled === false), JSON.stringify({ cards: dom.cards.length, expected: item.candidates.length, disabled: dom.cards.map((card) => card.disabled) }))
        const visuallyClipped = title.scrollHeight > title.clientHeight + 1
        checks.check(
          `${label}：如标题被 3 行限行，title 属性仍保留完整文本`,
          !visuallyClipped || title.titleAttr === item.title,
          JSON.stringify({ visuallyClipped, scrollHeight: title.scrollHeight, clientHeight: title.clientHeight }),
        )
      }

      try {
        for (const viewport of viewports) {
          await setTheme(client, 'light')
          await setLargeMode(client, false)
          await setViewport(client, viewport.width, viewport.height)
          const { dom } = await freshPage(client, mock, 'long-title', network)
          const item = latestItem(mock)
          assertTitle(dom, item, viewport.label, viewport.expectedFont)
        }

        // 暗色主题：390px 下标题对比度必须仍满足 AA。
        await setLargeMode(client, false)
        await setViewport(client, 390, 844)
        await setTheme(client, 'light')
        const { dom: lightDom } = await freshPage(client, mock, 'long-title', network)
        const item = latestItem(mock)
        await setTheme(client, 'dark')
        await sleep(80)
        const darkDom = await domOf(client)
        const darkContrast = contrastRatio(darkDom.title.color, darkDom.title.backgroundColor)
        checks.check('390 暗色：页面应用 dark 主题', (await evaluate(client, "document.documentElement.dataset.theme")) === 'dark', 'dark')
        checks.check('390 暗色：标题对比度 ≥ 4.5:1', darkContrast !== null && darkContrast >= 4.5, `${darkContrast === null ? 'unparsed' : darkContrast.toFixed(2)}:1（${darkDom.title.color} on ${darkDom.title.backgroundColor}）`)
        checks.check('390 暗色：页面仍无横向滚动', darkDom.page.docScrollWidth <= darkDom.page.innerWidth && darkDom.page.bodyScrollWidth <= darkDom.page.innerWidth, JSON.stringify(darkDom.page))
        checks.check('390 暗色：标题文本仍与接口一致', darkDom.title.text === item.title, JSON.stringify({ text: darkDom.title.text, expected: item.title }))
        checks.check('亮/暗主题切换不改变标题文本', lightDom.title.text === darkDom.title.text, JSON.stringify({ light: lightDom.title.text, dark: darkDom.title.text }))

        // 大屏：已有 .site-shell zoom:1.25；标题 CSS 字号不应再次乘 1.25。
        await setTheme(client, 'light')
        await setViewport(client, 1440, 900)
        await freshPage(client, mock, 'long-title', network)
        await setLargeMode(client, true)
        await sleep(80)
        const largeDom = await domOf(client)
        checks.check('1440 大屏：shell 启用 site-shell', (await evaluate(client, "document.querySelector('.f10-harness')?.classList.contains('site-shell')")) === true, 'site-shell')
        checks.check('1440 大屏：标题字号仍为 16px（未叠加 1.25 倍率）', Math.abs(largeDom.title.fontSize - 16) < 0.1, `${largeDom.title.fontSize}px`)
        checks.check('1440 大屏：标题不覆盖操作按钮且无横向滚动', !rectsOverlap(largeDom.title.rect, largeDom.nextButtonRect) && largeDom.page.docScrollWidth <= largeDom.page.innerWidth, JSON.stringify({ title: largeDom.title.rect, next: largeDom.nextButtonRect, page: largeDom.page }))
        checks.check('1440 大屏：标题文本与接口一致', largeDom.title.text === latestItem(mock).title, JSON.stringify({ text: largeDom.title.text, expected: latestItem(mock).title }))

        return { evidence: logEvidence(mock, network), dom: largeDom }
      } finally {
        await setLargeMode(client, false)
        await setTheme(client, 'light')
        await clearViewport(client)
      }
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
    '--window-size=1440,1400',
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
  await mkdir(envDir, { recursive: true })
  await writeFile(join(envDir, '.env.local'), `VITE_API_BASE_URL=http://127.0.0.1:${SERVER_PORT}\n`)

  console.log('=== F10 竞技场 UI 验收（mock B7 API + Edge headless/CDP）===')
  // 明确覆盖继承自开发 shell 的 VITE_API_BASE_URL；否则真实的 LAN 地址会进入
  // harness 构建，测试请求可能打到用户正在使用的网关而不是本地 mock。
  process.env.VITE_API_BASE_URL = `http://127.0.0.1:${SERVER_PORT}`
  console.log(`[build] 构建 harness（独立 outDir: ${distDir}）`)
  await build({ configFile: join(here, 'vite.harness.config.mjs'), logLevel: 'warn' })
  console.log('[build] OK')

  const mock = await createMockServer({ port: SERVER_PORT, staticDir: distDir })
  console.log(`[mock] 本地 mock 竞技场 API: http://127.0.0.1:${SERVER_PORT}`)

  const profileDir = join(os.tmpdir(), `tmf-f10-edge-${process.pid}-${Date.now()}`)
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
    // 本机 Edge headless 可能默认命中 prefers-reduced-motion: reduce；
    // 显式声明 no-preference，让 S1/S2 等场景验证 200ms 翻牌动画。
    await client.send('Emulation.setEmulatedMedia', {
      features: [{ name: 'prefers-reduced-motion', value: 'no-preference' }],
    })

    const network = { requests: [], responses: [], failed: [] }
    client.on('Network.requestWillBeSent', (params) => {
      network.requests.push({
        ts: Date.now(),
        requestId: params.requestId,
        url: params.request.url,
        method: params.request.method,
        type: params.type,
        postData: params.request.postData ?? null,
      })
    })
    client.on('Network.responseReceived', (params) => {
      network.responses.push({
        ts: Date.now(),
        requestId: params.requestId,
        url: params.response.url,
        status: params.response.status,
        mimeType: params.response.mimeType,
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
      const context = { checks, network, mock }
      const scenarioStartedAt = Date.now()
      let evidence = null
      let scenarioError = null
      try {
        const result = await scenario.run(client, context)
        evidence = { ...result.evidence, dom: result.dom ?? null }
      } catch (error) {
        scenarioError = error
        checks.check('场景执行未抛异常', false, error.stack || error.message)
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
        evidence,
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
      mode: 'mock B7 API + Edge headless/CDP',
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
    const reportPath = join(artifactDir, 'f10-report.json')
    await writeFile(reportPath, JSON.stringify(report, null, 2))
    console.log(`\n[artifact] ${reportPath}`)

    console.log('\n=== 结果汇总 ===')
    for (const item of summary) {
      const passed = item.checks.filter((check) => check.pass).length
      console.log(`${item.pass ? 'PASS' : 'FAIL'}  ${item.id.padEnd(22)} ${passed}/${item.checks.length}  ${item.name}`)
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
  console.error('[F10] 运行失败：', error.stack || error.message)
  process.exitCode = 1
})
