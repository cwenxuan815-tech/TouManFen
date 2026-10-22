/**
 * F11 测试用「本地可控 mock bench API」（不是真实后端，不进入 src/）。
 *
 * GET /api/v1/bench/pk 按当前 scenario 返回：
 *   ready      → 完整 raw artifact（fixture，含 5 模型 + 集成 + oracle，LLM missing）
 *   wrapped    → {request_id, artifact_path, artifact:<fixture>}（包装层兼容）
 *   empty      → meta + 空 models/derived
 *   error      → HTTP 500 JSON error
 *   path-only  → 只有 {request_id, artifact_path}，不含指标数据（前端必须报错而非造假）
 *
 * 场景由测试脚本 POST /__scenario?name=xxx 切换，并记录在 `requests` 供验收输出。
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

const SCENARIOS = ['ready', 'wrapped', 'empty', 'error', 'path-only']

export async function createMockBenchServer({ port, staticDir, fixturePath }) {
  const fixture = JSON.parse(await readFile(fixturePath, 'utf8'))
  const staticRoot = resolve(staticDir)
  const requests = []
  let scenario = 'ready'

  const server = http.createServer((req, res) => {
    void handle(req, res).catch((error) => {
      try {
        res.writeHead(500, { 'Content-Type': 'application/json; charset=utf-8', ...CORS_HEADERS })
        res.end(JSON.stringify({ error: { code: 'MOCK_SERVER_ERROR', message: String(error) } }))
      } catch {
        // 连接已关闭，忽略。
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
      if (!SCENARIOS.includes(name)) {
        res.writeHead(400, { 'Content-Type': 'application/json; charset=utf-8', ...CORS_HEADERS })
        res.end(JSON.stringify({ ok: false, error: `unknown scenario: ${name}` }))
        return
      }
      scenario = name
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', ...CORS_HEADERS })
      res.end(JSON.stringify({ ok: true, scenario }))
      return
    }

    if (url.pathname === '/__log') {
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', ...CORS_HEADERS })
      res.end(JSON.stringify({ scenario, requests, fixture }))
      return
    }

    if (url.pathname === '/api/v1/bench/pk') {
      const entry = {
        ts: Date.now(),
        method: req.method,
        url: req.url,
        scenario,
        headers: req.headers,
      }
      requests.push(entry)
      entry.scenario = scenario
      if (scenario === 'error') {
        entry.status = 500
        res.writeHead(500, { 'Content-Type': 'application/json; charset=utf-8', ...CORS_HEADERS })
        res.end(
          JSON.stringify({
            request_id: 'mock-bench-error',
            error: {
              code: 'BENCH_UNAVAILABLE',
              message: 'mock: benchmark artifact unavailable',
            },
          }),
        )
        return
      }
      let payload
      if (scenario === 'wrapped') {
        payload = {
          request_id: 'mock-bench-wrapped',
          artifact_path: fixture.artifact_path,
          artifact: fixture,
        }
      } else if (scenario === 'empty') {
        payload = {
          request_id: 'mock-bench-empty',
          meta: fixture.meta,
          models: [],
          derived: [],
          missing_models: [],
        }
      } else if (scenario === 'path-only') {
        payload = {
          request_id: 'mock-bench-path-only',
          artifact_path: fixture.artifact_path,
        }
      } else {
        payload = fixture
      }
      entry.status = 200
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8', ...CORS_HEADERS })
      res.end(JSON.stringify(payload))
      return
    }

    if (url.pathname === '/' || url.pathname === '/tests/f11/' || url.pathname === '/tests/f11') {
      res.writeHead(302, { Location: '/tests/f11/harness.html' })
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
    fixture,
    get scenario() {
      return scenario
    },
    async close() {
      await new Promise((resolvePromise) => server.close(resolvePromise))
    },
  }
}
