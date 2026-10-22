/**
 * F10 测试用「本地可控 mock 竞技场 API server」（不是真实 B7 后端，不进入 src/）。
 *
 * 用途：在浏览器里真实跑 `ArenaPage + useArena + ArenaAnonymousCard + ArenaLeaderboard`，
 * 覆盖匿名性、翻牌、榜单预热/就绪、限速/重复/网络/空题库等验收场景。
 *
 * 契约与 B7 一致：
 *   GET  /api/v1/arena/next        -> {request_id,item_id,title,candidates:[{anon_id,pred_class,latency_ms}],partial}
 *   POST /api/v1/arena/vote        -> {request_id,selected:{model_id,display_name,pred_class},elo_after,games_applied}
 *   GET  /api/v1/arena/leaderboard -> {request_id,status,data_kind,note,min_votes_for_ranking,entries[]}
 * 投票前响应**不含** model_id / display_name（与 B7 相同的匿名性约束）。
 *
 * 场景由 `POST /__scenario {name}` 控制（仅测试运行器调用，页面不感知）：
 *   normal / partial / ready / empty / duplicate / rate-limited / network
 * `GET /__log` 返回请求日志、服务端侧响应体、anon_id→模型映射与投票计数，供断言取证。
 * 所有数据均为本文件显式构造的 mock，不访问真实网关 / worker / LLM。
 */
import http from 'node:http'
import { randomBytes } from 'node:crypto'
import { readFile } from 'node:fs/promises'
import { extname, normalize, resolve, sep } from 'node:path'

const CONTENT_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
}

const CORS_HEADERS = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
  'Access-Control-Allow-Headers': 'Content-Type,X-Request-ID',
}

const MODELS = [
  { model_id: 'rf', display_name: '随机森林', pred_class: 'finance', elo0: 1218 },
  { model_id: 'fasttext', display_name: 'fastText', pred_class: 'finance', elo0: 1206 },
  { model_id: 'bert', display_name: 'BERT', pred_class: 'finance', elo0: 1188 },
  { model_id: 'distill', display_name: '蒸馏模型', pred_class: 'stocks', elo0: 1212 },
  { model_id: 'llm', display_name: 'LLM', pred_class: 'finance', elo0: 1176 },
]

const TITLES = [
  '多地公布新一轮城市更新计划',
  '高校发布今年招生政策调整方案',
  '国产推理芯片进入量产阶段',
  '联赛决赛门票今日开售',
  '新片首周票房突破十亿元',
]

const LEADERBOARD_NOTE = '人类偏好投票强度，仅作演示参考；票数可被本机清除。'
const WARMUP_MIN_VOTES = 50
/** 预热场景初始战绩（票数均 < 50，不足以排序）。 */
const WARMING_SEED = {
  rf: { wins: 2, losses: 1 },
  fasttext: { wins: 1, losses: 2 },
  bert: { wins: 0, losses: 3 },
  distill: { wins: 1, losses: 1 },
  llm: { wins: 0, losses: 2 },
}
/** ready 场景初始战绩（票数均 ≥ 50，可显示名次）。 */
const READY_SEED = {
  rf: { wins: 60, losses: 45 },
  fasttext: { wins: 58, losses: 47 },
  bert: { wins: 52, losses: 53 },
  distill: { wins: 55, losses: 50 },
  llm: { wins: 51, losses: 54 },
}

const WILSON_Z = 1.96

function round4(value) {
  return Math.round(value * 10_000) / 10_000
}

function wilson(wins, n, z = WILSON_Z) {
  if (n <= 0) return [0, 1]
  const p = wins / n
  const denominator = 1 + (z * z) / n
  const center = (p + (z * z) / (2 * n)) / denominator
  const margin = (z * Math.sqrt((p * (1 - p) + (z * z) / (4 * n)) / n)) / denominator
  const lower = Math.max(0, center - margin)
  const upper = Math.min(1, center + margin)
  return lower > upper ? [upper, lower] : [lower, upper]
}

/** 匿名 id 只用十六进制字符：不会意外包含模型 id 子串（rf/ft/llm/bert/distill）。 */
function newAnonId(used) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    const value = `an_${randomBytes(8).toString('hex').slice(0, 12)}`
    if (!used.has(value)) return value
  }
  return `an_${randomBytes(16).toString('hex')}`
}

function newItemId(counter) {
  return `item_${counter}_${randomBytes(8).toString('hex')}`
}

function shuffle(list) {
  const copy = [...list]
  for (let index = copy.length - 1; index > 0; index -= 1) {
    const swap = Math.floor(Math.random() * (index + 1))
    ;[copy[index], copy[swap]] = [copy[swap], copy[index]]
  }
  return copy
}

export async function createMockServer({ port, staticDir }) {
  const staticRoot = resolve(staticDir)
  const log = {
    requests: [],
    items: {},
    scenario: 'normal',
    votes: {},
    itemCounter: 0,
  }

  function resetState(scenario) {
    log.scenario = scenario
    log.requests.length = 0
    log.items = {}
    log.votes = {}
    log.itemCounter = 0
    const seed = scenario === 'ready' ? READY_SEED : WARMING_SEED
    for (const model of MODELS) {
      log.votes[model.model_id] = {
        wins: seed[model.model_id].wins,
        losses: seed[model.model_id].losses,
        elo: model.elo0,
      }
    }
  }

  resetState('normal')

  const server = http.createServer((req, res) => {
    void handleRequest(req, res).catch((error) => {
      try {
        res.writeHead(500, { 'Content-Type': 'application/json; charset=utf-8', ...CORS_HEADERS })
        res.end(JSON.stringify({ error: { code: 'MOCK_SERVER_ERROR', message: String(error) } }))
      } catch {
        // 连接已关闭。
      }
    })
  })

  function sendJson(res, status, body) {
    res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', ...CORS_HEADERS })
    res.end(JSON.stringify(body))
  }

  function record(entry) {
    log.requests.push(entry)
    return entry
  }

  async function readBody(req) {
    let body = ''
    for await (const chunk of req) body += chunk
    if (!body) return null
    try {
      return JSON.parse(body)
    } catch {
      return { __raw: body }
    }
  }

  function itemCandidates(itemId) {
    return log.items[itemId]?.candidates ?? null
  }

  function buildLeaderboard(requestId) {
    const entries = MODELS.map((model) => {
      const stat = log.votes[model.model_id]
      const sampleSize = stat.wins + stat.losses
      const votes = stat.wins
      const [lower, upper] = wilson(votes, sampleSize)
      return {
        model_id: model.model_id,
        display_name: model.display_name,
        elo: round4(stat.elo),
        votes,
        sample_size: sampleSize,
        wins: stat.wins,
        losses: stat.losses,
        win_rate: sampleSize ? round4(votes / sampleSize) : null,
        wilson_lower: round4(lower),
        wilson_upper: round4(upper),
        warming_up: sampleSize < WARMUP_MIN_VOTES,
        rank: null,
      }
    })
    const ready = entries.length > 0 && entries.every((entry) => entry.sample_size >= WARMUP_MIN_VOTES)
    if (ready) {
      const order = entries
        .map((_, index) => index)
        .sort((left, right) => entries[right].elo - entries[left].elo || left - right)
      order.forEach((entryIndex, rankIndex) => {
        entries[entryIndex].rank = rankIndex + 1
      })
    }
    return {
      request_id: requestId,
      status: ready ? 'ready' : 'warming_up',
      data_kind: 'vote',
      note: LEADERBOARD_NOTE,
      min_votes_for_ranking: WARMUP_MIN_VOTES,
      entries,
    }
  }

  async function handleRequest(req, res) {
    const url = new URL(req.url || '/', `http://127.0.0.1:${port}`)
    if (req.method === 'OPTIONS') {
      res.writeHead(204, CORS_HEADERS)
      res.end()
      return
    }

    if (url.pathname === '/__scenario' && req.method === 'POST') {
      const payload = await readBody(req)
      resetState(String(payload?.name || 'normal'))
      sendJson(res, 200, { ok: true, scenario: log.scenario })
      return
    }

    if (url.pathname === '/__log') {
      sendJson(res, 200, log)
      return
    }

    if (url.pathname === '/api/v1/arena/next' && req.method === 'GET') {
      const entry = record({
        ts: Date.now(),
        method: req.method,
        path: url.pathname,
        kind: 'next',
        body: null,
        status: 200,
        response: null,
      })
      if (log.scenario === 'empty') {
        entry.status = 503
        entry.response = {
          request_id: `mock-${Date.now()}`,
          error: { code: 'ARENA_NO_ITEMS', message: '竞技场暂无可用题目' },
        }
        sendJson(res, 503, entry.response)
        return
      }
      log.itemCounter += 1
      const limit = log.scenario === 'partial' ? 3 : 5
      const picked = shuffle(MODELS).slice(0, limit)
      const usedAnon = new Set()
      const itemId = newItemId(log.itemCounter)
      const candidates = picked.map((model) => {
        const anonId = newAnonId(usedAnon)
        usedAnon.add(anonId)
        return {
          anon_id: anonId,
          pred_class: model.pred_class,
          latency_ms: model.model_id === 'llm' ? 1380 : 90 + Math.round(Math.random() * 260),
          model_id: model.model_id,
          display_name: model.display_name,
        }
      })
      log.items[itemId] = {
        item_id: itemId,
        title: TITLES[log.itemCounter % TITLES.length],
        candidates,
      }
      entry.response = {
        request_id: `mock-${Date.now()}`,
        item_id: itemId,
        title: TITLES[log.itemCounter % TITLES.length],
        candidates: candidates.map((candidate) => ({
          anon_id: candidate.anon_id,
          pred_class: candidate.pred_class,
          latency_ms: candidate.latency_ms,
        })),
        partial: log.scenario === 'partial',
      }
      sendJson(res, 200, entry.response)
      return
    }

    if (url.pathname === '/api/v1/arena/vote' && req.method === 'POST') {
      const body = await readBody(req)
      const entry = record({
        ts: Date.now(),
        method: req.method,
        path: url.pathname,
        kind: 'vote',
        body,
        status: 200,
        response: null,
      })
      const scenario = log.scenario
      if (scenario === 'duplicate') {
        entry.status = 409
        entry.response = {
          request_id: `mock-${Date.now()}`,
          error: { code: 'ARENA_DUPLICATE_VOTE', message: '同一题目只能投一票' },
        }
        sendJson(res, 409, entry.response)
        return
      }
      if (scenario === 'rate-limited') {
        entry.status = 429
        entry.response = {
          request_id: `mock-${Date.now()}`,
          error: { code: 'ARENA_RATE_LIMITED', message: '投票过于频繁，请稍后再试' },
        }
        sendJson(res, 429, entry.response)
        return
      }
      if (scenario === 'network') {
        entry.status = 0
        entry.response = { error: 'mock: socket destroyed to simulate network failure' }
        res.destroy()
        return
      }

      const item = log.items[String(body?.item_id ?? '')]
      const candidate = item?.candidates.find((entryCandidate) => entryCandidate.anon_id === body?.anon_id)
      if (!item || !candidate) {
        entry.status = 400
        entry.response = {
          request_id: `mock-${Date.now()}`,
          error: { code: 'ARENA_ANON_INVALID', message: '候选编号无效' },
        }
        sendJson(res, 400, entry.response)
        return
      }
      const stat = log.votes[candidate.model_id]
      stat.wins += 1
      stat.elo += 8
      for (const other of item.candidates) {
        if (other.anon_id === candidate.anon_id) continue
        const otherStat = log.votes[other.model_id]
        otherStat.losses += 1
        otherStat.elo -= 2
      }
      entry.status = 200
      entry.response = {
        request_id: `mock-${Date.now()}`,
        selected: {
          model_id: candidate.model_id,
          display_name: candidate.display_name,
          pred_class: candidate.pred_class,
        },
        elo_after: round4(stat.elo),
        games_applied: item.candidates.length - 1,
      }
      sendJson(res, 200, entry.response)
      return
    }

    if (url.pathname === '/api/v1/arena/leaderboard' && req.method === 'GET') {
      const entry = record({
        ts: Date.now(),
        method: req.method,
        path: url.pathname,
        kind: 'leaderboard',
        body: null,
        status: 200,
        response: null,
      })
      entry.response = buildLeaderboard(`mock-${Date.now()}`)
      sendJson(res, 200, entry.response)
      return
    }

    if (url.pathname === '/' || url.pathname === '/tests/f10/' || url.pathname === '/tests/f10') {
      res.writeHead(302, { Location: '/tests/f10/harness.html' })
      res.end()
      return
    }

    await serveStatic(url.pathname, res)
  }

  async function serveStatic(pathname, res) {
    const decoded = decodeURIComponent(pathname)
    const target = resolve(staticRoot, `.${normalize(decoded)}`)
    if (!target.startsWith(staticRoot + sep) && target !== staticRoot) {
      res.writeHead(403, CORS_HEADERS)
      res.end('forbidden')
      return
    }
    try {
      const content = await readFile(target)
      res.writeHead(200, {
        'Content-Type': CONTENT_TYPES[extname(target).toLowerCase()] || 'application/octet-stream',
        'Cache-Control': 'no-store',
        ...CORS_HEADERS,
      })
      res.end(content)
    } catch {
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8', ...CORS_HEADERS })
      res.end('not found')
    }
  }

  await new Promise((resolvePromise, reject) => {
    server.once('error', reject)
    server.listen(port, '127.0.0.1', resolvePromise)
  })

  return {
    server,
    log,
    async setScenario(name) {
      resetState(name)
      return log.scenario
    },
    async close() {
      await new Promise((resolvePromise) => server.close(resolvePromise))
    },
  }
}
