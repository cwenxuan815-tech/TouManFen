/**
 * F20 阶段 5C：真实 App + 本地 mock 访问/业务 API + Edge headless。
 *
 * 仅本地 127.0.0.1 临时端口；不连接真实网关/worker/LLM。
 */
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdir, rm } from 'node:fs/promises'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import os from 'node:os'
import { build } from 'vite'
import { createMockServer } from './mock-server.mjs'

const here = dirname(fileURLToPath(import.meta.url))
const frontendRoot = resolve(here, '../..')
const distDir = join(here, 'dist')
const SERVER_PORT = 18196
const CDP_PORT = 9346
const HARNESS_URL = `http://127.0.0.1:${SERVER_PORT}/tests/f20/harness.html`
process.env.F20_SERVER_PORT = String(SERVER_PORT)

const EDGE_CANDIDATES = [
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
]

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

async function waitFor(fn, { timeout = 12_000, interval = 100, label = 'condition' } = {}) {
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

function buildChecks() {
  const checks = []
  return {
    checks,
    check(name, pass, detail = '') {
      checks.push({ name, pass: Boolean(pass), detail: String(detail) })
    },
    eq(name, actual, expected) {
      const left = JSON.stringify(actual)
      const right = JSON.stringify(expected)
      checks.push({
        name,
        pass: left === right,
        detail: `${left} !== ${right}`,
      })
    },
  }
}

async function setScenario(scenario) {
  const response = await fetch(`http://127.0.0.1:${SERVER_PORT}/__scenario`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ scenario }),
  })
  if (!response.ok) throw new Error(`setScenario ${scenario} failed`)
}

async function getLog() {
  return (await fetch(`http://127.0.0.1:${SERVER_PORT}/__log`)).json()
}

async function navigate(client, { scenario, seedTicket = null, width = null }) {
  await setScenario(scenario)
  if (width) {
    await client.send('Emulation.setDeviceMetricsOverride', {
      width,
      height: 844,
      deviceScaleFactor: 1,
      mobile: true,
    })
  } else {
    await client.send('Emulation.clearDeviceMetricsOverride')
  }
  // about:blank has an opaque origin; seed storage in the new same-origin
  // document before the app module executes.
  const preload = await client.send('Page.addScriptToEvaluateOnNewDocument', {
    source: `try {
      localStorage.clear();
      ${seedTicket ? `localStorage.setItem('tmf.access.ticket', ${JSON.stringify(seedTicket)});` : ''}
      location.hash = '';
    } catch (error) { /* storage unavailable in harness */ }`,
  })
  try {
    await client.send('Page.navigate', { url: `${HARNESS_URL}?t=${Date.now()}` })
    await waitFor(
      () => evaluate(client, 'Boolean(document.querySelector("#app"))'),
      { timeout: 15_000, label: 'F20 app mounted' },
    )
  } finally {
    await client.send('Page.removeScriptToEvaluateOnNewDocument', {
      identifier: preload.identifier,
    })
  }
}

async function ui(client) {
  return evaluate(client, `(() => {
    const panel = document.querySelector('[data-access-panel]');
    const button = document.querySelector('[data-access-retry]');
    return {
      panel: panel ? panel.getAttribute('data-access-state') : null,
      panelText: panel ? (panel.innerText || '').replace(/\\s+/g, ' ').trim() : '',
      active: Boolean(document.querySelector('[data-access-active]')),
      activeCountdown: document.querySelector('[data-access-active-countdown]')?.textContent?.trim() || '',
      lab: Boolean(document.querySelector('#lab')),
      position: document.querySelector('[data-access-position]')?.textContent?.trim() || '',
      countdown: document.querySelector('[data-access-countdown]')?.textContent?.trim() || '',
      hasStorageTicket: Boolean(localStorage.getItem('tmf.access.ticket')),
      url: location.href,
      scrollWidth: document.documentElement.scrollWidth,
      innerWidth: window.innerWidth,
      retryVisible: Boolean(button && button.getBoundingClientRect().width > 0),
      cancelVisible: Boolean(document.querySelector('[data-access-cancel]')),
      leaveVisible: Boolean(document.querySelector('[data-access-leave]')),
    };
  })()`)
}

async function click(client, selector) {
  const clicked = await evaluate(client, `(() => {
    const element = document.querySelector(${JSON.stringify(selector)});
    if (!element) return false;
    element.click();
    return true;
  })()`)
  if (!clicked) throw new Error(`click target missing: ${selector}`)
}

function scenarioDisabled(ctx) {
  return {
    name: 'disabled compatibility',
    async run(client, checks, check) {
      await navigate(client, { scenario: 'disabled' })
      await waitFor(
        () => evaluate(client, 'Boolean(document.querySelector("#lab"))'),
        { label: 'disabled business content' },
      )
      const state = await ui(client)
      const log = await getLog()
      check('disabled: no access overlay', state.panel === null, JSON.stringify(state))
      check('disabled: business content visible', state.lab)
      check(
        'disabled: business request has no access ticket',
        log.business.length > 0 && log.business.every((item) => item.hasTicket === false),
        JSON.stringify(log.business),
      )
      check('disabled: no stale ticket stored', state.hasStorageTicket === false)
    },
  }
}

function scenarioReadyActive(ctx) {
  return {
    name: 'ready -> claim -> active',
    async run(client, checks, check) {
      await navigate(client, { scenario: 'ready' })
      await waitFor(
        () => evaluate(client, 'Boolean(document.querySelector("[data-access-active]"))'),
        { label: 'ready auto claim active' },
      )
      await waitFor(
        () => evaluate(client, 'Boolean(document.querySelector("#lab"))'),
        { label: 'active business content' },
      )
      const state = await ui(client)
      const log = await getLog()
      check('ready/active: active badge visible', state.active)
      check('ready/active: business content visible', state.lab)
      check('ready/active: ticket saved', state.hasStorageTicket)
      check('ready/active: active countdown present', /\d+:\d\d/.test(state.activeCountdown), state.activeCountdown)
      check(
        'ready/active: enter did not auto inject ticket',
        log.access.some((item) => item.path === '/api/v1/access/enter' && item.hasTicket === false),
        JSON.stringify(log.access),
      )
      check(
        'ready/active: claim sent ticket',
        log.access.some((item) => item.path === '/api/v1/access/claim' && item.hasTicket === true),
        JSON.stringify(log.access),
      )
      check(
        'ready/active: business request carries ticket',
        log.business.length > 0 && log.business.every((item) => item.hasTicket === true),
        JSON.stringify(log.business),
      )
      check('ready/active: ticket not in URL', !state.url.includes('f20-opaque'))
      const ocrNav = await evaluate(client, `(() => {
        const link = document.querySelector('a[href="http://192.168.12.82:8000/#image"]');
        return link
          ? { href: link.getAttribute('href'), text: (link.textContent || '').trim(), visible: Boolean(link.offsetParent) }
          : null;
      })()`)
      check(
        'ready/active: OCR nav link points to OCR service',
        ocrNav?.href === 'http://192.168.12.82:8000/#image' &&
          ocrNav.visible &&
          ocrNav.text.includes('OCR'),
        JSON.stringify(ocrNav),
      )
      const weiboNav = await evaluate(client, `(() => {
        const link = document.querySelector('a[href="http://192.168.12.54:8080/"]');
        return link
          ? { href: link.getAttribute('href'), text: (link.textContent || '').trim(), visible: Boolean(link.offsetParent) }
          : null;
      })()`)
      check(
        'ready/active: Weibo hot-search nav link points to service',
        weiboNav?.href === 'http://192.168.12.54:8080/' &&
          weiboNav.visible &&
          weiboNav.text.includes('微博热搜'),
        JSON.stringify(weiboNav),
      )
      const before = await getLog()
      await sleep(1100)
      const after = await getLog()
      check(
        'ready/active: business bootstrapped once',
        after.modelsCalls === before.modelsCalls,
        `${before.modelsCalls} -> ${after.modelsCalls}`,
      )
      await click(client, '[data-access-leave]')
      await waitFor(
        () => evaluate(client, 'document.querySelector("[data-access-panel]")?.getAttribute("data-access-state") === "cancelled"'),
        { label: 'leave cancelled panel' },
      )
      const left = await ui(client)
      const leaveLog = await getLog()
      check('leave: sets cancelled without cooling', left.panel === 'cancelled' && !left.panelText.includes('冷却'), JSON.stringify(left))
      check('leave: clears local ticket', left.hasStorageTicket === false)
      check('leave: calls leave API', leaveLog.accessLeave === 1, String(leaveLog.accessLeave))
    },
  }
}

function scenarioReadyExpire(ctx) {
  return {
    name: 'ready 15s expiry recovery',
    async run(client, checks, check) {
      await navigate(client, { scenario: 'ready_expire' })
      await waitFor(
        () => evaluate(client, 'Boolean(document.querySelector("[data-access-active]"))'),
        { timeout: 6_000, label: 'ready expiry recovery active' },
      )
      const log = await getLog()
      check('ready expiry: claim attempted', log.accessClaim === 1, String(log.accessClaim))
      check(
        'ready expiry: recovery enter omitted stale ticket',
        log.access.filter((item) => item.path === '/api/v1/access/enter').length >= 2,
        JSON.stringify(log.access),
      )
      check('ready expiry: reaches active after recovery', (await ui(client)).active)
    },
  }
}

function scenarioCompareFallback(ctx) {
  return {
    name: 'access active: compare POST fallback (no EventSource)',
    async run(client, checks, check) {
      await navigate(client, { scenario: 'active' })
      await waitFor(
        () => evaluate(client, 'Boolean(document.querySelector("[data-access-active]"))'),
        { label: 'compare active' },
      )
      await click(client, '[data-top-mode="compare"]')
      await waitFor(
        () => evaluate(client, 'Boolean(document.querySelector("#news-title")?.offsetParent)'),
        { label: 'compare title input visible' },
      )
      check(
        'active compare: title input remains visible',
        await evaluate(client, 'Boolean(document.querySelector("#news-title")?.offsetParent)'),
      )
      await evaluate(client, `(() => {
        const element = document.querySelector('#news-title');
        const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')?.set;
        setter?.call(element, 'F20 access compare title');
        element.dispatchEvent(new Event('input', { bubbles: true }));
      })()`)
      await evaluate(client, `document.querySelector('[data-submit]')?.click()`)
      await waitFor(
        async () => (await getLog()).business.some((item) => item.path === '/api/v1/compare'),
        { timeout: 8_000, label: 'compare POST fallback' },
      )
      const log = await getLog()
      const compare = log.business.find((item) => item.path === '/api/v1/compare')
      const stream = log.business.find((item) => item.path === '/api/v1/compare/stream')
      check('active compare: uses gated POST fallback', Boolean(compare) && compare.hasTicket === true, JSON.stringify(log.business))
      check('active compare: does not attempt EventSource SSE', stream === undefined, JSON.stringify(log.business))
    },
  }
}

function scenarioQueued(ctx) {
  return {
    name: 'queued position/poll/cancel',
    async run(client, checks, check) {
      await navigate(client, { scenario: 'queued' })
      await waitFor(
        () => evaluate(client, 'document.querySelector("[data-access-panel]")?.getAttribute("data-access-state") === "queued"'),
        { label: 'queued panel' },
      )
      const first = await ui(client)
      check('queued: position and ahead visible', first.panelText.includes('第 3 位') && first.panelText.includes('前方 2 人'), first.panelText)
      check('queued: business content hidden', !first.lab)
      const before = await getLog()
      check('queued: no business call', before.business.length === 0, JSON.stringify(before.business))
      await waitFor(
        async () => (await getLog()).accessStatus >= 1,
        { timeout: 5_000, label: 'jitter status poll' },
      )
      check('queued: status poll sent ticket', (await getLog()).access.some(
        (item) => item.path === '/api/v1/access/status' && item.hasTicket === true,
      ))
      await click(client, '[data-access-cancel]')
      await waitFor(
        () => evaluate(client, 'document.querySelector("[data-access-panel]")?.getAttribute("data-access-state") === "cancelled"'),
        { label: 'cancel queued' },
      )
      const cancelled = await ui(client)
      const log = await getLog()
      check('cancel: cancelled state', cancelled.panel === 'cancelled', cancelled.panelText)
      check('cancel: clears local ticket', cancelled.hasStorageTicket === false)
      check('cancel: calls cancel API', log.accessCancel === 1, String(log.accessCancel))
    },
  }
}

function scenarioHiddenPoll(ctx) {
  return {
    name: 'hidden page lowers poll frequency',
    async run(client, checks, check) {
      await navigate(client, { scenario: 'queued' })
      await waitFor(
        async () => (await getLog()).accessStatus >= 1,
        { timeout: 5_000, label: 'visible status poll' },
      )
      await evaluate(client, `(() => {
        Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
        document.dispatchEvent(new Event('visibilitychange'));
      })()`)
      await sleep(700)
      const baseline = (await getLog()).accessStatus
      await sleep(2_400)
      const during = await getLog()
      check(
        'hidden: no 2s-frequency polling',
        during.accessStatus - baseline <= 1,
        `status ${baseline} -> ${during.accessStatus}`,
      )
      check('hidden: queued state preserved', (await ui(client)).panel === 'queued')
    },
  }
}

function scenarioQueueFull(ctx) {
  return {
    name: 'queue_full manual retry',
    async run(client, checks, check) {
      await navigate(client, { scenario: 'queue_full' })
      await waitFor(
        () => evaluate(client, 'document.querySelector("[data-access-panel]")?.getAttribute("data-access-state") === "queue_full"'),
        { label: 'queue_full panel' },
      )
      const first = await getLog()
      await sleep(1_200)
      const second = await getLog()
      check('queue_full: visible and retryable', (await ui(client)).retryVisible)
      check('queue_full: no dense auto retry', second.accessEnter === first.accessEnter, `${first.accessEnter} -> ${second.accessEnter}`)
      await setScenario('ready')
      await click(client, '[data-access-retry]')
      await waitFor(
        () => evaluate(client, 'Boolean(document.querySelector("[data-access-active]"))'),
        { label: 'manual retry active' },
      )
      check('queue_full: manual retry enters active', (await ui(client)).active)
    },
  }
}

function scenarioCooling(ctx) {
  return {
    name: 'cooling countdown/recovery',
    async run(client, checks, check) {
      await navigate(client, { scenario: 'cooling' })
      await waitFor(
        () => evaluate(client, 'document.querySelector("[data-access-panel]")?.getAttribute("data-access-state") === "cooling"'),
        { label: 'cooling panel' },
      )
      const cooling = await ui(client)
      check('cooling: countdown visible', cooling.panelText.includes('秒后可重试'), cooling.panelText)
      check('cooling: business hidden', !cooling.lab)
      await waitFor(
        () => evaluate(client, 'Boolean(document.querySelector("[data-access-active]"))'),
        { timeout: 8_000, label: 'cooling -> active' },
      )
      check('cooling: recovers to active', (await ui(client)).active)
    },
  }
}

function scenarioExpire(ctx) {
  return {
    name: 'active expiry fail-closed',
    async run(client, checks, check) {
      await navigate(client, { scenario: 'expire' })
      await waitFor(
        () => evaluate(client, 'Boolean(document.querySelector("[data-access-active]"))'),
        { label: 'expire active' },
      )
      await waitFor(
        () => evaluate(client, 'document.querySelector("[data-access-panel]")?.getAttribute("data-access-state") === "cooling"'),
        { timeout: 7_000, label: 'expire -> cooling' },
      )
      const before = await getLog()
      await sleep(900)
      const after = await getLog()
      const state = await ui(client)
      check('expire: no business content while cooling', !state.lab, JSON.stringify(state))
      check(
        'expire: no new business request after expiry',
        after.modelsCalls === before.modelsCalls,
        `${before.modelsCalls} -> ${after.modelsCalls}`,
      )
      check('expire: countdown/panel visible', state.panel === 'cooling' && Boolean(state.countdown), JSON.stringify(state))
    },
  }
}

function scenarioUnavailableIdentity(ctx) {
  return {
    name: 'unavailable/identity fail-closed',
    async run(client, checks, check) {
      for (const [scenario, panelState] of [['unavailable', 'unavailable'], ['identity', 'identity']]) {
        await navigate(client, { scenario })
        await waitFor(
          () => evaluate(client, `document.querySelector("[data-access-panel]")?.getAttribute("data-access-state") === "${panelState}"`),
          { label: `${scenario} panel` },
        )
        const state = await ui(client)
        const log = await getLog()
        check(`${scenario}: fail-closed panel`, state.panel === panelState, JSON.stringify(state))
        check(`${scenario}: business content hidden`, !state.lab)
        check(`${scenario}: no business API call`, log.business.length === 0, JSON.stringify(log.business))
      }
    },
  }
}

function scenarioOldTicket(ctx) {
  return {
    name: 'stale ticket recovery',
    async run(client, checks, check) {
      await navigate(client, { scenario: 'old_ticket', seedTicket: 'f20-stale-ticket' })
      await waitFor(
        () => evaluate(client, 'Boolean(document.querySelector("[data-access-active]"))'),
        { label: 'old ticket recovery active' },
      )
      const state = await ui(client)
      const log = await getLog()
      const enters = log.access.filter((item) => item.path === '/api/v1/access/enter')
      check('old ticket: new ticket stored', state.hasStorageTicket)
      check('old ticket: first enter carried stale ticket', enters[0]?.hasTicket === true, JSON.stringify(enters))
      check('old ticket: recovery enter omitted stale ticket', enters[1]?.hasTicket === false, JSON.stringify(enters))
      check('old ticket: ticket absent from URL', !state.url.includes('f20-stale-ticket'))
    },
  }
}

function scenarioThrottle(ctx) {
  return {
    name: 'poll throttle backoff',
    async run(client, checks, check) {
      await navigate(client, { scenario: 'throttle' })
      await waitFor(
        () => evaluate(client, 'document.querySelector("[data-access-panel]")?.getAttribute("data-access-state") === "queued"'),
        { label: 'throttle queued' },
      )
      await waitFor(
        async () => (await getLog()).accessStatus >= 2,
        { timeout: 8_000, label: 'throttle retry status' },
      )
      const state = await ui(client)
      check('throttle: stays queued, not fatal', state.panel === 'queued', JSON.stringify(state))
      check('throttle: no fatal error text', !state.panelText.includes('不可用'), state.panelText)
    },
  }
}

function scenarioStorageEvent(ctx) {
  return {
    name: 'same-origin storage invalidation recovery',
    async run(client, checks, check) {
      await navigate(client, { scenario: 'active' })
      await waitFor(
        () => evaluate(client, 'Boolean(document.querySelector("[data-access-active]"))'),
        { label: 'storage active' },
      )
      const before = await getLog()
      await evaluate(client, `localStorage.removeItem('tmf.access.ticket'); window.dispatchEvent(new StorageEvent('storage', { key: 'tmf.access.ticket', newValue: null, storageArea: localStorage }));`)
      await waitFor(
        async () => (await getLog()).accessEnter > before.accessEnter,
        { timeout: 6_000, label: 'storage recovery enter' },
      )
      check('storage: other-tab invalidation triggers recovery', (await getLog()).accessEnter > before.accessEnter)
    },
  }
}

function scenarioMobile(ctx) {
  return {
    name: '390px access UI',
    async run(client, checks, check) {
      await navigate(client, { scenario: 'queued', width: 390 })
      await waitFor(
        () => evaluate(client, 'document.querySelector("[data-access-panel]")?.getAttribute("data-access-state") === "queued"'),
        { label: 'mobile queued panel' },
      )
      const state = await ui(client)
      check('mobile: no horizontal overflow', state.scrollWidth <= state.innerWidth + 1, `${state.scrollWidth}/${state.innerWidth}`)
      check('mobile: cancel button visible', state.cancelVisible)
      check('mobile: position readable', state.panelText.includes('第 3 位'), state.panelText)
      await client.send('Emulation.clearDeviceMetricsOverride')
    },
  }
}

const SCENARIOS = [
  scenarioDisabled,
  scenarioReadyActive,
  scenarioReadyExpire,
  scenarioCompareFallback,
  scenarioQueued,
  scenarioHiddenPoll,
  scenarioQueueFull,
  scenarioCooling,
  scenarioExpire,
  scenarioUnavailableIdentity,
  scenarioOldTicket,
  scenarioThrottle,
  scenarioStorageEvent,
  scenarioMobile,
]

async function findEdge() {
  return EDGE_CANDIDATES.find((candidate) => existsSync(candidate)) || null
}

async function main() {
  console.log('=== F20 阶段 5C 访问 UI 验收（真实 App + 本地 mock + Edge headless） ===')
  const edgePath = await findEdge()
  if (!edgePath) throw new Error('未找到 Microsoft Edge（msedge.exe）')

  console.log(`[build] 构建 F20 harness（${distDir}）`)
  await build({ configFile: join(here, 'vite.harness.config.mjs'), logLevel: 'warn' })
  console.log('[build] OK')

  const mock = await createMockServer({ port: SERVER_PORT, staticDir: distDir })
  const profileDir = join(os.tmpdir(), `tmf-f20-edge-${process.pid}-${Date.now()}`)
  await mkdir(profileDir, { recursive: true })

  let edge = null
  let client = null
  let failures = 0
  const summary = []

  try {
    edge = spawn(edgePath, [
      '--headless=new',
      '--disable-gpu',
      '--no-first-run',
      '--no-default-browser-check',
      `--remote-debugging-port=${CDP_PORT}`,
      `--user-data-dir=${profileDir}`,
      '--window-size=1280,900',
      'about:blank',
    ], { stdio: 'ignore' })

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
    console.log(`[edge] ${version?.Browser || 'Edge'}`)

    for (const factory of SCENARIOS) {
      const scenario = factory()
      const { checks, check } = buildChecks()
      console.log(`\n--- ${scenario.name} ---`)
      try {
        await scenario.run(client, checks, check)
      } catch (error) {
        check(`${scenario.name}: no runtime exception`, false, String(error?.message || error))
      }
      for (const item of checks) {
        console.log(`${item.pass ? 'PASS' : 'FAIL'} ${item.name}${item.pass ? '' : ` :: ${item.detail}`}`)
        if (!item.pass) failures += 1
      }
      summary.push({ name: scenario.name, checks })
    }

    const total = summary.reduce((sum, item) => sum + item.checks.length, 0)
    console.log(`\nRESULT ${failures ? 'FAIL' : 'PASS'} (${total - failures}/${total} checks)`)
    return failures ? 1 : 0
  } finally {
    try {
      client?.close()
    } catch {
      // ignore
    }
    if (edge) {
      try {
        edge.kill()
      } catch {
        // ignore
      }
    }
    await sleep(300)
    await mock.close()
    await rm(profileDir, { recursive: true, force: true }).catch(() => {})
  }
}

main()
  .then((code) => {
    process.exitCode = code
  })
  .catch((error) => {
    console.error(`F20 FATAL: ${error?.stack || error}`)
    process.exitCode = 1
  })
