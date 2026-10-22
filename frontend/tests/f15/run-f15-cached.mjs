// F15 最小可复现验收：静态 dist + 拦截 /api/v1/models 模拟后端不可达，验证 cached 快照。
// 不启动真实后端、不调用 LLM、不触碰用户服务。
import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { spawn } from 'node:child_process'

const root = path.resolve(process.cwd())
const dist = path.join(root, 'dist')
const port = 18081
const debugPort = 19223
const profile = path.join(process.env.TEMP || '.', `tmf-f15-${Date.now()}`)
const edge = 'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe'
const sleep = (ms) => new Promise((r) => setTimeout(r, ms))
const types = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.json': 'application/json', '.svg': 'image/svg+xml', '.png': 'image/png' }

if (!fs.existsSync(path.join(dist, 'snapshot.json'))) throw new Error('dist/snapshot.json missing; run pnpm build')
const server = http.createServer((req, res) => {
  const urlPath = decodeURIComponent((req.url || '/').split('?')[0])
  let file = path.join(dist, urlPath === '/' ? 'index.html' : urlPath)
  if (!file.startsWith(dist) || !fs.existsSync(file) || fs.statSync(file).isDirectory()) file = path.join(dist, 'index.html')
  res.writeHead(200, { 'Content-Type': types[path.extname(file)] || 'application/octet-stream' })
  fs.createReadStream(file).pipe(res)
})
await new Promise((resolve) => server.listen(port, '127.0.0.1', resolve))
const browser = spawn(edge, ['--headless=new', '--disable-gpu', '--no-first-run', '--no-default-browser-check',
  '--disable-remote-fonts', '--hide-scrollbars', `--remote-debugging-port=${debugPort}`, `--user-data-dir=${profile}`,
  `http://127.0.0.1:${port}/`], { stdio: 'ignore' })

let page
for (let i = 0; i < 100; i += 1) {
  try {
    const list = await (await fetch(`http://127.0.0.1:${debugPort}/json/list`)).json()
    page = list.find((item) => item.type === 'page')
    if (page) break
  } catch { /* wait */ }
  await sleep(200)
}
if (!page) throw new Error('CDP page not found')
const ws = new WebSocket(page.webSocketDebuggerUrl)
const pending = new Map(); let seq = 0
ws.onmessage = (event) => {
  const message = JSON.parse(event.data)
  if (message.id && pending.has(message.id)) {
    const item = pending.get(message.id); pending.delete(message.id)
    message.error ? item.reject(new Error(JSON.stringify(message.error))) : item.resolve(message.result)
  }
  if (message.method === 'Fetch.requestPaused') {
    const method = message.params.request.method
    const url = message.params.request.url
    if (url.includes('/api/v1/')) {
      void send('Fetch.failRequest', { requestId: message.params.requestId, errorReason: 'ConnectionRefused' })
    } else {
      void send('Fetch.continueRequest', { requestId: message.params.requestId })
    }
  }
}
await new Promise((resolve, reject) => { ws.onopen = resolve; ws.onerror = reject })
function send(method, params = {}) {
  return new Promise((resolve, reject) => {
    const id = ++seq; pending.set(id, { resolve, reject })
    ws.send(JSON.stringify({ id, method, params }))
  })
}
async function evaluate(expression) {
  const result = await send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true })
  if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails))
  return result.result?.value
}
await send('Page.enable'); await send('Runtime.enable')
await send('Fetch.enable', { patterns: [{ urlPattern: '*api/v1/*', requestStage: 'Request' }] })
await send('Page.navigate', { url: `http://127.0.0.1:${port}/` })
await sleep(3500)
const state = await evaluate(`(() => ({
  badge: document.querySelector('[data-testid=demo-status]')?.innerText.replace(/\\s+/g,' ').trim() || null,
  cached: !!document.querySelector('[data-testid=cached-snapshot]'),
  generated: document.querySelector('[data-testid=cached-snapshot]')?.innerText.match(/生成于\\s*([^，]+)/)?.[1] || null,
  snapshotError: document.querySelector('[data-testid=snapshot-error]')?.innerText || null,
  titleCount: document.querySelectorAll('[data-testid=cached-snapshot] li').length,
}))()`)
const shot = await send('Page.captureScreenshot', { format: 'png' })
fs.mkdirSync(path.join(root, 'tests', 'f15', 'artifacts'), { recursive: true })
fs.writeFileSync(path.join(root, 'tests', 'f15', 'artifacts', 'cached-fallback.png'), Buffer.from(shot.data, 'base64'))
const ok = state.cached && String(state.badge).includes('离线快照') && Boolean(state.generated) && !state.snapshotError && state.titleCount >= 8
console.log(JSON.stringify({ ...state, ok }, null, 2))
await send('Browser.close'); ws.close()
browser.kill()
server.close()
await sleep(500)
fs.rmSync(profile, { recursive: true, force: true })
if (!ok) process.exit(1)
