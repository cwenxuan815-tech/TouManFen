/**
 * F20 阶段 5C 本地 mock：访问 API + 最小业务 API + 静态 harness。
 * 不连接真实网关/worker/LLM；日志只记录 ticket 是否存在，不保存 raw ticket。
 */
import { createServer } from 'node:http'
import { createReadStream, existsSync, statSync } from 'node:fs'
import { extname, join, normalize, resolve } from 'node:path'

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.woff2': 'font/woff2',
}

const MODELS = [
  ['rf', '随机森林'],
  ['fasttext', 'fastText'],
  ['bert', 'BERT'],
  ['llm', '大模型'],
  ['distill', '蒸馏 BiLSTM'],
].map(([model_id, display_name]) => ({
  model_id,
  display_name,
  enabled: true,
  worker_id: model_id,
  model_version: null,
  status: 'up',
}))

const ACCESS_HEADER = 'x-access-ticket'

function json(res, status, body) {
  const text = JSON.stringify(body)
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
  })
  res.end(text)
}

function readBody(req) {
  return new Promise((resolvePromise) => {
    let body = ''
    req.on('data', (chunk) => {
      body += chunk
    })
    req.on('end', () => {
      try {
        resolvePromise(body ? JSON.parse(body) : {})
      } catch {
        resolvePromise({})
      }
    })
  })
}

function error(res, status, code, message, details) {
  json(res, status, {
    request_id: 'f20-mock',
    error: { code, message, ...(details ? { details } : {}) },
  })
}

export async function createMockServer({ port, staticDir }) {
  const staticRoot = resolve(staticDir)
  const log = {
    access: [],
    business: [],
    accessEnter: 0,
    accessStatus: 0,
    accessClaim: 0,
    accessLeave: 0,
    accessCancel: 0,
    modelsCalls: 0,
    demoCalls: 0,
  }
  let scenario = 'ready'
  let startedAt = Date.now()
  let statusCalls = 0
  let ticketSeq = 0
  let activeTicket = ''
  let readyExpired = false

  function reset(nextScenario) {
    scenario = nextScenario
    startedAt = Date.now()
    statusCalls = 0
    ticketSeq = 0
    activeTicket = ''
    readyExpired = false
    log.access.length = 0
    log.business.length = 0
    for (const key of Object.keys(log)) {
      if (typeof log[key] === 'number') log[key] = 0
    }
  }

  function issueTicket() {
    if (!activeTicket) activeTicket = `f20-opaque-${++ticketSeq}`
    return activeTicket
  }

  function accessBody(state, extra = {}) {
    return {
      request_id: 'f20-access',
      state,
      ticket: state === 'active' || state === 'ready' || state === 'queued' ? issueTicket() : null,
      ...extra,
    }
  }

  function elapsed() {
    return Date.now() - startedAt
  }

  async function handleAccess(req, res, path) {
    const hasTicket = Boolean(req.headers[ACCESS_HEADER])
    log.access.push({ method: req.method, path, hasTicket, at: Date.now() })
    if (req.method === 'POST' && path === '/api/v1/access/enter') {
      log.accessEnter += 1
      if (scenario === 'disabled') {
        error(res, 404, 'ACCESS_DISABLED', '访问控制未启用。')
        return
      }
      if (scenario === 'old_ticket' && hasTicket) {
        error(res, 403, 'ACCESS_TICKET_INVALID', '访问票据无效或已过期。')
        return
      }
      if (scenario === 'queue_full') {
        error(res, 429, 'ACCESS_QUEUE_FULL', '排队人数已满。', { state: 'queue_full' })
        return
      }
      if (scenario === 'unavailable') {
        error(res, 503, 'ACCESS_UNAVAILABLE', '访问管理暂时不可用。')
        return
      }
      if (scenario === 'identity') {
        error(res, 403, 'ACCESS_IDENTITY_INVALID', '无法确认当前网络身份。')
        return
      }
      if (scenario === 'ready') {
        json(res, 200, accessBody('ready', { remaining_seconds: 15 }))
        return
      }
      if (scenario === 'ready_expire') {
        if (readyExpired) {
          json(res, 200, accessBody('active', { remaining_seconds: 4 }))
        } else {
          json(res, 200, accessBody('ready', { remaining_seconds: 15 }))
        }
        return
      }
      if (scenario === 'queued' || scenario === 'queued_promote' || scenario === 'throttle') {
        json(res, 200, accessBody('queued', {
          position: 3,
          ahead: 2,
          remaining_seconds: 120,
        }))
        return
      }
      if (scenario === 'cooling') {
        json(res, 200, accessBody('cooling', { remaining_seconds: 1.5 }))
        return
      }
      if (scenario === 'expire') {
        json(res, 200, accessBody('active', { remaining_seconds: 2 }))
        return
      }
      json(res, 200, accessBody('active', { remaining_seconds: 4 }))
      return
    }

    if (req.method === 'GET' && path === '/api/v1/access/status') {
      log.accessStatus += 1
      statusCalls += 1
      if (!hasTicket) {
        error(res, 403, 'ACCESS_TICKET_INVALID', '缺少访问票据。')
        return
      }
      if (scenario === 'throttle' && statusCalls === 1) {
        error(res, 429, 'ACCESS_POLL_THROTTLED', '状态轮询过于频繁。', {
          retry_after_seconds: 1.2,
        })
        return
      }
      if (scenario === 'queued_promote' && statusCalls >= 2) {
        json(res, 200, accessBody('ready', { remaining_seconds: 15 }))
        return
      }
      if (scenario === 'cooling' && elapsed() > 1400) {
        json(res, 200, accessBody('ready', { remaining_seconds: 15 }))
        return
      }
      if (scenario === 'expire' && elapsed() > 2000) {
        json(res, 200, accessBody('cooling', { remaining_seconds: 2 }))
        return
      }
      if (scenario === 'queued' || scenario === 'queued_promote' || scenario === 'throttle') {
        json(res, 200, accessBody('queued', {
          position: 3,
          ahead: 2,
          remaining_seconds: 120,
        }))
        return
      }
      if (scenario === 'cooling') {
        json(res, 200, accessBody('cooling', { remaining_seconds: 1 }))
        return
      }
      if (scenario === 'expire') {
        json(res, 200, accessBody('active', { remaining_seconds: 2 }))
        return
      }
      json(res, 200, accessBody('active', { remaining_seconds: 4 }))
      return
    }

    if (req.method === 'POST' && path === '/api/v1/access/claim') {
      log.accessClaim += 1
      if (!hasTicket) {
        error(res, 403, 'ACCESS_TICKET_INVALID', '缺少访问票据。')
        return
      }
      if (scenario === 'ready_expire') {
        readyExpired = true
        error(res, 403, 'ACCESS_TICKET_INVALID', 'ready 15 秒已过。')
        return
      }
      json(res, 200, accessBody('active', { remaining_seconds: 4 }))
      return
    }

    if (req.method === 'POST' && path === '/api/v1/access/leave') {
      log.accessLeave += 1
      if (!hasTicket) {
        error(res, 403, 'ACCESS_TICKET_INVALID', '缺少访问票据。')
        return
      }
      json(res, 200, { request_id: 'f20-access', state: 'cancelled' })
      return
    }

    if (req.method === 'POST' && path === '/api/v1/access/cancel') {
      log.accessCancel += 1
      if (!hasTicket) {
        error(res, 403, 'ACCESS_TICKET_INVALID', '缺少访问票据。')
        return
      }
      json(res, 200, { request_id: 'f20-access', state: 'cancelled' })
      return
    }

    error(res, 404, 'NOT_FOUND', '未知访问路径。')
  }

  async function handleBusiness(req, res, path) {
    const hasTicket = Boolean(req.headers[ACCESS_HEADER])
    log.business.push({ method: req.method, path, hasTicket })
    if (path === '/api/v1/models') {
      log.modelsCalls += 1
      json(res, 200, {
        request_id: 'f20-models',
        default_model: 'fasttext',
        classes: [
          'finance', 'realty', 'stocks', 'education', 'science',
          'society', 'politics', 'sports', 'game', 'entertainment',
        ],
        models: MODELS,
      })
      return
    }
    if (path === '/api/v1/demo/state') {
      log.demoCalls += 1
      json(res, 200, {
        request_id: 'f20-demo',
        data_source: 'unknown',
        ready_models: 0,
        total_models: 5,
        mixed: false,
        reason: 'mock',
        source_counts: { unknown: 5 },
        worker_sources: [],
      })
      return
    }
    if (path === '/api/v1/compare' && req.method === 'POST') {
      json(res, 200, {
        request_id: 'f20-compare',
        title: 'F20 mock compare',
        total: 1,
        ok: 1,
        failed: 0,
        elapsed_ms: 1,
        total_timeout_ms: 1000,
        partial: false,
        results: [{
          model_id: 'rf',
          status: 'ok',
          latency_ms: 1,
          pred_class: 'finance',
          pred_label: '财经',
          model_version: null,
          error: null,
          data_source: 'mock',
        }],
      })
      return
    }
    if (path === '/api/v1/compare/stream') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' })
      res.end(': f20 unsupported\n\n')
      return
    }
    json(res, 404, { request_id: 'f20', error: { code: 'NOT_FOUND', message: 'not found' } })
  }

  const server = createServer(async (req, res) => {
    const url = new URL(req.url || '/', `http://127.0.0.1:${port}`)
    if (url.pathname === '/__scenario' && req.method === 'POST') {
      const body = await readBody(req)
      reset(String(body.scenario || 'ready'))
      json(res, 200, { ok: true, scenario })
      return
    }
    if (url.pathname === '/__log' && req.method === 'GET') {
      json(res, 200, log)
      return
    }
    if (url.pathname.startsWith('/api/v1/access/')) {
      await handleAccess(req, res, url.pathname)
      return
    }
    if (url.pathname.startsWith('/api/v1/')) {
      await handleBusiness(req, res, url.pathname)
      return
    }

    const relative = url.pathname === '/' ? 'index.html' : url.pathname.replace(/^\/+/, '')
    const filePath = normalize(join(staticRoot, relative))
    if (!filePath.startsWith(staticRoot) || !existsSync(filePath) || !statSync(filePath).isFile()) {
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' })
      res.end('not found')
      return
    }
    res.writeHead(200, {
      'Content-Type': MIME[extname(filePath)] || 'application/octet-stream',
      'Cache-Control': 'no-store',
    })
    createReadStream(filePath).pipe(res)
  })

  await new Promise((resolvePromise, reject) => {
    server.once('error', reject)
    server.listen(port, '127.0.0.1', resolvePromise)
  })

  return {
    close: () =>
      new Promise((resolvePromise) => server.close(() => resolvePromise())),
  }
}
