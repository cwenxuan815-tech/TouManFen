/**
 * F17 批量分类前端验收 mock API（本地、无真实服务/LLM）。
 *
 * 仅用于 tests/f17；响应结构按阶段 2A 后端契约构造。
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
  { model_id: 'rf', display_name: '随机森林', enabled: true, model_version: null, status: 'up' },
  { model_id: 'fasttext', display_name: 'fastText', enabled: true, model_version: null, status: 'up' },
  { model_id: 'bert', display_name: 'BERT', enabled: true, model_version: null, status: 'up' },
  { model_id: 'llm', display_name: '大模型', enabled: true, model_version: null, status: 'up' },
  { model_id: 'distill', display_name: '蒸馏 BiLSTM', enabled: true, model_version: null, status: 'up' },
]

const sleep = (ms) => new Promise((resolvePromise) => setTimeout(resolvePromise, ms))

export async function createMockServer({ port, staticDir }) {
  const staticRoot = resolve(staticDir)
  const log = {
    scenario: 'normal',
    requests: [],
    batchCalls: 0,
    batchActive: 0,
    batchMaxActive: 0,
    feedbackStatsCalls: 0,
  }

  function reset(scenario) {
    log.scenario = scenario
    log.requests.length = 0
    log.batchCalls = 0
    log.batchActive = 0
    log.batchMaxActive = 0
    log.feedbackStatsCalls = 0
  }

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

  function requestId(prefix) {
    return `${prefix}-${Date.now().toString(36)}-${randomBytes(3).toString('hex')}`
  }

  function singleResponse(body) {
    return {
      request_id: requestId('mock-single'),
      model_id: body?.model || 'fasttext',
      model_version: null,
      pred_class: 'finance',
      latency_ms: 12,
      scores: { finance: 0.9, sports: 0.1 },
      score_kind: 'raw_proba',
      top_k: [
        { class_id: 'finance', score: 0.9 },
        { class_id: 'sports', score: 0.1 },
      ],
      rejected: false,
      reject_reason: null,
      threshold_meta: null,
      human_feedback_hint: null,
      data_source: 'mock',
    }
  }

  function batchResult(item, index, failed) {
    if (failed) {
      return {
        item_id: item.item_id,
        index,
        status: 'error',
        latency_ms: 0,
        error: { code: 'WORKER_UNAVAILABLE', message: 'mock 模型暂时不可用' },
      }
    }
    const pred = index % 2 === 0 ? 'finance' : 'stocks'
    return {
      item_id: item.item_id,
      index,
      status: 'ok',
      pred_class: pred,
      latency_ms: 7 + index,
      model_version: null,
      data_source: 'mock',
      scores: { [pred]: 0.82, finance: 0.18 },
      score_kind: 'raw_proba',
      top_k: [
        { class_id: pred, score: 0.82 },
        { class_id: 'finance', score: 0.18 },
      ],
      rejected: false,
      reject_reason: null,
      threshold_meta: null,
    }
  }

  async function handleBatch(req, res) {
    const body = await readBody(req)
    const entry = record({
      kind: 'batch',
      ts: Date.now(),
      body,
      status: 0,
      response: null,
    })
    log.batchCalls += 1
    log.batchActive += 1
    log.batchMaxActive = Math.max(log.batchMaxActive, log.batchActive)
    try {
      if (log.scenario === 'network') {
        entry.status = 0
        entry.response = { error: 'mock: socket destroyed to simulate network failure' }
        res.destroy()
        return
      }
      if (log.scenario === 'llm' && body?.model === 'llm') {
        entry.status = 400
        entry.response = {
          request_id: requestId('mock-batch'),
          error: { code: 'BATCH_LLM_NOT_ALLOWED', message: '批量分类暂不支持大模型' },
        }
        sendJson(res, 400, entry.response)
        return
      }
      if (log.scenario === 'chunk-fail' && log.batchCalls >= 2) {
        entry.status = 503
        entry.response = {
          request_id: requestId('mock-batch'),
          error: { code: 'WORKER_UNAVAILABLE', message: 'mock 批量服务暂时不可用' },
        }
        sendJson(res, 503, entry.response)
        return
      }
      if (log.scenario === 'slow-second' && log.batchCalls >= 2) {
        await sleep(800)
      }
      if (log.scenario === 'slow-first' && log.batchCalls === 1) {
        await sleep(800)
      }

      const items = Array.isArray(body?.items) ? body.items : []
      const results = items.map((item, index) => {
        const failed =
          log.scenario === 'all-fail'
          || (log.scenario === 'partial' && String(item.title).includes('FAIL'))
        return batchResult(item, index, failed)
      })
      const ok = results.filter((item) => item.status === 'ok').length
      const failed = results.length - ok
      const status = failed === 0 ? 'ok' : ok === 0 ? 'all_failed' : 'partial'
      entry.status = 200
      entry.response = {
        request_id: requestId('mock-batch'),
        batch_id: body?.batch_id ?? null,
        model_id: body?.model || 'fasttext',
        summary: {
          total: results.length,
          ok,
          failed,
          status,
          elapsed_ms: 12,
          total_timeout_ms: 30000,
        },
        results,
      }
      sendJson(res, 200, entry.response)
    } finally {
      log.batchActive -= 1
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
      reset(String(payload?.name || 'normal'))
      sendJson(res, 200, { ok: true, scenario: log.scenario })
      return
    }
    if (url.pathname === '/__log') {
      sendJson(res, 200, log)
      return
    }
    if (url.pathname === '/api/v1/models' && req.method === 'GET') {
      sendJson(res, 200, {
        request_id: requestId('mock-models'),
        default_model: 'fasttext',
        classes: ['finance', 'realty', 'stocks', 'education', 'science', 'society', 'politics', 'sports', 'game', 'entertainment'],
        models: MODELS,
      })
      return
    }
    if (url.pathname === '/api/v1/feedback/stats' && req.method === 'GET') {
      log.feedbackStatsCalls += 1
      sendJson(res, 200, {
        request_id: requestId('mock-feedback'),
        total: 0,
        pending: 0,
        by_class: {},
        by_model: {},
        similar_hits: 0,
        retention_days: 30,
      })
      return
    }
    if (url.pathname === '/api/v1/feedback' && req.method === 'POST') {
      sendJson(res, 200, {
        request_id: requestId('mock-feedback'),
        feedback_id: 1,
        status: 'pending',
        total: 1,
        message: '反馈已提交，待审核',
      })
      return
    }
    if (url.pathname === '/api/v1/classify' && req.method === 'POST') {
      const body = await readBody(req)
      const entry = record({ kind: 'classify', ts: Date.now(), body, status: 200, response: null })
      entry.response = singleResponse(body)
      sendJson(res, 200, entry.response)
      return
    }
    if (url.pathname === '/api/v1/classify/batch' && req.method === 'POST') {
      await handleBatch(req, res)
      return
    }

    // 静态 harness 文件
    const requested = normalize(url.pathname).replace(/^([/\\])+/, '')
    const filePath = resolve(staticRoot, requested)
    if (!filePath.startsWith(staticRoot + sep) && filePath !== staticRoot) {
      res.writeHead(403)
      res.end('forbidden')
      return
    }
    try {
      const data = await readFile(filePath)
      const type = CONTENT_TYPES[extname(filePath)] || 'application/octet-stream'
      res.writeHead(200, { 'Content-Type': type })
      res.end(data)
    } catch {
      res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' })
      res.end('not found')
    }
  }

  const server = http.createServer((req, res) => {
    void handleRequest(req, res).catch((error) => {
      try {
        sendJson(res, 500, { error: { code: 'MOCK_SERVER_ERROR', message: String(error) } })
      } catch {
        // 连接已关闭
      }
    })
  })

  await new Promise((resolvePromise, reject) => {
    server.once('error', reject)
    server.listen(port, '127.0.0.1', resolvePromise)
  })

  return {
    log,
    close: () => new Promise((resolvePromise) => server.close(resolvePromise)),
  }
}
