/**
 * F18 阶段 3A：在 Edge/Chromium 中执行 titleFiles 纯函数断言。
 * 不读取真实用户文件，不访问网关，不调用 LLM。
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdir, readFile, rm } from 'node:fs/promises'
import { createServer } from 'node:http'
import { dirname, extname, normalize, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import os from 'node:os'
import { build } from 'vite'

const here = dirname(fileURLToPath(import.meta.url))
const distDir = resolve(here, 'dist')
const SERVER_PORT = 18192
const CDP_PORT = 9343
const HARNESS_URL = `http://127.0.0.1:${SERVER_PORT}/tests/f18/harness.html`

const EDGE_CANDIDATES = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
]

const CONTENT_TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
}

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
      let message
      try {
        message = JSON.parse(String(event.data))
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

async function waitFor(fn, { timeout = 20_000, interval = 120, label = 'condition' } = {}) {
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

function startStaticServer() {
  const root = resolve(distDir)
  const server = createServer((req, res) => {
    const url = new URL(req.url || '/', `http://127.0.0.1:${SERVER_PORT}`)
    const requested = normalize(url.pathname).replace(/^([/\\])+/, '')
    const filePath = resolve(root, requested)
    if (!filePath.startsWith(root + sep) && filePath !== root) {
      res.writeHead(403)
      res.end('forbidden')
      return
    }
    readFile(filePath)
      .then((data) => {
        res.writeHead(200, {
          'Content-Type': CONTENT_TYPES[extname(filePath)] || 'application/octet-stream',
        })
        res.end(data)
      })
      .catch(() => {
        res.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' })
        res.end('not found')
      })
  })
  return new Promise((resolvePromise, reject) => {
    server.once('error', reject)
    server.listen(SERVER_PORT, '127.0.0.1', () => resolvePromise(server))
  })
}

async function main() {
  console.log(`[build] 构建 F18 harness（${distDir}）`)
  await build({ configFile: resolve(here, 'vite.harness.config.mjs'), logLevel: 'warn' })
  console.log('[build] OK')

  const server = await startStaticServer()
  const profileDir = resolve(os.tmpdir(), `tmf-f18-edge-${process.pid}-${Date.now()}`)
  await mkdir(profileDir, { recursive: true })
  let edge = null
  let client = null
  let failures = 0
  let total = 0

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
      '--window-size=1200,900',
      'about:blank',
    ], { stdio: 'ignore', windowsHide: true })

    let version = null
    await waitFor(async () => {
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
    await client.send('Page.navigate', { url: `${HARNESS_URL}?t=${Date.now()}` })
    await waitFor(
      async () => evaluate(client, 'Boolean(window.__f18 && typeof window.__f18.run === "function")'),
      { timeout: 15_000, label: 'F18 harness ready' },
    )

    console.log(`\n=== F18 阶段 3A 文件解析断言（${version?.Browser || 'Edge'}） ===`)
    const result = await evaluate(client, 'window.__f18.run()')
    console.log(`[browser] ${JSON.stringify(result.browser)}`)
    for (const item of result.checks || []) {
      total += 1
      if (!item.pass) failures += 1
      console.log(`  ${item.pass ? '✓' : '✗'} ${item.name}${item.detail ? ` — ${item.detail}` : ''}`)
    }
    console.log(`\n总结果：${failures === 0 ? 'PASS' : 'FAIL'}（失败 ${failures} / ${total}）`)
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
    await new Promise((resolvePromise) => server.close(resolvePromise))
    await sleep(250)
    await rm(profileDir, { recursive: true, force: true }).catch(() => {})
  }
}

main().catch((error) => {
  console.error('[F18] 运行失败：', error.stack || error.message)
  process.exitCode = 1
})
