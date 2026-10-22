/**
 * F14 测试用「本地可控 mock API server」（不是真实后端，不进入 src/）。
 *
 * 只覆盖 F14 键盘-only 流程与 50 卡/6×CPU 验收需要的最小端点：
 *   GET  /api/v1/models
 *   POST /api/v1/classify            （单模型 + F5 实时输入共用）
 *   POST /api/v1/compare             （兜底，不被键盘流程触发）
 *   GET  /api/v1/feedback/stats      POST /api/v1/feedback
 *   GET  /api/v1/arena/next          POST /api/v1/arena/vote
 *   GET  /api/v1/arena/leaderboard   GET  /api/v1/bench/pk
 *
 * 场景：`POST/GET /__scenario?name=normal|slow`（slow 放慢 classify / arena next，
 * 用于在 reduced-motion 下检查 loading spinner）；`GET /__log` 输出请求日志。
 * 全部数据均为本文件显式构造的 mock，不访问真实网关 / worker / LLM。
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

const CLASSES = [
  'finance',
  'realty',
  'stocks',
  'education',
  'science',
  'society',
  'politics',
  'sports',
  'game',
  'entertainment',
]

const MODELS = [
  { model_id: 'rf', display_name: '随机森林', model_version: 'rf.pkl', pred: 'finance' },
  { model_id: 'fasttext', display_name: 'fastText', model_version: 'ft.pkl', pred: 'finance' },
  { model_id: 'bert', display_name: 'BERT', model_version: 'bert.pkl', pred: 'finance' },
  { model_id: 'distill', display_name: '蒸馏模型', model_version: 'distill.pkl', pred: 'stocks' },
  { model_id: 'llm', display_name: 'LLM', model_version: null, pred: 'finance' },
]

const TITLE = '北京二手房成交量创年内新高'

const BENCH_FIXTURE = {
  meta: {
    dataset: 'test',
    split: 'test',
    sample_size: 10000,
    generated_at: '2026-10-21T00:00:00Z',
    code_version: 'mock-f14',
    model_versions: { rf: 'rf.pkl', fasttext: 'ft.pkl', bert: 'bert.pkl', distill: 'distill.pkl' },
    metric_def: { macro_f1: 'macro', avg_latency_ms: 'mean', size_bytes: 'disk' },
    data_kind: 'offline',
  },
  models: [
    { model_id: 'rf', display_name: '随机森林', status: 'ok', metrics: { macro_f1: 0.92, accuracy: 0.93, avg_latency_ms: 12, size_bytes: 5_400_000 } },
    { model_id: 'fasttext', display_name: 'fastText', status: 'ok', metrics: { macro_f1: 0.9, accuracy: 0.91, avg_latency_ms: 8, size_bytes: 2_100_000 } },
    { model_id: 'bert', display_name: 'BERT', status: 'ok', metrics: { macro_f1: 0.95, accuracy: 0.96, avg_latency_ms: 220, size_bytes: 420_000_000 } },
  ],
  derived: [
    { kind: 'weighted_vote', display_name: '加权投票集成', status: 'ok', metrics: { macro_f1: 0.96, accuracy: 0.97, avg_latency_ms: 260, size_bytes: 0 }, note: '非独立模型' },
    { kind: 'oracle', display_name: 'oracle 参考上界', status: 'ok', metrics: { macro_f1: 0.99, accuracy: 0.99, avg_latency_ms: 0, size_bytes: 0 }, note: '参考上界' },
  ],
  missing_models: [],
}

function round4(value) {
  return Math.round(value * 10_000) / 10_000
}

function wilson(wins, n, z = 1.96) {
  if (n <= 0) return [0, 1]
  const p = wins / n
  const denominator = 1 + (z * z) / n
  const center = (p + (z * z) / (2 * n)) / denominator
  const margin = (z * Math.sqrt((p * (1 - p) + (z * z) / (4 * n)) / n)) / denominator
  return [Math.max(0, center - margin), Math.min(1, center + margin)]
}

function newAnonId(used) {
  for (let attempt = 0; attempt < 50; attempt += 1) {
    const value = `an_${randomBytes(8).toString('hex').slice(0, 12)}`
    if (!used.has(value)) return value
  }
  return `an_${randomBytes(16).toString('hex')}`
}

const sleep = (ms) => new Promise((resolvePromise) => setTimeout(resolvePromise, ms))

export async function createMockServer({ port, staticDir }) {
  const staticRoot = resolve(staticDir)
  const log = { requests: [], scenario: 'normal', items: {}, itemCounter: 0, feedbackTotal: 0 }
  const votes = Object.fromEntries(
    MODELS.map((model) => [model.model_id, { wins: model.model_id === 'rf' ? 2 : 1, losses: 1, elo: 1200 }]),
  )

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

  function buildClassify(body) {
    const modelId = body?.model === 'fasttext' ? 'fasttext' : body?.model === 'bert' ? 'bert' : 'rf'
    const model = MODELS.find((item) => item.model_id === modelId) || MODELS[0]
    const topK = [
      { class_id: model.pred, score: 0.91 },
      { class_id: 'stocks', score: 0.06 },
      { class_id: 'realty', score: 0.02 },
    ]
    return {
      request_id: `mock-${Date.now()}-${Math.round(Math.random() * 1e6)}`,
      model_id: model.model_id,
      model_version: model.model_version,
      pred_class: model.pred,
      latency_ms: 12 + Math.round(Math.random() * 20),
      scores: Object.fromEntries(topK.map((entry) => [entry.class_id, entry.score])),
      score_kind: 'raw_proba',
      top_k: topK,
      data_source: 'mock',
    }
  }

  function buildLeaderboard(requestId) {
    const entries = MODELS.map((model) => {
      const stat = votes[model.model_id]
      const total = stat.wins + stat.losses
      const [lower, upper] = wilson(stat.wins, total)
      return {
        model_id: model.model_id,
        display_name: model.display_name,
        elo: round4(stat.elo),
        votes: stat.wins,
        sample_size: total,
        wins: stat.wins,
        losses: stat.losses,
        win_rate: total ? round4(stat.wins / total) : null,
        wilson_lower: round4(lower),
        wilson_upper: round4(upper),
        warming_up: total < 50,
        rank: null,
      }
    })
    return {
      request_id: requestId,
      status: 'warming_up',
      data_kind: 'vote',
      note: '人类偏好投票强度，仅作演示参考；票数可被本机清除。',
      min_votes_for_ranking: 50,
      entries,
    }
  }

  const server = http.createServer((req, res) => {
    void handle(req, res).catch((error) => {
      try {
        sendJson(res, 500, { error: { code: 'MOCK_SERVER_ERROR', message: String(error) } })
      } catch {
        // 连接已关闭。
      }
    })
  })

  async function handle(req, res) {
    const url = new URL(req.url || '/', `http://127.0.0.1:${port}`)
    if (req.method === 'OPTIONS') {
      res.writeHead(204, CORS_HEADERS)
      res.end()
      return
    }

    if (url.pathname === '/__scenario') {
      const name = url.searchParams.get('name') || ''
      if (name !== 'normal' && name !== 'slow') {
        sendJson(res, 400, { ok: false, error: `unknown scenario: ${name}` })
        return
      }
      log.scenario = name
      sendJson(res, 200, { ok: true, scenario: name })
      return
    }

    if (url.pathname === '/__log') {
      sendJson(res, 200, log)
      return
    }

    const entry = record({ ts: Date.now(), method: req.method, path: url.pathname, body: null })

    if (url.pathname === '/api/v1/models' && req.method === 'GET') {
      entry.status = 200
      sendJson(res, 200, {
        request_id: 'mock-models',
        default_model: 'rf',
        classes: CLASSES,
        models: MODELS.map((model) => ({
          model_id: model.model_id,
          display_name: model.display_name,
          enabled: true,
          worker_id: `w-${model.model_id}`,
          model_version: model.model_version,
          status: 'up',
        })),
      })
      return
    }

    if (url.pathname === '/api/v1/classify' && req.method === 'POST') {
      entry.body = await readBody(req)
      if (log.scenario === 'slow') await sleep(400)
      entry.status = 200
      sendJson(res, 200, buildClassify(entry.body))
      return
    }

    if (url.pathname === '/api/v1/compare' && req.method === 'POST') {
      entry.body = await readBody(req)
      entry.status = 200
      sendJson(res, 200, {
        request_id: `mock-compare-${Date.now()}`,
        summary: { total: 1, ok: 1, failed: 0, status: 'ok' },
        results: [buildClassify(entry.body)],
      })
      return
    }

    if (url.pathname === '/api/v1/feedback/stats' && req.method === 'GET') {
      entry.status = 200
      sendJson(res, 200, {
        request_id: 'mock-feedback-stats',
        total: log.feedbackTotal,
        pending: log.feedbackTotal,
        by_class: {},
        by_model: {},
        similar_hits: 0,
        retention_days: 30,
      })
      return
    }

    if (url.pathname === '/api/v1/feedback' && req.method === 'POST') {
      entry.body = await readBody(req)
      log.feedbackTotal += 1
      entry.status = 200
      sendJson(res, 200, {
        request_id: `mock-feedback-${Date.now()}`,
        feedback_id: log.feedbackTotal,
        status: 'pending',
        total: log.feedbackTotal,
        message: '反馈已提交，待审核',
      })
      return
    }

    if (url.pathname === '/api/v1/arena/next' && req.method === 'GET') {
      if (log.scenario === 'slow') await sleep(700)
      log.itemCounter += 1
      const used = new Set()
      const itemId = `item_${log.itemCounter}_${randomBytes(6).toString('hex')}`
      const candidates = MODELS.map((model) => {
        const anonId = newAnonId(used)
        used.add(anonId)
        return {
          anon_id: anonId,
          pred_class: model.pred,
          latency_ms: 90 + Math.round(Math.random() * 200),
          model_id: model.model_id,
          display_name: model.display_name,
        }
      })
      log.items[itemId] = { item_id: itemId, title: TITLE, candidates }
      entry.status = 200
      sendJson(res, 200, {
        request_id: `mock-arena-${Date.now()}`,
        item_id: itemId,
        title: TITLE,
        candidates: candidates.map((candidate) => ({
          anon_id: candidate.anon_id,
          pred_class: candidate.pred_class,
          latency_ms: candidate.latency_ms,
        })),
        partial: false,
      })
      return
    }

    if (url.pathname === '/api/v1/arena/vote' && req.method === 'POST') {
      entry.body = await readBody(req)
      const item = log.items[entry.body?.item_id]
      const candidate = item?.candidates.find((entryCandidate) => entryCandidate.anon_id === entry.body?.anon_id)
      if (!candidate) {
        entry.status = 400
        sendJson(res, 400, {
          request_id: `mock-vote-${Date.now()}`,
          error: { code: 'ARENA_ANON_INVALID', message: '候选编号无效' },
        })
        return
      }
      votes[candidate.model_id].wins += 1
      votes[candidate.model_id].elo += 8
      entry.status = 200
      sendJson(res, 200, {
        request_id: `mock-vote-${Date.now()}`,
        selected: {
          model_id: candidate.model_id,
          display_name: candidate.display_name,
          pred_class: candidate.pred_class,
        },
        elo_after: round4(votes[candidate.model_id].elo),
        games_applied: item.candidates.length - 1,
      })
      return
    }

    if (url.pathname === '/api/v1/arena/leaderboard' && req.method === 'GET') {
      entry.status = 200
      sendJson(res, 200, buildLeaderboard(`mock-board-${Date.now()}`))
      return
    }

    if (url.pathname === '/api/v1/bench/pk' && req.method === 'GET') {
      entry.status = 200
      sendJson(res, 200, { request_id: 'mock-bench', ...BENCH_FIXTURE })
      return
    }

    if (url.pathname === '/' || url.pathname === '/tests/f14/' || url.pathname === '/tests/f14') {
      res.writeHead(302, { Location: '/tests/f14/harness.html' })
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
      log.scenario = name
      return name
    },
    async close() {
      await new Promise((resolvePromise) => server.close(resolvePromise))
    },
  }
}
