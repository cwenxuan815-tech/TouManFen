/**
 * F14 前端质量横切验收运行器：本地 mock API + Edge headless（CDP）。
 *
 * 用法（在 frontend 目录下）：
 *   node tests/f14/run-scenarios.mjs
 *
 * 覆盖：
 * - skip link（首个可聚焦元素 → `<main id="main-content">`）、焦点环（亮/暗）；
 * - Tab 顺序、移动菜单打开后焦点/链接可用、Esc 关闭并归还焦点；
 * - 键盘-only 完整流程：输入 → 提交 → 纠错反馈（含焦点管理/陷阱）→ 进入 Arena 投票；
 * - aria-live 只播报变化（结果摘要/榜单指标切换/Toaster 去重）；
 * - reduced-motion：F10 翻牌 / F11 条形 / F6 分数条 / Toaster / loading spinner 均无动画；
 * - 6× CPU throttle（模拟）下输入延迟、滚动与按钮可用；
 * - 50 张结果卡仍可滚动/聚焦/打开反馈面板。
 *
 * 全部请求指向本地 mock（不访问真实网关 / worker / 真实 LLM）。
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
const artifactDir = join(here, 'artifacts')
const distDir = join(here, 'dist')
const envDir = join(here, 'env')
const SERVER_PORT = 18191
const CDP_PORT = 9336
const HARNESS_URL = `http://127.0.0.1:${SERVER_PORT}/tests/f14/harness.html`
const VIEWPORT = { width: 1440, height: 900 }

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

  once(method, timeoutMs = 20_000) {
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

const KEYS = {
  Tab: { key: 'Tab', code: 'Tab', vk: 9 },
  Enter: { key: 'Enter', code: 'Enter', vk: 13, text: '\r' },
  Escape: { key: 'Escape', code: 'Escape', vk: 27 },
  Space: { key: ' ', code: 'Space', vk: 32, text: ' ' },
}

async function press(client, name, modifiers = 0) {
  const def = KEYS[name]
  const base = {
    key: def.key,
    code: def.code,
    windowsVirtualKeyCode: def.vk,
    nativeVirtualKeyCode: def.vk,
    modifiers,
  }
  // Puppeteer/Playwright 对 Enter/Space 会带 text 一起派发；只有带 text 的 keyDown
  // 才会触发按钮的默认点击行为（无 text 会被当成 rawKeyDown）。
  await client.send('Input.dispatchKeyEvent', {
    type: 'keyDown',
    ...base,
    ...(def.text ? { text: def.text, unmodifiedText: def.text } : {}),
  })
  await client.send('Input.dispatchKeyEvent', { type: 'keyUp', ...base })
}

/** 键盘激活当前焦点；Enter 无效果时回退 Space（不同浏览器对按钮的默认动作时机不同）。 */
async function activate(client, isDone, { timeout = 4000 } = {}) {
  await press(client, 'Enter')
  try {
    return await waitFor(isDone, { timeout, interval: 80, label: 'activate(Enter)' })
  } catch {
    await press(client, 'Space')
    return waitFor(isDone, { timeout, interval: 80, label: 'activate(Space)' })
  }
}

async function insertText(client, text) {
  await client.send('Input.insertText', { text })
}

async function active(client) {
  return evaluate(client, 'window.__f14.active()')
}

async function dom(client) {
  return evaluate(client, 'window.__f14.dom()')
}

async function activeMatches(client, selector) {
  return evaluate(
    client,
    `Boolean(document.activeElement && document.activeElement.matches(${JSON.stringify(selector)}))`,
  )
}

async function tabUntil(client, selector, { max = 400, label = selector } = {}) {
  let last = null
  for (let index = 0; index < max; index += 1) {
    await press(client, 'Tab')
    if (await activeMatches(client, selector)) {
      return { tabs: index + 1, active: await active(client) }
    }
    last = await active(client)
  }
  throw new Error(`tabUntil 未到达 ${label}（${max} 次 Tab，最后焦点 ${JSON.stringify(last)}）`)
}

async function motion(client, selector) {
  return evaluate(
    client,
    `(() => {
      const element = document.querySelector(${JSON.stringify(selector)})
      if (!element) return null
      const style = getComputedStyle(element)
      const parse = (value) => (value || '').split(',').map((part) => part.trim()).filter(Boolean)
        .map((part) => part.endsWith('ms') ? parseFloat(part) : parseFloat(part) * 1000)
      return {
        animationName: style.animationName,
        animationDurations: parse(style.animationDuration),
        transitionDurations: parse(style.transitionDuration),
      }
    })()`,
  )
}

function allShort(motionInfo, thresholdMs = 1) {
  if (!motionInfo) return false
  const durations = [...(motionInfo.animationDurations || []), ...(motionInfo.transitionDurations || [])]
  return durations.every((value) => Number.isFinite(value) && value <= thresholdMs)
}

async function ringOf(client, selector) {
  return evaluate(client, `window.__f14.ring(${JSON.stringify(selector)})`)
}

async function focusSelector(client, selector) {
  return evaluate(client, `window.__f14.focusSelector(${JSON.stringify(selector)})`)
}

async function navigate(client, url) {
  const loaded = client.once('Page.loadEventFired', 25_000)
  await client.send('Page.navigate', { url })
  await loaded
  await waitFor(
    async () => evaluate(client, 'Boolean(window.__f14 && window.__f14.ready)'),
    { timeout: 20_000, label: 'harness ready' },
  )
}

async function freshPage(client, mock, { mode = 'app', scenario = 'normal' } = {}) {
  await mock.setScenario(scenario)
  await navigate(
    client,
    `${HARNESS_URL}?mode=${encodeURIComponent(mode)}&scenario=${scenario}&t=${Date.now()}`,
  )
  await waitFor(
    async () => {
      const summary = await dom(client)
      return summary && summary.main !== undefined ? summary : null
    },
    { timeout: 15_000, label: 'app mounted' },
  )
  return dom(client)
}

function luminance(rgb) {
  const match = /rgba?\(([^)]+)\)/.exec(rgb || '')
  if (!match) return null
  const parts = match[1].split(',').map((part) => Number.parseFloat(part.trim()))
  const [r, g, b] = parts
  const channel = (value) => {
    const normalized = value / 255
    return normalized <= 0.03928 ? normalized / 12.92 : ((normalized + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
}

function contrastRatio(foreground, background) {
  const first = luminance(foreground)
  const second = luminance(background)
  if (first === null || second === null) return null
  const light = Math.max(first, second)
  const dark = Math.min(first, second)
  return (light + 0.05) / (dark + 0.05)
}

const SCENARIOS = [
  {
    id: 'skip-link-focus-ring',
    name: 'S1 skip link / 焦点环（亮+暗）/ Tab 顺序',
    async run(client, context) {
      const { checks } = context
      await freshPage(client, context.mock)
      const summary = await dom(client)
      checks.check('App 已渲染主内容区', Boolean(summary.main), JSON.stringify(summary.main))
      checks.check(
        'main 使用 id="main-content" 且 tabindex="-1"',
        summary.main?.id === 'main-content' && summary.main?.tabindex === '-1',
        JSON.stringify(summary.main),
      )
      checks.check(
        'skip link 存在且指向 main-content',
        summary.skipLink?.href === '#main-content' && summary.skipLink?.text.includes('跳到主要内容'),
        JSON.stringify(summary.skipLink),
      )
      checks.check(
        'skip link 是页面首个可聚焦元素',
        summary.skipLink?.first === true && summary.firstFocusables[0]?.isSkipLink === true,
        JSON.stringify(summary.firstFocusables.slice(0, 3)),
      )
      const skipHidden = await evaluate(
        client,
        `(() => {
          const element = document.querySelector('[data-skip-link]')
          const rect = element.getBoundingClientRect()
          const style = getComputedStyle(element)
          return { bottom: rect.bottom, transform: style.transform, opacity: style.opacity }
        })()`,
      )
      checks.check(
        'skip link 未聚焦时移出视口（不遮挡页面）',
        skipHidden.bottom <= 0 && skipHidden.opacity !== '0',
        JSON.stringify(skipHidden),
      )

      // 从 body 开始按一次 Tab：焦点应落到 skip link（键盘可见）。
      await evaluate(client, 'document.activeElement && document.activeElement.blur()')
      await press(client, 'Tab')
      const skipActive = await waitFor(
        async () =>
          (await evaluate(
            client,
            "Boolean(document.activeElement && document.activeElement.hasAttribute('data-skip-link'))",
          ))
            ? await active(client)
            : null,
        { timeout: 3000, label: 'skip link focused' },
      )
      checks.check('Tab 首次聚焦 skip link', skipActive?.tag === 'a', JSON.stringify(skipActive))
      await sleep(260)
      const skipRing = await ringOf(client, '[data-skip-link]')
      checks.check(
        'skip link 聚焦后出现在视口内且焦点环可见（≥2px 实线、未被裁切）',
        skipRing?.inViewport === true &&
          skipRing?.outlineWidth >= 2 &&
          skipRing?.outlineStyle === 'solid' &&
          skipRing?.clipped === false,
        JSON.stringify(skipRing),
      )
      await captureScreenshot(client, 'S1-skip-link-focus')

      // Enter 激活 skip link：焦点/滚动到 main-content。
      await press(client, 'Enter')
      const mainActive = await waitFor(
        async () => {
          const current = await active(client)
          return current?.id === 'main-content' ? current : null
        },
        { timeout: 3000, label: 'main focused' },
      )
      checks.check('Enter 激活 skip link 后焦点落到 main', mainActive?.id === 'main-content', JSON.stringify(mainActive))
      checks.check(
        '激活 skip link 后 URL hash=#main-content',
        (await evaluate(client, 'window.location.hash')) === '#main-content',
        await evaluate(client, 'window.location.hash'),
      )

      // Tab 顺序：从 skip link 起连续 Tab，前 8 个应依次是品牌/导航 7 项。
      await focusSelector(client, '[data-skip-link]')
      const sequence = []
      for (let index = 0; index < 10; index += 1) {
        await press(client, 'Tab')
        sequence.push(await active(client))
      }
      const hrefs = sequence.map((item) => item?.href)
      checks.check(
        'Tab 顺序：品牌 → 分类实验台 → 模型矩阵 → 竞技场 → PK 榜单 → OCR 识别 → 微博热搜 → 关于',
        hrefs[0] === '#top' &&
          hrefs[1] === '#lab' &&
          hrefs[2] === '#models' &&
          hrefs[3] === '#arena' &&
          hrefs[4] === '#bench' &&
          hrefs[5] === 'http://192.168.12.82:8000/#image' &&
          hrefs[6] === 'http://192.168.12.54:8080/' &&
          hrefs[7] === '#about',
        JSON.stringify(hrefs),
      )
      const labActive = sequence[1]
      checks.check('导航链接有可见焦点环', labActive?.outlineWidth >= 2 && labActive?.focusVisible === true, JSON.stringify(labActive))
      checks.check(
        '导航焦点环未被 overflow 裁切且在视口内',
        labActive?.clipped === false && labActive?.inViewport === true,
        JSON.stringify({ clipped: labActive?.clipped, inViewport: labActive?.inViewport }),
      )
      const anchorTargets = await evaluate(
        client,
        `(() => ['#lab', '#models', '#about'].map((href) => ({ href, exists: Boolean(document.querySelector(href)) })))()`,
      )
      checks.check(
        '导航锚点 #lab/#models/#about 均能定位到页面区块',
        anchorTargets.every((target) => target.exists),
        JSON.stringify(anchorTargets),
      )
      await captureScreenshot(client, 'S1-nav-focus')

      // 暗色下焦点环可见（token 切换为浅蓝 #9cc4ff）。
      await evaluate(client, "document.documentElement.dataset.theme = 'dark'")
      await focusSelector(client, 'a[href="#lab"]')
      const darkRing = await ringOf(client, 'a[href="#lab"]')
      const darkBackground = await evaluate(client, 'getComputedStyle(document.documentElement).backgroundColor')
      const darkContrast = contrastRatio(darkRing?.outlineColor, darkBackground)
      checks.check(
        '暗色主题焦点环可见（浅蓝 + 与页面背景对比度 ≥ 3:1）',
        darkRing?.outlineWidth >= 2 && darkContrast !== null && darkContrast >= 3,
        JSON.stringify({ ring: darkRing, background: darkBackground, contrast: darkContrast }),
      )
      await captureScreenshot(client, 'S1-dark-focus')
      await evaluate(client, "document.documentElement.dataset.theme = 'light'")

      return { evidence: { summary, sequence: hrefs, skipRing, darkRing, contrast: darkContrast } }
    },
  },
  {
    id: 'keyboard-only-flow',
    name: 'S2 键盘-only：输入 → 提交 → 纠错反馈（焦点管理）→ Arena 投票',
    async run(client, context) {
      const { checks } = context
      await freshPage(client, context.mock)

      // 输入标题：Tab 到 textarea 后用真实键盘输入。
      await tabUntil(client, '#news-title', { max: 120, label: '新闻标题输入框' })
      const title = '北京二手房成交量创年内新高'
      await insertText(client, title)
      const typed = await evaluate(client, "document.querySelector('#news-title').value")
      checks.check('键盘输入进入标题框', typed === title, typed)
      const inputFocus = await evaluate(
        client,
        `(() => {
          const wrap = document.querySelector('.textarea-wrap')
          const area = document.querySelector('#news-title')
          return {
            activeIsArea: document.activeElement === area,
            wrapShadow: getComputedStyle(wrap).boxShadow,
            wrapBorder: getComputedStyle(wrap).borderColor,
          }
        })()`,
      )
      checks.check(
        '输入框焦点可见（包裹层 focus-within 高亮）',
        inputFocus.activeIsArea === true && inputFocus.wrapShadow !== 'none',
        JSON.stringify(inputFocus),
      )
      checks.check(
        '输入后字符计数同步更新',
        (await evaluate(client, "document.querySelector('.char-count')?.textContent || ''")).includes(String(title.length)),
        await evaluate(client, "document.querySelector('.char-count')?.textContent || ''"),
      )

      // Tab 到提交按钮并激活。
      await tabUntil(client, '.submit-button', { max: 120, label: '提交按钮' })
      await activate(
        client,
        async () => Boolean((await dom(client)).result?.exists),
        { timeout: 8000 },
      )
      const resultDom = await dom(client)
      checks.check('Enter 提交后渲染结果卡', resultDom.result?.exists === true, JSON.stringify(resultDom.result))
      checks.check('结果区不再整体 aria-live（避免整块朗读）', resultDom.result?.sectionAriaLive === null, String(resultDom.result?.sectionAriaLive))
      checks.check(
        '结果摘要通过独立 sr-only aria-live 播报一次',
        (resultDom.result?.liveText || '').includes('分类完成') &&
          (resultDom.liveRegions || []).filter((region) => (region.text || '').includes('分类完成')).length === 1,
        JSON.stringify({ liveText: resultDom.result?.liveText }),
      )
      await captureScreenshot(client, 'S2-result')

      // 反馈面板：打开后焦点进入第一项。
      await tabUntil(client, '.feedback-toggle', { max: 160, label: '判错了按钮' })
      await activate(client, async () => Boolean(await evaluate(client, "Boolean(document.querySelector('[data-feedback-panel]'))")))
      const chipActive = await waitFor(
        async () => ((await active(client))?.cls || '').includes('feedback-chip') ? await active(client) : null,
        { timeout: 3000, label: 'feedback first item focused' },
      )
      checks.check('打开反馈面板后焦点进入第一项', chipActive?.cls.includes('feedback-chip') === true, JSON.stringify(chipActive))

      // Escape 关闭并归还焦点。
      await press(client, 'Escape')
      await waitFor(
        async () => await activeMatches(client, '.feedback-toggle'),
        { timeout: 3000, label: 'focus restored to trigger' },
      )
      const closed = await dom(client)
      checks.check('Escape 关闭面板', await evaluate(client, "Boolean(document.querySelector('[data-feedback-panel]'))") === false, String(closed.hash))
      checks.check('关闭后焦点回到触发按钮', await activeMatches(client, '.feedback-toggle'), JSON.stringify(await active(client)))

      // 重新打开，验证简单焦点循环（trap）：末尾 Tab → 回到触发按钮；Shift+Tab 反向。
      await activate(client, async () => Boolean(await evaluate(client, "Boolean(document.querySelector('[data-feedback-panel]'))")))
      await focusSelector(client, '.feedback-cancel')
      await press(client, 'Tab')
      const trapForward = await active(client)
      checks.check(
        '面板 Tab 循环：末尾控件 Tab 回到触发按钮',
        trapForward?.cls.includes('feedback-toggle') === true,
        JSON.stringify(trapForward),
      )
      await press(client, 'Tab', 8) // Shift+Tab
      const trapBackward = await active(client)
      checks.check(
        '面板 Tab 循环：触发按钮 Shift+Tab 回到末尾控件',
        trapBackward?.cls.includes('feedback-cancel') === true,
        JSON.stringify(trapBackward),
      )
      await tabUntil(
        client,
        '.feedback-chip:not(.current):not([disabled])',
        { max: 30, label: '非当前类别 chip' },
      )
      await press(client, 'Enter')
      const correctedClass = await evaluate(
        client,
        "document.querySelector('.feedback-chip.selected')?.textContent?.trim() || ''",
      )
      checks.check('键盘选中纠正类别', correctedClass.length > 0, correctedClass)
      await tabUntil(client, '.feedback-submit:not([disabled])', { max: 40, label: '提交反馈按钮' })
      await press(client, 'Enter')
      const feedbackDone = await waitFor(
        async () => {
          const current = await dom(client)
          const panelOpen = await evaluate(client, "Boolean(document.querySelector('[data-feedback-panel]'))")
          return !panelOpen && current.toasts.length > 0 ? current : null
        },
        { timeout: 8000, label: 'feedback submitted' },
      )
      checks.check(
        '反馈提交成功有「待审核」提示',
        feedbackDone.toasts.some((toast) => toast.includes('待审核')),
        JSON.stringify(feedbackDone.toasts),
      )
      checks.check('提交后焦点回到触发按钮', await activeMatches(client, '.feedback-toggle'), JSON.stringify(await active(client)))
      checks.check(
        '反馈状态只通过 Toaster 单条播报（无重复 live 区）',
        (feedbackDone.liveRegions || []).filter((region) => (region.text || '').includes('待审核')).length === 1,
        JSON.stringify((feedbackDone.liveRegions || []).filter((region) => (region.text || '').includes('待审核'))),
      )
      await captureScreenshot(client, 'S2-feedback')

      // 键盘导航到 #arena 并投票。
      await tabUntil(client, 'a[href="#arena"]', { max: 400, label: '导航：竞技场' })
      await press(client, 'Enter')
      await waitFor(
        async () => ((await dom(client)).arena?.state || '') === 'ready',
        { timeout: 10_000, label: 'arena ready' },
      )
      checks.check('Enter 激活导航进入 Arena', (await dom(client)).arena?.state === 'ready', JSON.stringify((await dom(client)).arena))
      await tabUntil(client, '[data-arena-card]', { max: 40, label: '匿名候选卡片' })
      const cardActive = await active(client)
      checks.check(
        '匿名卡片键盘焦点环可见且未被裁切',
        cardActive?.outlineWidth >= 2 && cardActive?.clipped === false,
        JSON.stringify(cardActive),
      )
      await press(client, 'Enter')
      await waitFor(
        async () => ((await dom(client)).arena?.state || '') === 'revealed',
        { timeout: 8000, label: 'arena revealed' },
      )
      const revealedActive = await waitFor(
        async () => ((await active(client))?.tag || '') === 'p' && (await active(client))?.cls.includes('arena-reveal-note')
          ? await active(client)
          : null,
        { timeout: 3000, label: 'reveal note focused' },
      )
      checks.check('Enter 投票后翻牌揭晓', (await dom(client)).arena?.revealed === true, JSON.stringify((await dom(client)).arena))
      checks.check(
        '揭晓后焦点落到揭晓状态区（卡片 disabled 不丢焦点）',
        revealedActive?.cls.includes('arena-reveal-note') === true,
        JSON.stringify(revealedActive),
      )
      await captureScreenshot(client, 'S2-arena-revealed')

      await tabUntil(client, '[data-arena-aftervote-next]', { max: 20, label: '再抽一题按钮' })
      await press(client, 'Enter')
      await waitFor(
        async () => {
          const current = await dom(client)
          return current.arena?.state === 'ready' && current.arena?.cards === 5 ? current : null
        },
        { timeout: 10000, label: 'next arena item ready' },
      )
      checks.check('键盘触发「再抽一题」可继续', true, 'arena ready')

      return { evidence: { title, typed, resultDom, feedbackToasts: feedbackDone.toasts } }
    },
  },
  {
    id: 'reduced-motion',
    name: 'S3 reduced-motion：F10 翻牌 / F11 条形 / F6 分数条 / Toaster / spinner 均无动画',
    async run(client, context) {
      const { checks } = context
      await client.send('Emulation.setEmulatedMedia', {
        features: [{ name: 'prefers-reduced-motion', value: 'reduce' }],
      })
      try {
        await freshPage(client, context.mock, { scenario: 'slow' })
        const initial = await dom(client)
        checks.check('浏览器已应用 prefers-reduced-motion: reduce', initial.reducedMotion === true, String(initial.reducedMotion))

        // 分类结果（F6 分数条）
        await tabUntil(client, '#news-title', { max: 120, label: '标题框' })
        await insertText(client, '减少动效验收标题')
        await tabUntil(client, '.submit-button', { max: 60, label: '提交按钮' })
        await activate(client, async () => Boolean((await dom(client)).result?.exists), { timeout: 12_000 })
        const scoreMotion = await motion(client, '.score-track i')
        checks.check('reduce：F6 分数条无过渡（≤1ms）', allShort(scoreMotion), JSON.stringify(scoreMotion))
        const resultMotion = await motion(client, '.result-section')
        checks.check('reduce：结果区无动画', allShort(resultMotion), JSON.stringify(resultMotion))

        // Arena loading spinner（mock slow）
        await evaluate(client, "window.__f14.setHash('#arena')")
        const spinner = await waitFor(
          async () => {
            const info = await motion(client, '.state-spinner')
            return info ? info : null
          },
          { timeout: 8000, label: 'arena loading spinner' },
        )
        checks.check('reduce：loading spinner 无旋转动画', spinner.animationName === 'none' && allShort(spinner), JSON.stringify(spinner))

        await waitFor(
          async () => ((await dom(client)).arena?.state || '') === 'ready',
          { timeout: 12_000, label: 'arena ready (slow)' },
        )
        await focusSelector(client, '[data-arena-card]')
        await press(client, 'Enter')
        await waitFor(
          async () => ((await dom(client)).arena?.state || '') === 'revealed',
          { timeout: 10000, label: 'arena revealed (reduce)' },
        )
        const flipMotion = await motion(client, '.arena-card-inner')
        checks.check(
          'reduce：F10 翻牌无动画（animation-name=none、时长≤1ms）',
          flipMotion?.animationName === 'none' && allShort(flipMotion),
          JSON.stringify(flipMotion),
        )
        const toastMotion = await motion(client, '.toast')
        checks.check('reduce：Toaster 无过渡动画', toastMotion !== null && allShort(toastMotion), JSON.stringify(toastMotion))

        // F11 条形（切换 metric 时 width 过渡）
        await evaluate(client, "window.__f14.setHash('#bench')")
        await waitFor(
          async () => ((await dom(client)).bench?.rows || 0) > 0,
          { timeout: 12_000, label: 'bench rows' },
        )
        const barMotion = await motion(client, '.row-bar')
        checks.check('reduce：F11 条形无过渡（≤1ms）', allShort(barMotion), JSON.stringify(barMotion))
        checks.check(
          'F11 榜单 aria-live 由独立摘要区承担（榜单本体不再整体播报）',
          await evaluate(client, "document.querySelector('[data-role=\"bench-board\"]')?.getAttribute('aria-live') === null"),
          'bench-board aria-live',
        )
        const metricsBefore = await evaluate(client, "Array.from(document.querySelectorAll('[data-role=\"metric-value\"]')).length")
        await evaluate(
          client,
          "window.__f14.clickSelector('.metric-button[data-metric=\"avg_latency_ms\"]')",
        )
        await sleep(250)
        const benchLiveText = await evaluate(
          client,
          "document.querySelector('[data-live=\"bench\"]')?.textContent?.trim() || ''",
        )
        checks.check(
          'F11 指标切换只播报一句摘要（aria-live 变化）',
          metricsBefore > 0 && benchLiveText.includes('已切换指标') && benchLiveText.includes('平均延迟'),
          JSON.stringify({ metricsBefore, benchLiveText }),
        )
        await captureScreenshot(client, 'S3-reduced-motion')
        return { evidence: { scoreMotion, spinner, flipMotion, toastMotion, barMotion } }
      } finally {
        await client.send('Emulation.setEmulatedMedia', {
          features: [{ name: 'prefers-reduced-motion', value: 'no-preference' }],
        })
      }
    },
  },
  {
    id: 'mobile-menu-taborder',
    name: 'S4 移动菜单：打开后链接可 Tab、Esc 关闭并归还焦点',
    async run(client, context) {
      const { checks } = context
      await client.send('Emulation.setDeviceMetricsOverride', {
        width: 390,
        height: 844,
        deviceScaleFactor: 1,
        mobile: true,
      })
      try {
        await freshPage(client, context.mock)
        const initial = await dom(client)
        checks.check('移动视口下菜单按钮可见', initial.menu?.toggleVisible === true, JSON.stringify(initial.menu))
        checks.check('移动视口下导航默认收起', initial.menu?.navVisible === false, JSON.stringify(initial.menu))

        await evaluate(client, 'document.activeElement && document.activeElement.blur()')
        await press(client, 'Tab') // skip link
        await press(client, 'Tab') // brand
        await press(client, 'Tab') // menu toggle
        const toggleActive = await waitFor(
          async () => ((await active(client))?.cls || '').includes('menu-toggle') ? await active(client) : null,
          { timeout: 3000, label: 'menu toggle focused' },
        )
        checks.check('Tab 可到达移动菜单按钮', toggleActive?.cls.includes('menu-toggle') === true, JSON.stringify(toggleActive))

        await press(client, 'Enter')
        const opened = await waitFor(
          async () => {
            const current = await dom(client)
            return current.menu?.expanded === 'true' && current.menu?.navVisible === true ? current : null
          },
          { timeout: 3000, label: 'menu opened' },
        )
        checks.check('Enter 打开移动菜单（aria-expanded=true）', opened.menu?.expanded === 'true', JSON.stringify(opened.menu))
        checks.check(
          '菜单连接 aria-controls=primary-nav 且导航可见',
          opened.menu?.controls === 'primary-nav' && opened.menu?.navVisible === true,
          JSON.stringify(opened.menu),
        )
        await captureScreenshot(client, 'S4-mobile-menu')

        await press(client, 'Tab')
        const firstLink = await waitFor(
          async () => ((await active(client))?.href || '') === '#lab' ? await active(client) : null,
          { timeout: 3000, label: 'first nav link focused' },
        )
        checks.check('打开后 Tab 进入第一个导航链接', firstLink?.href === '#lab', JSON.stringify(firstLink))
        const linkRing = await ringOf(client, 'a[href="#lab"]')
        checks.check('移动菜单链接有可见焦点环', linkRing?.outlineWidth >= 2 && linkRing?.clipped === false, JSON.stringify(linkRing))

        await press(client, 'Escape')
        await waitFor(
          async () => {
            const current = await dom(client)
            return current.menu?.expanded === 'false' ? current : null
          },
          { timeout: 3000, label: 'menu closed' },
        )
        const closed = await dom(client)
        checks.check('Esc 关闭移动菜单', closed.menu?.expanded === 'false' && closed.menu?.navVisible === false, JSON.stringify(closed.menu))
        checks.check(
          '关闭后焦点回到菜单按钮',
          (await active(client))?.cls.includes('menu-toggle') === true,
          JSON.stringify(await active(client)),
        )
        return { evidence: { initial, opened, closed } }
      } finally {
        await client.send('Emulation.clearDeviceMetricsOverride')
      }
    },
  },
  {
    id: 'cpu-throttle',
    name: 'S5 6× CPU throttle（模拟）：输入延迟/滚动/按钮仍可用',
    async run(client, context) {
      const { checks } = context
      await client.send('Emulation.setDeviceMetricsOverride', {
        width: VIEWPORT.width,
        height: VIEWPORT.height,
        deviceScaleFactor: 1,
        mobile: false,
      })
      await client.send('Emulation.setCPUThrottlingRate', { rate: 6 })
      try {
        await freshPage(client, context.mock)
        const samples = []
        for (let index = 0; index < 5; index += 1) {
          const text = `性能测试标题 ${index + 1}`
          const sample = await evaluate(
            client,
            `window.__f14.measureInputLatency(${JSON.stringify(text)})`,
          )
          samples.push(sample)
        }
        const durations = samples.map((sample) => sample.ms).filter((value) => Number.isFinite(value))
        durations.sort((left, right) => left - right)
        const median = durations[Math.floor(durations.length / 2)]
        checks.check('6× 模拟下输入均成功写入并更新 DOM', samples.every((sample) => sample.ok === true && sample.value.length > 0), JSON.stringify(samples.map((sample) => sample.counter)))
        checks.check(
          '6× 模拟下 input→DOM 更新中位延迟 < 250ms',
          median < 250,
          `median=${median?.toFixed(1)}ms samples=${durations.map((value) => value.toFixed(1)).join(',')}`,
        )
        checks.check(
          '字符计数与输入长度一致',
          samples.every((sample) => String(sample.counter || '').startsWith(String(sample.titleTrimmedLength))),
          JSON.stringify(samples.map((sample) => sample.counter)),
        )
        const scroll = await evaluate(client, 'window.__f14.scrollCheck()')
        checks.check('6× 模拟下页面仍可滚动', scroll.scrollY > 0, JSON.stringify(scroll))
        const themeBefore = (await dom(client)).theme
        await evaluate(client, "window.__f14.clickSelector('.display-toggle')")
        await sleep(200)
        const themeAfter = (await dom(client)).theme
        checks.check('6× 模拟下按钮点击仍生效（主题切换）', themeBefore !== themeAfter, `${themeBefore} → ${themeAfter}`)
        await evaluate(client, "window.__f14.clickSelector('.display-toggle')")
        await evaluate(client, "window.__f14.focusSelector('#news-title')")
        await insertText(client, '键盘输入')
        checks.check(
          '6× 模拟下真实键盘输入仍可用',
          (await evaluate(client, "document.querySelector('#news-title').value")).includes('键盘输入'),
          await evaluate(client, "document.querySelector('#news-title').value"),
        )
        await captureScreenshot(client, 'S5-cpu-throttle')
        return {
          evidence: {
            simulated: 'CDP Emulation.setCPUThrottlingRate({rate:6})',
            viewport: VIEWPORT,
            samples,
            medianMs: median,
            scroll,
            theme: `${themeBefore} → ${themeAfter}`,
          },
        }
      } finally {
        await client.send('Emulation.setCPUThrottlingRate', { rate: 1 })
        await client.send('Emulation.clearDeviceMetricsOverride')
      }
    },
  },
  {
    id: 'fifty-result-cards',
    name: 'S6 50 张结果卡：可滚动、可聚焦、反馈面板可打开、更新不卡死',
    async run(client, context) {
      const { checks } = context
      await freshPage(client, context.mock, { mode: 'cards50' })
      const initial = await waitFor(
        async () => {
          const current = await dom(client)
          return current.cardMode?.cards === 50 ? current : null
        },
        { timeout: 12000, label: '50 result cards' },
      )
      checks.check('渲染 50 张真实结果卡', initial.cardMode?.cards === 50, JSON.stringify(initial.cardMode))
      checks.check(
        '50 个反馈面板的控制 id 唯一（useId 无重复）',
        initial.cardMode?.uniqueControls === 50,
        JSON.stringify({ controls: initial.cardMode?.feedbackControls?.length, unique: initial.cardMode?.uniqueControls }),
      )

      const scrollInfo = await evaluate(client, 'window.__f14.scrollToCard(49)')
      checks.check('可滚动到第 50 张卡', scrollInfo.ok === true && scrollInfo.scrollY > 0, JSON.stringify(scrollInfo))
      checks.check(
        '第 50 张卡在视口内且反馈按钮已聚焦',
        scrollInfo.rect?.bottom <= scrollInfo.innerHeight + 1 && scrollInfo.rect?.top >= -1,
        JSON.stringify(scrollInfo),
      )
      await press(client, 'Enter')
      await waitFor(
        async () => (await evaluate(client, 'window.__f14.cardSummary().openPanels')) === 1,
        { timeout: 4000, label: 'last card feedback panel' },
      )
      const opened = await evaluate(client, 'window.__f14.cardSummary()')
      checks.check('第 50 张卡的反馈面板可键盘打开', opened.openPanels === 1, JSON.stringify(opened))
      checks.check(
        '一次只打开一个面板且面板 id 唯一',
        opened.openPanelIds.length === 1 && String(opened.openPanelIds[0]).startsWith('feedback-panel-'),
        JSON.stringify(opened.openPanelIds),
      )
      await captureScreenshot(client, 'S6-fifty-cards')
      await press(client, 'Escape')
      await waitFor(
        async () => (await evaluate(client, 'window.__f14.cardSummary().openPanels')) === 0,
        { timeout: 3000, label: 'last card panel closed' },
      )
      checks.check('Esc 可关闭第 50 张卡的面板', true, 'closed')

      const update = await evaluate(client, 'window.__f14.measureCardUpdate()')
      checks.check(
        '更新一张卡后 50 张仍在、文案已更新（更新延迟 < 250ms）',
        update.count === 50 && update.ms < 250 && String(update.lastText).includes('更新后的标题'),
        JSON.stringify(update),
      )
      return { evidence: { initial: initial.cardMode, scrollInfo, opened, update } }
    },
  },
]

function findEdgePath() {
  return EDGE_CANDIDATES.find((candidate) => existsSync(candidate)) || null
}

async function launchEdge(profileDir) {
  const edgePath = findEdgePath()
  if (!edgePath) throw new Error('未找到 Microsoft Edge（msedge.exe）')
  const child = spawn(
    edgePath,
    [
      '--headless=new',
      '--disable-gpu',
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-extensions',
      '--disable-background-networking',
      '--disable-sync',
      `--remote-debugging-port=${CDP_PORT}`,
      `--user-data-dir=${profileDir}`,
      '--window-size=1440,920',
      'about:blank',
    ],
    { stdio: 'ignore', windowsHide: true },
  )
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

  console.log('=== F14 前端质量横切验收（本地 mock API + Edge headless/CDP）===')
  console.log(`[build] 构建 harness（独立 outDir: ${distDir}）`)
  await build({ configFile: join(here, 'vite.harness.config.mjs'), logLevel: 'warn' })
  console.log('[build] OK')

  const mock = await createMockServer({ port: SERVER_PORT, staticDir: distDir })
  console.log(`[mock] 本地 mock API: http://127.0.0.1:${SERVER_PORT}`)

  const profileDir = join(os.tmpdir(), `tmf-f14-edge-${process.pid}-${Date.now()}`)
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
      maxTotalBufferSize: 50_000_000,
      maxResourceBufferSize: 5_000_000,
    })
    await client.send('Runtime.enable')
    await client.send('Page.enable')
    await client.send('Emulation.setEmulatedMedia', {
      features: [{ name: 'prefers-reduced-motion', value: 'no-preference' }],
    })
    await client.send('Emulation.setDeviceMetricsOverride', {
      width: VIEWPORT.width,
      height: VIEWPORT.height,
      deviceScaleFactor: 1,
      mobile: false,
    })

    const only = (process.env.F14_ONLY || '')
      .split(',')
      .map((item) => item.trim())
      .filter(Boolean)
    const selectedScenarios = only.length
      ? SCENARIOS.filter((scenario) => only.includes(scenario.id))
      : SCENARIOS

    for (const scenario of selectedScenarios) {
      const checks = buildChecks()
      const context = { checks, mock }
      const startedAt = Date.now()
      let evidence = null
      let scenarioError = null
      try {
        const result = await scenario.run(client, context)
        evidence = result.evidence ?? null
      } catch (error) {
        scenarioError = error
        checks.check('场景执行未抛异常', false, error.stack || error.message)
      }
      const failed = checks.checks.filter((check) => !check.pass)
      failures += failed.length
      const pass = !scenarioError && failed.length === 0
      summary.push({
        id: scenario.id,
        name: scenario.name,
        pass,
        checks: checks.checks,
        elapsedMs: Date.now() - startedAt,
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
      mode: '本地 mock API + Edge headless/CDP',
      viewport: VIEWPORT,
      environment: {
        mobileViewport: '390×844（CDP device metrics，模拟）',
        cpuThrottle: '6×（CDP Emulation.setCPUThrottlingRate，模拟）',
        reducedMotion: 'CDP Emulation.setEmulatedMedia(prefers-reduced-motion: reduce)',
      },
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
    const reportPath = join(artifactDir, 'f14-report.json')
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
    // 清理本次构建产物与临时 env（artifacts/ 报告与截图保留）。
    await rm(distDir, { recursive: true, force: true }).catch(() => {})
    await rm(envDir, { recursive: true, force: true }).catch(() => {})
  }

  process.exitCode = failures === 0 ? 0 : 1
}

main().catch((error) => {
  console.error('[F14] 运行失败：', error.stack || error.message)
  process.exitCode = 1
})
