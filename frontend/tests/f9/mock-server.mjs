/**
 * F9 测试用「本地可控 mock SSE server」（不是真实后端，不进入 src/）。
 *
 * 用途：在浏览器里真实跑 `ResultStage + useCompareStream + CompareBoard`，
 * 覆盖四类场景；所有响应均由本文件显式构造，标明 mock。
 *
 * 场景由 `title` 决定（前端只发送 title/models，故用 title 作为 mock 控制通道）：
 *   f9-normal        5 个模型按序返回 + 1 条心跳 + 1 条无效 JSON + 1 条重复 model
 *   f9-llm-offline   LLM 返回 event: model status=error，其余 4 个成功
 *   f9-switch        模型慢速返回（用于中途切面板，验证连接被关闭）
 *   f9-fallback      只发心跳注释、永不发事件（触发 3s 无首事件回退 POST /compare）
 *   f9-stream-error  先返回 1 个 model，再发流级 event: error
 *   f9-connect-error SSE 端点直接 503（EventSource 连接失败 → 立即回退 POST）
 *
 * 请求日志保存在返回对象的 `requests` / `streamEvents`，供验收脚本输出证据。
 */
import http from 'node:http'
import { readFile } from 'node:fs/promises'
import { extname, join, normalize, resolve, sep } from 'node:path'

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

const MODEL_IDS = ['rf', 'fasttext', 'bert', 'distill', 'llm']

function now() {
  return Date.now()
}

function modelEvent(requestId, modelId, status, latencyMs, predClass, error = null) {
  return {
    request_id: requestId,
    model_id: modelId,
    status,
    latency_ms: latencyMs,
    model_version: status === 'ok' ? `${modelId}.pkl` : null,
    pred_class: status === 'ok' ? predClass : null,
    error: status === 'ok' ? null : error,
  }
}

function summaryEvent(requestId, status, total, ok, failed, elapsedMs) {
  return {
    request_id: requestId,
    status,
    total,
    ok,
    failed,
    elapsed_ms: elapsedMs,
    total_timeout_ms: 18_000,
    partial: status === 'partial',
  }
}

function doneEvent(requestId, status, total, ok, failed) {
  return { request_id: requestId, status, total, ok, failed }
}

/** 模拟 POST /compare 完整结果（回退路径）。 */
function buildCompareResponse(title, models) {
  const requested = models.length ? models : MODEL_IDS
  const requestId = `mock-post-${Date.now()}`
  const predicted = { rf: 'finance', fasttext: 'finance', bert: 'finance', distill: 'stocks', llm: 'finance' }
  const latency = { rf: 90, fasttext: 110, bert: 220, distill: 300, llm: 1400 }
  const results = requested.map((modelId) => ({
    model_id: modelId,
    status: 'ok',
    model_version: `${modelId}.pkl`,
    pred_class: predicted[modelId] || 'finance',
    latency_ms: latency[modelId] || 150,
    error: null,
    data_source: 'mock',
  }))
  return {
    request_id: requestId,
    summary: { total: requested.length, ok: requested.length, failed: 0, status: 'ok' },
    results,
    title,
    data_source: 'mock',
  }
}

export async function createMockServer({ port, staticDir }) {
  const requests = []
  const streamEvents = []
  const staticRoot = resolve(staticDir)

  const server = http.createServer((req, res) => {
    void handleRequest(req, res).catch((error) => {
      try {
        res.writeHead(500, { 'Content-Type': 'application/json; charset=utf-8', ...CORS_HEADERS })
        res.end(JSON.stringify({ error: { code: 'MOCK_SERVER_ERROR', message: String(error) } }))
      } catch {
        // 连接已关闭，忽略。
      }
    })
  })

  async function handleRequest(req, res) {
    const url = new URL(req.url || '/', `http://127.0.0.1:${port}`)
    if (req.method === 'OPTIONS') {
      res.writeHead(204, CORS_HEADERS)
      res.end()
      return
    }

    if (url.pathname === '/__log') {
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', ...CORS_HEADERS })
      res.end(JSON.stringify({ requests, streamEvents }, null, 2))
      return
    }

    if (url.pathname === '/api/v1/compare/stream') {
      handleStream(req, res, url)
      return
    }

    if (url.pathname === '/api/v1/compare' && req.method === 'POST') {
      let body = ''
      req.on('data', (chunk) => {
        body += chunk
      })
      req.on('end', () => {
        let parsed = null
        try {
          parsed = JSON.parse(body || '{}')
        } catch {
          parsed = null
        }
        const entry = {
          ts: now(),
          method: req.method,
          url: req.url,
          kind: 'compare',
          path: url.pathname,
          title: parsed?.title ?? null,
          models: parsed?.models ?? [],
          headers: req.headers,
          body,
        }
        requests.push(entry)
        const response = buildCompareResponse(parsed?.title ?? '', Array.isArray(parsed?.models) ? parsed.models : [])
        const delay = Number(url.searchParams.get('delay') || 250)
        setTimeout(() => {
          try {
            res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', ...CORS_HEADERS })
            res.end(JSON.stringify(response))
            entry.respondedAt = now()
          } catch {
            // 连接已关闭。
          }
        }, delay)
      })
      return
    }

    if (url.pathname === '/api/v1/models') {
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', ...CORS_HEADERS })
      res.end(JSON.stringify({ request_id: 'mock-models', default_model: 'rf', classes: [], models: [] }))
      return
    }

    if (url.pathname === '/' || url.pathname === '/tests/f9/' || url.pathname === '/tests/f9') {
      res.writeHead(302, { Location: '/tests/f9/harness.html' })
      res.end()
      return
    }

    await serveStatic(url.pathname, res)
  }

  function handleStream(req, res, url) {
    const title = url.searchParams.get('title') || ''
    const modelIds = (url.searchParams.get('models') || '')
      .split(',')
      .map((item) => decodeURIComponent(item))
      .filter(Boolean)
    const requestId = `mock-stream-${title}`
    const entry = {
      ts: now(),
      method: req.method,
      url: req.url,
      kind: 'stream',
      path: url.pathname,
      title,
      models: modelIds,
      headers: req.headers,
      openedAt: now(),
      closedAt: null,
      status: 200,
    }
    requests.push(entry)

    if (title === 'f9-connect-error') {
      entry.status = 503
      res.writeHead(503, { 'Content-Type': 'application/json; charset=utf-8', ...CORS_HEADERS })
      res.end(JSON.stringify({ error: { code: 'INTERNAL_ERROR', message: 'mock: stream unavailable' } }))
      entry.respondedAt = now()
      return
    }

    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
      'X-Accel-Buffering': 'no',
      ...CORS_HEADERS,
    })

    const timers = []
    const intervals = []
    let finished = false
    let clientClosed = false

    const markClosed = () => {
      if (entry.closedAt === null) entry.closedAt = now()
      clientClosed = true
      for (const timer of timers) clearTimeout(timer)
      for (const interval of intervals) clearInterval(interval)
    }
    req.on('close', markClosed)
    res.on('close', () => {
      if (entry.closedAt === null) entry.closedAt = now()
    })

    const write = (chunk) => {
      if (finished || clientClosed || res.writableEnded || res.destroyed) return false
      try {
        res.write(chunk)
        return true
      } catch {
        return false
      }
    }
    const comment = (text) => write(`: ${text}\n\n`)
    const send = (eventName, data) => {
      const ok = write(`event: ${eventName}\ndata: ${JSON.stringify(data)}\n\n`)
      if (ok) {
        streamEvents.push({
          ts: now(),
          scenario: title,
          requestId,
          event: eventName,
          model_id: data && typeof data === 'object' ? data.model_id ?? null : null,
          status: data && typeof data === 'object' ? data.status ?? null : null,
        })
      }
      return ok
    }
    const schedule = (delay, fn) => {
      timers.push(setTimeout(() => {
        if (!clientClosed) fn()
      }, delay))
    }
    const finish = () => {
      if (finished) return
      finished = true
      entry.finishedAt = now()
      try {
        res.end()
      } catch {
        // ignore
      }
    }

    comment('heartbeat')

    if (title === 'f9-normal') {
      // 无效 JSON：必须被丢弃，且不能算作「有效首事件”。
      schedule(40, () => write('event: model\ndata: {"oops": \n\n'))
      // 间隔 400ms：便于在浏览器里截到「逐项落位 / LLM 仍在等待中」的中间态。
      schedule(100, () => send('model', modelEvent(requestId, 'rf', 'ok', 90, 'finance')))
      schedule(500, () => send('model', modelEvent(requestId, 'fasttext', 'ok', 110, 'finance')))
      schedule(900, () => send('model', modelEvent(requestId, 'bert', 'ok', 220, 'finance')))
      schedule(1300, () => send('model', modelEvent(requestId, 'distill', 'ok', 300, 'stocks')))
      // 重复 model_id：必须按 model_id 去重，不覆盖先到的 latency=90。
      schedule(1700, () => send('model', modelEvent(requestId, 'rf', 'ok', 999, 'finance')))
      schedule(2100, () => send('model', modelEvent(requestId, 'llm', 'ok', 1400, 'finance')))
      schedule(2200, () => send('summary', summaryEvent(requestId, 'ok', 5, 5, 0, 2200)))
      schedule(2230, () => send('done', doneEvent(requestId, 'ok', 5, 5, 0)))
      schedule(2260, finish)
      return
    }

    if (title === 'f9-all-failed') {
      schedule(80, () =>
        send('model', modelEvent(requestId, 'rf', 'error', 0, null, {
          code: 'WORKER_TIMEOUT',
          message: '随机森林响应超时（mock）',
        })),
      )
      schedule(160, () =>
        send('model', modelEvent(requestId, 'bert', 'error', 0, null, {
          code: 'WORKER_UNAVAILABLE',
          message: 'BERT 服务不可达（mock）',
        })),
      )
      schedule(260, () => send('summary', summaryEvent(requestId, 'all_failed', 2, 0, 2, 260)))
      schedule(290, () => send('done', doneEvent(requestId, 'all_failed', 2, 0, 2)))
      schedule(320, finish)
      return
    }

    if (title === 'f9-llm-offline') {
      schedule(70, () => send('model', modelEvent(requestId, 'rf', 'ok', 90, 'finance')))
      schedule(150, () => send('model', modelEvent(requestId, 'fasttext', 'ok', 110, 'finance')))
      schedule(240, () => send('model', modelEvent(requestId, 'bert', 'ok', 220, 'finance')))
      schedule(330, () => send('model', modelEvent(requestId, 'distill', 'ok', 300, 'stocks')))
      schedule(420, () =>
        send(
          'model',
          modelEvent(requestId, 'llm', 'error', 0, null, {
            code: 'WORKER_UNAVAILABLE',
            message: 'LLM worker 不可达（mock）',
          }),
        ),
      )
      schedule(500, () => send('summary', summaryEvent(requestId, 'partial', 5, 4, 1, 500)))
      schedule(530, () => send('done', doneEvent(requestId, 'partial', 5, 4, 1)))
      schedule(560, finish)
      return
    }

    if (title === 'f9-switch') {
      schedule(100, () => send('model', modelEvent(requestId, 'rf', 'ok', 90, 'finance')))
      schedule(600, () => send('model', modelEvent(requestId, 'fasttext', 'ok', 110, 'finance')))
      schedule(1300, () => send('model', modelEvent(requestId, 'bert', 'ok', 220, 'finance')))
      schedule(2100, () => send('model', modelEvent(requestId, 'distill', 'ok', 300, 'stocks')))
      schedule(2900, () => send('model', modelEvent(requestId, 'llm', 'ok', 1400, 'finance')))
      schedule(3000, () => send('summary', summaryEvent(requestId, 'ok', 5, 5, 0, 3000)))
      schedule(3030, () => send('done', doneEvent(requestId, 'ok', 5, 5, 0)))
      schedule(3060, finish)
      return
    }

    if (title === 'f9-fallback') {
      // 只发心跳注释，永不发事件：前端应在 3s 后关闭连接并发 POST /compare。
      intervals.push(setInterval(() => comment('heartbeat'), 400))
      schedule(15_000, finish)
      return
    }

    if (title === 'f9-stream-error') {
      schedule(100, () => send('model', modelEvent(requestId, 'rf', 'ok', 90, 'finance')))
      schedule(350, () =>
        send('error', {
          request_id: requestId,
          code: 'INTERNAL_ERROR',
          message: 'mock: compare stream broke',
        }),
      )
      schedule(400, finish)
      return
    }

    // 未知 title：直接按 all_failed 收尾，避免悬挂连接。
    schedule(100, () => send('error', {
      request_id: requestId,
      code: 'INVALID_TITLE',
      message: `mock: unknown scenario title ${title}`,
    }))
    schedule(150, finish)
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
    requests,
    streamEvents,
    async close() {
      await new Promise((resolvePromise) => server.close(resolvePromise))
    },
  }
}
