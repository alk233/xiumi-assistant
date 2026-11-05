/**
 * xiumi-assistant — Host half.
 *
 * Registers tools that drive a visible browser through a long-lived Python
 * Playwright bridge (`bridge.py`). The bridge speaks one JSON object per line
 * over stdin/stdout, so browser state (login, tab, page) survives across tool
 * calls instead of restarting per invocation.
 */
import { spawn, spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, readFileSync, readdirSync, renameSync, unlinkSync, writeFileSync } from 'node:fs'
import { createInterface } from 'node:readline'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { catalogPath, readConfig, writeConfig } from './config.js'
import { LearningLibrary } from './learning.js'
import {
  applyProfileTheme,
  buildThemedHtml,
  htmlToText,
  normalizeColorId,
  normalizeStyleId,
  resolveTheme,
  suggestColorTheme,
  themeCatalog,
} from './theme.js'

export const inject = ['tools', 'webServer']

const HERE = dirname(fileURLToPath(import.meta.url))
const BRIDGE_PY = join(HERE, 'bridge.py')
const BUNDLED_CATALOG = join(HERE, 'data', 'template-catalog.json')
const CATALOG_SCHEMA_VERSION = 1
const CATALOG_TTL_DAYS = 30

const DEFAULTS = {
  pythonPath: '',
  browserChannel: 'msedge',
  headless: false,
  loginTimeoutMs: 900000,
}

/** Optional portable interpreter fallbacks supplied by the runtime environment. */
const FALLBACK_PYTHON_PATHS = [
  process.env.XIUMI_FALLBACK_PYTHON,
  process.env.GZH_CONTENT_PYTHON,
].filter(Boolean)

function versionAtLeast(version, minimum) {
  const left = String(version || '')
    .split('.')
    .slice(0, 3)
    .map((part) => Number.parseInt(part, 10) || 0)
  const right = String(minimum || '')
    .split('.')
    .slice(0, 3)
    .map((part) => Number.parseInt(part, 10) || 0)
  for (let index = 0; index < Math.max(left.length, right.length); index += 1) {
    const a = left[index] || 0
    const b = right[index] || 0
    if (a !== b) return a > b
  }
  return true
}

function pythonUsable(candidate) {
  const value = String(candidate || '').trim()
  if (!value) return false
  if ((value.includes('\\') || value.includes('/')) && !existsSync(value)) {
    return false
  }
  const code =
    'import json; from importlib.metadata import version; ' +
    'print(json.dumps({"playwright": version("playwright")}))'
  const probe = spawnSync(value, ['-c', code], {
    encoding: 'utf8',
    windowsHide: true,
    timeout: 8000,
  })
  if (probe.status !== 0 || !probe.stdout) return false
  let payload
  try {
    const lines = String(probe.stdout).trim().split(/\r?\n/)
    payload = JSON.parse(lines[lines.length - 1])
  } catch {
    return false
  }
  return versionAtLeast(payload?.playwright, '1.40')
}

function pythonFromPath() {
  const finder = process.platform === 'win32' ? 'where' : 'which'
  const names = process.platform === 'win32' ? ['python', 'python3'] : ['python3', 'python']
  const found = []
  for (const name of names) {
    const probe = spawnSync(finder, [name], {
      encoding: 'utf8',
      windowsHide: true,
      timeout: 3000,
    })
    if (probe.status !== 0 || !probe.stdout) continue
    for (const line of probe.stdout.split(/\r?\n/)) {
      const value = line.trim()
      if (value && !found.includes(value)) found.push(value)
    }
  }
  return found
}

function resolvePython(settings) {
  const explicit = String(settings?.pythonPath || '').trim()
  const configured = explicit ? [explicit] : []
  const env = [process.env.XIUMI_PYTHON, process.env.DSH_PYTHON].filter(Boolean)
  const fromPath = pythonFromPath()
  const candidates = [...configured, ...env, ...fromPath, ...FALLBACK_PYTHON_PATHS]
  const seen = new Set()
  for (const candidate of candidates) {
    if (seen.has(candidate)) continue
    seen.add(candidate)
    if (pythonUsable(candidate)) return candidate
  }
  throw new Error(
    '未找到已安装 playwright>=1.40 的 Python 环境。' +
      '请在插件配置 pythonPath 指向满足条件的解释器，或在该解释器执行 ' +
      '`python -m pip install -U playwright==1.64.1` 后重试。' +
      (explicit ? ` 当前配置 ${explicit} 不满足条件。` : ''),
  )
}

function text(value) {
  return [{ type: 'text', text: typeof value === 'string' ? value : JSON.stringify(value, null, 2) }]
}

export class Bridge {
  constructor(settings, library = null) {
    this.settings = settings
    this.library = library
    this.child = null
    this.pending = new Map()
    this.targets = new Map()
    this.ownDrafts = new Set()
    this.seq = 0
    this.stderrTail = ''
    this.disposed = false
    this.operationTail = Promise.resolve()
    this.activeOperation = null
    this.needsReconcile = null
    this.lastStatus = { running: false, loggedIn: false, url: '' }
  }

  resolvePython() { return this.python || (this.python = resolvePython(this.settings)) }

  runExclusive(action, { signal, owner = 'shared', target } = {}) {
    const run = this.operationTail.catch(() => {}).then(async () => {
      if (this.disposed) throw new Error('秀米助手已停用')
      if (signal?.aborted) throw new Error('操作在排队期间已取消，未执行')
      this.activeOperation = { signal, owner, target }
      try { return await action() } finally { this.activeOperation = null }
    })
    this.operationTail = run.catch(() => {})
    return run
  }

  ensure() {
    if (this.child && this.child.exitCode === null && !this.child.killed) return
    if (this.disposed) throw new Error('xiumi bridge already disposed')
    const python = this.resolvePython()
    const child = spawn(python, [BRIDGE_PY, JSON.stringify(this.settings)], {
      cwd: HERE, stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true,
    })
    this.child = child
    this.stderrTail = ''
    const lines = createInterface({ input: child.stdout })
    lines.on('line', line => this.onLine(line))
    child.stderr.on('data', chunk => { this.stderrTail = (this.stderrTail + chunk.toString('utf8')).slice(-4000) })
    child.stdin.on('error', error => this.failAll(new Error('桥接写入失败，操作结果未知：' + error.message)))
    child.on('error', error => this.failAll(new Error('无法启动 Python 桥接进程：' + error.message)))
    child.on('exit', code => {
      if (this.child === child) this.child = null
      if (!this.disposed) this.failAll(new Error('桥接进程退出，未完成操作结果未知（code=' + code + '）'))
      lines.close()
    })
  }

  failAll(error) {
    for (const entry of this.pending.values()) {
      clearTimeout(entry.timer)
      entry.cleanup?.()
      if (!entry.settled) entry.reject(error)
    }
    if (this.pending.size) this.needsReconcile = { reason: 'bridgeDisconnected' }
    this.pending.clear()
  }

  onLine(line) {
    let message
    try { message = JSON.parse(line) } catch { return }
    const entry = this.pending.get(message.id)
    if (!entry) return
    this.pending.delete(message.id)
    clearTimeout(entry.timer)
    entry.cleanup?.()
    if (entry.settled) return
    if (!message.ok) {
      if (['article_write', 'article_update', 'article_save', 'template_apply', 'component_insert', 'asset_insert', 'image_insert', 'image_manifest'].includes(entry.method) || (entry.method === 'article_edit' && entry.params.action !== 'inspect') || (entry.method === 'compose' && !entry.params.dryRun)) this.needsReconcile = { reason: 'operationFailed', method: entry.method }
      const error = new Error(String(message.error || '桥接调用失败'))
      if (message.screenshotPath) error.screenshotPath = message.screenshotPath
      entry.reject(error)
      return
    }
    const result = message.result
    const context = message.context
    if (context && entry.owner && !result?.blocked && result?.ok !== false) this.targets.set(entry.owner, context)
    if (entry.method === 'status' || entry.method === 'open') this.lastStatus = result || this.lastStatus
    if (entry.method === 'article_open' && !result?.blocked) this.ownDrafts.delete(entry.owner)
    if (context?.newArticle && (entry.method === 'article_new' || (entry.method === 'compose' && entry.params.mode === 'create' && !entry.params.dryRun))) this.ownDrafts.add(entry.owner)
    const articleId = result?.articleId || context?.articleId
    try {
      if (articleId && this.ownDrafts.has(entry.owner) && !['article_open', 'article_inspect', 'reference', 'style_study'].includes(entry.method)) this.library?.rememberOwn({ articleId, url: result?.url || context?.url })
      if (entry.method === 'reference' && entry.params.action === 'learn' && result?.profile) {
        const learning = this.library?.observe({ url: entry.params.url || result.profile.sourceUrl, profile: result.observation?.profile || result.profile, observation: result.observation })
        if (learning) result.learning = learning
      }
      const learned = []
      for (const sample of result?.learningObservations || []) learned.push(this.library?.observe({ ...sample, profile: sample.observation?.profile || sample.profile }))
      if (learned.length) result.learning = { observations: learned }
    } catch (error) {
      if (result && typeof result === 'object') result.learningWarning = error.message
    }
    if (result && typeof result === 'object') delete result.learningObservations
    if (entry.method === 'article_edit' && entry.params.action === 'inspect') this.needsReconcile = null
    entry.resolve(result)
  }

  request(method, params = {}, timeoutMs = 60000) {
    const readOnly = method === 'status' || method === 'ping' || method === 'screenshot' ||
      (method === 'article_edit' && params.action === 'inspect') ||
      method === 'article_inspect' || (method === 'reference' && ['list', 'show', 'learn'].includes(params.action))
    if (this.needsReconcile && !readOnly && method !== 'shutdown') {
      throw new Error('上一操作结果未知，已阻止继续修改。请先 xiumi_edit inspect 对账；不要直接重试。')
    }
    const signal = this.activeOperation?.signal
    if (signal?.aborted) throw new Error('操作已取消，未发送桥接请求')
    const owner = this.activeOperation?.owner || 'shared'
    const targetMethods = new Set(['article_write', 'article_update', 'article_save', 'article_edit', 'template_apply', 'component_insert', 'asset_insert', 'image_insert', 'image_manifest'])
    const savedTarget = this.targets.get(owner)
    const target = targetMethods.has(method) || (method === 'compose' && params.mode === 'layout-only')
      ? { ...savedTarget, ...this.activeOperation?.target } : undefined
    if ((targetMethods.has(method) || (method === 'compose' && params.mode === 'layout-only')) && !savedTarget && !(method === 'article_edit' && params.action === 'inspect')) throw new Error('本对话尚未绑定文章，请先 xiumi_articles open/new 或 xiumi_edit inspect。')
    this.ensure()
    const id = ++this.seq
    return new Promise((resolve, reject) => {
      const entry = { resolve, reject, owner, method, params, settled: false, timer: null }
      const cancel = reason => {
        if (entry.settled || !this.pending.has(id)) return
        entry.settled = true
        clearTimeout(entry.timer)
        entry.cleanup?.()
        this.needsReconcile = { requestId: id, method, reason }
        try { this.child?.stdin.write(JSON.stringify({ method: 'cancel', params: { requestId: id } }) + '\n') } catch {}
        reject(new Error('操作已' + reason + '，已请求停止后续步骤；已发生的修改结果未知，请 inspect 对账。'))
      }
      entry.timer = setTimeout(() => cancel('超时'), timeoutMs)
      if (signal) {
        const abort = () => cancel('取消')
        signal.addEventListener('abort', abort, { once: true })
        entry.cleanup = () => signal.removeEventListener('abort', abort)
      }
      this.pending.set(id, entry)
      try {
        this.child.stdin.write(JSON.stringify({ id, method, params, target }) + '\n', error => {
          if (error) cancel('连接中断')
        })
      } catch { cancel('连接中断') }
    })
  }

  async dispose() {
    if (this.disposed) return
    this.disposed = true
    for (const [id] of this.pending) {
      try { this.child?.stdin.write(JSON.stringify({ method: 'cancel', params: { requestId: id } }) + '\n') } catch {}
    }
    this.failAll(new Error('秀米助手已停用，未完成操作请重新检查'))
    const child = this.child
    this.child = null
    if (!child || child.exitCode !== null) return
    await new Promise(resolve => {
      const timer = setTimeout(() => { try { child.kill() } catch {} resolve() }, 3000)
      child.once('exit', () => { clearTimeout(timer); resolve() })
      try { child.stdin.write(JSON.stringify({ id: -1, method: 'shutdown', params: {} }) + '\n') } catch { clearTimeout(timer); resolve() }
    })
  }
}

const STATUS_SCHEMA = {
  type: 'object',
  properties: {
    running: { type: 'boolean' },
    loggedIn: { type: 'boolean' },
    url: { type: 'string' },
    title: { type: 'string' },
    action: { type: 'string' },
    ok: { type: 'boolean' },
    blocked: { type: 'boolean' },
    unsavedChanges: { type: 'boolean' },
    message: { type: 'string' },
    relaunchedPage: { type: 'boolean' },
    relaunchedBrowser: { type: 'boolean' },
    autoSaved: { type: 'boolean' },
    autoSavedPrevious: { type: 'boolean' },
    discardedDraft: { type: 'boolean' },
    left: { type: 'boolean' },
    saved: { type: 'boolean' },
    reason: { type: 'string' },
    screenshotPath: { type: 'string' },
    savedResult: { type: 'object', additionalProperties: true },
    current: { type: 'object', additionalProperties: true },
    baseline: { type: 'object', additionalProperties: true },
    recoveredFromDirtyDraft: { type: 'object', additionalProperties: true },
  },
  required: ['running', 'loggedIn', 'url', 'title'],
  additionalProperties: false,
}

function setNoStore(res) {
  res.setHeader('Cache-Control', 'no-store, no-cache, must-revalidate')
  res.setHeader('Pragma', 'no-cache')
  res.setHeader('X-Content-Type-Options', 'nosniff')
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload)
  res.statusCode = status
  res.setHeader('Content-Type', 'application/json; charset=utf-8')
  setNoStore(res)
  res.end(body)
}

function isLoopbackAddress(address) {
  const value = String(address || '').toLowerCase()
  return (
    value === '127.0.0.1' ||
    value === '::1' ||
    value === '::ffff:127.0.0.1' ||
    value === 'localhost'
  )
}

function isAllowedHost(req, expectedOrigin) {
  const host = String(req.headers?.host || '').trim().toLowerCase()
  if (!host) return false
  let port = String(req.socket?.localPort || '')
  if (expectedOrigin) {
    try {
      const parsed = new URL(String(expectedOrigin))
      if (parsed.port) port = parsed.port
    } catch {
      return false
    }
  }
  const allowed = port
    ? [`127.0.0.1:${port}`, `localhost:${port}`]
    : ['127.0.0.1', 'localhost']
  return allowed.includes(host)
}

function isSameOriginRequest(req, expectedOrigin) {
  const site = req.headers['sec-fetch-site']
  if (site && !['same-origin', 'none'].includes(String(site).toLowerCase())) {
    return false
  }
  if (!expectedOrigin) return true

  let expected = ''
  try {
    expected = new URL(String(expectedOrigin)).origin
  } catch {
    return false
  }

  const origin = req.headers.origin
  if (origin) {
    try {
      return new URL(String(origin)).origin === expected
    } catch {
      return false
    }
  }

  const referer = req.headers.referer
  if (referer) {
    try {
      return new URL(String(referer)).origin === expected
    } catch {
      return false
    }
  }

  // 浏览器同源 fetch 通常带 Sec-Fetch-Site；没有 Origin/Referer 的本地
  // 工具调用（curl/测试）只要来自 loopback 就放行。
  return true
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    let size = 0
    const chunks = []
    req.on('data', (chunk) => {
      size += chunk.length
      if (size > 128 * 1024) {
        reject(new Error('请求体过大'))
        req.destroy()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })
}

async function readJsonBody(req) {
  const raw = await readBody(req)
  if (!raw.trim()) return {}
  try {
    const value = JSON.parse(raw)
    return value && typeof value === 'object' ? value : {}
  } catch {
    throw new Error('请求体不是合法 JSON')
  }
}

async function safeStatus(bridge) {
  if (!bridge.child || bridge.activeOperation || bridge.pending.size) return bridge.lastStatus || { running: false, loggedIn: false, url: '' }
  try {
    const status = await bridge.request('status', {}, 8000)
    return {
      running: Boolean(status?.running),
      loggedIn: Boolean(status?.loggedIn),
      url: String(status?.url ?? ''),
    }
  } catch {
    return { running: false, loggedIn: false, url: '' }
  }
}

function publicConfig(config, status, resolved) {
  return {
    account: config.account,
    password: config.password,
    stylePreset: config.stylePreset,
    colorTheme: config.colorTheme,
    hasPassword: Boolean(config.password),
    loggedIn: status.loggedIn,
    browserRunning: status.running,
    url: status.url,
    resolvedStyle: resolved.resolvedStyle,
    resolvedTheme: resolved.resolvedTheme,
    resolveReason: resolved.resolveReason,
    resolvedStyleName: resolved.resolvedStyleName,
    resolvedThemeName: resolved.resolvedThemeName,
  }
}

/**
 * 同源 HTTP 接口给设置页使用。它只读写用户主目录的 config.json，并在需要时
 * 通过常驻 bridge 调用 login_fill；密码只存在于请求/配置文件，不进入日志。
 */
export function createWebHandler(
  bridge,
  store = { read: readConfig, write: writeConfig },
  options = {},
) {
  return async function handler(req, res) {
    try {
      if (!isLoopbackAddress(req.socket?.remoteAddress)) {
        sendJson(res, 403, { ok: false, error: '仅允许本机访问秀米助手设置接口。' })
        return
      }
      const expectedOrigin =
        typeof options.getOrigin === 'function' ? options.getOrigin() : ''
      if (!isAllowedHost(req, expectedOrigin)) {
        sendJson(res, 403, { ok: false, error: '拒绝非本机 Host 的请求。' })
        return
      }
      if (!isSameOriginRequest(req, expectedOrigin)) {
        sendJson(res, 403, { ok: false, error: '拒绝跨站请求。' })
        return
      }

      const url = new URL(req.url || '/', 'http://localhost')
      const path = url.pathname
      const method = String(req.method || 'GET').toUpperCase()

      if (method === 'OPTIONS') {
        res.statusCode = 204
        setNoStore(res)
        res.end()
        return
      }

      if (path === '/__xiumi/theme' && method === 'GET') {
        sendJson(res, 200, { ok: true, ...themeCatalog() })
        return
      }

      if (path === '/__xiumi/config' && method === 'GET') {
        const config = store.read()
        const status = await safeStatus(bridge)
        const resolved = resolveTheme(config.stylePreset, config.colorTheme, {})
        sendJson(res, 200, { ok: true, ...publicConfig(config, status, resolved) })
        return
      }

      if (path === '/__xiumi/config' && method === 'POST') {
        const body = await readJsonBody(req)
        const current = store.read()
        const next = {
          account:
            typeof body.account === 'string' ? body.account : current.account,
          password:
            typeof body.password === 'string' ? body.password : current.password,
          stylePreset:
            body.stylePreset === undefined
              ? current.stylePreset
              : normalizeStyleId(body.stylePreset),
          colorTheme:
            body.colorTheme === undefined
              ? current.colorTheme
              : normalizeColorId(body.colorTheme),
        }
        const saved = store.write(next)
        sendJson(res, 200, {
          ok: true,
          hasPassword: Boolean(saved.password),
          stylePreset: saved.stylePreset,
          colorTheme: saved.colorTheme,
        })
        return
      }

      if (path === '/__xiumi/resolve' && method === 'POST') {
        const body = await readJsonBody(req)
        const config = store.read()
        const resolved = resolveTheme(config.stylePreset, config.colorTheme, {
          title: typeof body.title === 'string' ? body.title : '',
          textPreview:
            typeof body.textPreview === 'string' ? body.textPreview : '',
        })
        sendJson(res, 200, {
          ok: true,
          resolvedStyle: resolved.resolvedStyle,
          resolvedTheme: resolved.resolvedTheme,
          resolveReason: resolved.resolveReason,
          resolvedStyleName: resolved.resolvedStyleName,
          resolvedThemeName: resolved.resolvedThemeName,
        })
        return
      }

      if (path === '/__xiumi/open' && method === 'POST') {
        const status = await bridge.request('open', {}, 120000)
        sendJson(res, 200, {
          ok: true,
          running: Boolean(status?.running),
          loggedIn: Boolean(status?.loggedIn),
          url: String(status?.url ?? ''),
          title: String(status?.title ?? ''),
        })
        return
      }

      if (path === '/__xiumi/login' && method === 'POST') {
        const config = store.read()
        if (!config.account || !config.password) {
          sendJson(res, 400, {
            ok: false,
            error: '请先在设置页填写账号和密码并保存，再点击登录。',
          })
          return
        }
        const filled = await bridge.request(
          'login_fill',
          {
            account: config.account,
            password: config.password,
            submit: true,
          },
          150000,
        )
        let loggedIn = false
        let message = String(filled?.message || '')
        if (filled?.loginFailed) {
          // 秀米已明确拒绝这次登录（密码错误等），不再等待，立即返回并解除前端 busy。
          const status = await safeStatus(bridge)
          sendJson(res, 200, {
            ok: false,
            filled: true,
            submitted: true,
            loggedIn: false,
            browserRunning: status.running,
            url: status.url || String(filled?.url ?? ''),
            message: String(filled?.loginError || '登录失败：秀米提示账号或密码不正确。'),
          })
          return
        }
        if (filled?.captchaRequired) {
          // 秀米要求人机/短信验证码，必须由用户在可见窗口里手动完成，不要在这里空等。
          const status = await safeStatus(bridge)
          sendJson(res, 200, {
            ok: true,
            filled: true,
            submitted: true,
            captchaRequired: true,
            loggedIn: false,
            browserRunning: status.running,
            url: status.url || String(filled?.url ?? ''),
            message: String(
              filled?.message ||
                '秀米要求完成验证码，请在弹出的浏览器窗口中手动完成，完成后点「登录秀米」或刷新状态确认。',
            ),
          })
          return
        }
        if (filled?.filled && filled?.submitted) {
          try {
            const waited = await bridge.request(
              'login_wait',
              { timeoutMs: 12000 },
              25000,
            )
            loggedIn = Boolean(waited?.loggedIn)
            if (waited?.message) message = `${message} ${waited.message}`.trim()
          } catch (error) {
            message = `${message} 登录后等待状态超时：${
              error instanceof Error ? error.message : '未知错误'
            }`.trim()
          }
        }
        const status = await safeStatus(bridge)
        sendJson(res, 200, {
          ok: true,
          filled: Boolean(filled?.filled),
          submitted: Boolean(filled?.submitted),
          loggedIn,
          browserRunning: status.running,
          url: status.url || String(filled?.url ?? ''),
          message: message || (loggedIn ? '登录成功。' : '登录未完成，请查看可见浏览器窗口。'),
        })
        return
      }

      if (path === '/__xiumi/logout' && method === 'POST') {
        const body = await readJsonBody(req)
        const keepProfile = body.keepProfile !== false
        const clearCredentials = body.clearCredentials !== false
        let outcome
        try {
          outcome = await bridge.request(
            'logout',
            { keepProfile },
            90000,
          )
        } catch (error) {
          sendJson(res, 502, {
            ok: false,
            error: `退出失败：${
              error instanceof Error ? error.message : '浏览器桥接无响应'
            }`,
          })
          return
        }
        let credentialsCleared = false
        if (clearCredentials) {
          try {
            const current = store.read()
            store.write({
              account: '',
              password: '',
              stylePreset: current.stylePreset,
              colorTheme: current.colorTheme,
            })
            credentialsCleared = true
          } catch {
            credentialsCleared = false
          }
        }
        const status = await safeStatus(bridge)
        const parts = [
          String(outcome?.message || '').trim(),
          credentialsCleared ? '本机保存的账号与密码已清空。' : '',
        ].filter(Boolean)
        sendJson(res, 200, {
          ok: Boolean(outcome?.ok),
          loggedIn: Boolean(outcome?.loggedIn ?? status.loggedIn),
          browserRunning: Boolean(status.running),
          clearedCookies: Boolean(outcome?.clearedCookies),
          clearedStorage: Boolean(outcome?.clearedStorage),
          profileKept: Boolean(outcome?.profileKept),
          credentialsCleared,
          account: '',
          hasPassword: false,
          url: String(outcome?.url ?? status.url ?? ''),
          message:
            parts.join('') ||
            (outcome?.loggedIn
              ? '页面仍显示已登录，请稍后重试。'
              : '已退出秀米账号。'),
        })
        return
      }

      sendJson(res, 404, { ok: false, error: '接口不存在' })
    } catch {
      try {
        sendJson(res, 500, {
          ok: false,
          error: '请求处理失败，请检查配置或稍后重试。',
        })
      } catch {
        // 响应可能已结束，忽略。
      }
    }
  }
}

function browserTool(bridge) {
  return {
    name: 'xiumi_browser',
    description:
      '控制秀米助手使用的可见浏览器会话（打开/查询/关闭/站内导航/截图都在这里）。' +
      'action=open 打开（或复用）秀米首页并返回登录状态；status 只查询；close 关闭浏览器；' +
      'action=back/home/editor/list 在同一个窗口里做站内导航，像人一样点返回，不要重启浏览器或重新登录；' +
      'action=screenshot 对当前页面截图，返回图片绝对路径、URL、标题和可见文本前 500 字，' +
      '遇到失败、空白页、弹窗或不确定页面状态时先截图再判断，不要靠猜。' +
      '浏览器窗口全程可见且登录后长期驻留；只有用户明确要求关闭才 close。' +
      '当前稿有未保存改动时，close 与离开编辑器的导航会返回 unsavedChanges=true 并拒绝执行，' +
      '必须先保存或明确传 discard=true；默认绝不丢弃用户内容。',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          enum: ['open', 'status', 'close', 'back', 'home', 'editor', 'list', 'screenshot'],
        },
        discard: {
          type: 'boolean',
          description: 'close 或离开编辑器的导航时使用：true 表示明确丢弃当前未保存稿。',
        },
        name: {
          type: 'string',
          description: 'screenshot 的可选文件名标记，例如 error-article-new；不能包含路径分隔符。',
        },
        fullPage: {
          type: 'boolean',
          description: 'screenshot 是否截整页，默认 false（只截当前视口）。',
        },
      },
      required: ['action'],
      additionalProperties: false,
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, value) => text(value),
    },
    async execute(args) {
      const action = args?.action ?? 'status'
      if (action === 'screenshot') {
        return bridge.request(
          'screenshot',
          { name: args?.name, fullPage: args?.fullPage === true },
          60000,
        )
      }
      if (['back', 'home', 'editor', 'list'].includes(action)) {
        return bridge.request(
          'nav',
          { action, discard: args?.discard === true },
          120000,
        )
      }
      if (action === 'close') {
        return bridge.request('close', { discard: args?.discard === true })
      }
      return bridge.request(action === 'open' ? 'open' : 'status', {}, 120000)
    },
  }
}

function loginTool(bridge, settings, store = { read: readConfig, write: writeConfig }) {
  return {
    name: 'xiumi_login',
    description:
      '检查、等待、填入或退出秀米登录。action=check 立即返回当前登录状态；' +
      'action=wait 在可见浏览器里轮询等待用户手动完成登录（扫码/账号密码），' +
      '不关闭窗口、不超时退出。action=fill 把调用方传入的账号密码填入登录页；' +
      '账号密码只存在于本次工具调用参数中，桥接不记录、不回显、不写入文件或日志；' +
      '遇到图形/短信验证码时只提示用户在弹出的浏览器窗口中手动完成，不尝试绕过。' +
      'action=logout 清除本机浏览器里的秀米登录态（cookies + localStorage），' +
      '保留浏览器 profile 与设置页里已保存的账号密码，退出后回到未登录首页。',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', enum: ['check', 'wait', 'fill', 'logout'] },
        keepProfile: {
          type: 'boolean',
          description: 'action=logout 时是否保留浏览器 profile（默认 true；传 false 会顺带关闭浏览器）。',
        },
        timeoutMs: {
          type: 'integer',
          description: 'action=wait 时等待登录的最长毫秒数，默认 120000（2 分钟），可重复调用。',
        },
        account: {
          type: 'string',
          description: 'action=fill 时的秀米账号（手机号或邮箱）；只从本次调用读取，工具不保存。',
        },
        password: {
          type: 'string',
          description: 'action=fill 时的秀米密码；只从本次调用读取，工具不保存、不回显。',
        },
        submit: {
          type: 'boolean',
          description: 'action=fill 时是否点击「登录」提交，默认 true；验证码仍需用户手动完成。',
        },
        clearCredentials: {
          type: 'boolean',
          description:
            'action=logout 时是否同时清空本机保存的账号与密码（默认 true）；' +
            '传 false 只退出浏览器登录态，保留设置页里的凭据。',
        },
      },
      required: ['action'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          loggedIn: { type: 'boolean' },
          running: { type: 'boolean' },
          clearedCookies: { type: 'boolean' },
          clearedStorage: { type: 'boolean' },
          profileKept: { type: 'boolean' },
          filled: { type: 'boolean' },
          submitted: { type: 'boolean' },
          loginFailed: { type: 'boolean' },
          loginError: { type: 'string' },
          captchaRequired: { type: 'boolean' },
          url: { type: 'string' },
          waitedMs: { type: 'integer' },
          message: { type: 'string' },
        },
        required: ['url', 'message'],
        additionalProperties: false,
      },
      render: (_args, value) => text(value),
    },
    async execute(args) {
      const action = args?.action ?? 'check'
      if (action === 'check') {
        const status = await bridge.request('status', {}, 60000)
        const running = Boolean(status?.running)
        return {
          loggedIn: Boolean(status?.loggedIn),
          url: String(status?.url ?? ''),
          waitedMs: 0,
          message: status?.loggedIn
            ? '已登录。'
            : running
              ? '尚未登录：请在可见浏览器窗口中点击右上角「登录」，完成扫码或账号密码登录。'
              : '浏览器尚未打开：请先调用 xiumi_browser 的 open，再检查或完成登录。',
        }
      }
      if (action === 'fill') {
        if (typeof args?.account !== 'string' || !args.account.trim()) {
          throw new Error('action=fill 需要 account 参数（账号只从本次调用读取，工具不保存）。')
        }
        if (typeof args?.password !== 'string' || !args.password) {
          throw new Error('action=fill 需要 password 参数（密码只从本次调用读取，工具不保存）。')
        }
        return bridge.request(
          'login_fill',
          {
            account: args.account,
            password: args.password,
            submit: args.submit !== false,
          },
          120000,
        )
      }
      if (action === 'logout') {
        const outcome = await bridge.request(
          'logout',
          { keepProfile: args?.keepProfile !== false },
          90000,
        )
        let credentialsCleared = false
        if (args?.clearCredentials !== false) {
          try {
            const current = store.read()
            store.write({
              account: '',
              password: '',
              stylePreset: current.stylePreset,
              colorTheme: current.colorTheme,
            })
            credentialsCleared = true
          } catch {
            credentialsCleared = false
          }
        }
        const parts = [
          String(outcome?.message ?? '').trim(),
          credentialsCleared ? '本机保存的账号与密码已清空。' : '',
        ].filter(Boolean)
        return {
          loggedIn: Boolean(outcome?.loggedIn),
          running: Boolean(outcome?.running),
          clearedCookies: Boolean(outcome?.clearedCookies),
          clearedStorage: Boolean(outcome?.clearedStorage),
          profileKept: Boolean(outcome?.profileKept),
          credentialsCleared,
          url: String(outcome?.url ?? ''),
          message:
            parts.join('') ||
            (outcome?.loggedIn ? '页面仍显示已登录。' : '已退出秀米账号。'),
        }
      }
      const timeoutMs = Number.isFinite(args?.timeoutMs)
        ? Math.max(3000, Math.min(settings.loginTimeoutMs, args.timeoutMs))
        : 120000
      const result = await bridge.request('login_wait', { timeoutMs }, timeoutMs + 30000)
      return result
    },
  }
}

function articlesTool(bridge) {
  return {
    name: 'xiumi_articles',
    description:
      '秀米图文对象操作。action=list 只读列出「我的图文」（最多 50 条；未登录会明确报错）；' +
      'action=inspect 只读预览当前/指定图文，返回标题、段落数、前 N 段和正文预览，绝不保存；' +
      'action=new 新建空白图文并返回当前状态；action=open 只读打开已有图文并记录 articleId；' +
      'action=update 修改已 article_open 的图文正文（必须 confirm=true，且先展示对象）；' +
      'action=save 保存当前图文（必须 confirm=true，以页面真实提示判定成败）。' +
      '新建或修改前应把返回的对象展示给用户确认；未确认时 update/save 一律拒绝。' +
      'action=new 前会检查图文容量：剩余不足或仅剩 2 篇以内时返回 quotaBlocked=true 与 options；' +
      '必须把 options 展示给用户等待选择（优先建议修改已有图文），绝不自行删除旧文。' +
      '离开编辑器前检查完整模型；脏稿会被拦截，须显式保存验收或按授权 discard=true。' +
      '只有明确传 discard=true 才跳过保存丢弃当前改动。' +
      'action=new 会先保存旧稿，再以「取消」处理新稿的恢复弹窗，确保新稿空白，并返回 discardedDraft。' +
      'list/inspect 在独立页面读取；open 切稿不会隐式保存。查看外部图文会自动积累结构技巧。',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          enum: ['list', 'inspect', 'new', 'open', 'update', 'save'],
        },
        url: {
          type: 'string',
          description: 'action=inspect/open 时的秀米图文 URL；open 也可用 articleId。',
        },
        articleId: {
          type: 'string',
          description: 'action=open 时的秀米图文数字 ID（与 url 二选一）。',
        },
        html: {
          type: 'string',
          description: 'action=update 时的新正文 HTML；与 blocks 二选一。',
        },
        blocks: {
          type: 'array',
          items: { type: 'string' },
          description: 'action=update 时的正文段落数组；与 html 二选一。',
        },
        cellIndexes: { type: 'array', items: { type: 'integer' }, description: 'update 修改的正文格；先用 xiumi_edit inspect 核对，未选中的组件保留。' },
        confirm: {
          type: 'boolean',
          description:
            'action=update/save 的硬门禁；必须为 true。调用前应展示对象和当前内容并取得用户明确确认。',
        },
        paragraphLimit: {
          type: 'integer',
          description: 'action=inspect 返回前 N 段文本，默认 10。',
        },
        discard: {
          type: 'boolean',
          description:
            'action=new/open：明确丢弃当前未保存稿；默认 false，存在未保存改动则阻止离开。',
        },
      },
      required: ['action'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          action: { type: 'string' },
          items: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                title: { type: 'string' },
                url: { type: 'string' },
              },
              required: ['title', 'url'],
              additionalProperties: false,
            },
          },
          title: { type: 'string' },
          textPreview: { type: 'string' },
          editable: { type: 'boolean' },
          articleId: { type: 'string' },
          url: { type: 'string' },
          charCount: { type: 'integer' },
          paragraphCount: { type: 'integer' },
          imageCount: { type: 'integer' },
          paragraphs: { type: 'array', items: { type: 'string' } },
          before: {
            type: 'object',
            properties: {
              title: { type: 'string' },
              textPreview: { type: 'string' },
              charCount: { type: 'integer' },
              paragraphCount: { type: 'integer' },
            },
            required: ['title', 'textPreview', 'charCount', 'paragraphCount'],
            additionalProperties: false,
          },
          after: {
            type: 'object',
            properties: {
              title: { type: 'string' },
              textPreview: { type: 'string' },
              charCount: { type: 'integer' },
              paragraphCount: { type: 'integer' },
            },
            required: ['title', 'textPreview', 'charCount', 'paragraphCount'],
            additionalProperties: false,
          },
          writtenChars: { type: 'integer' },
          modelUpdated: { type: 'boolean' },
          confirm: { type: 'boolean' },
          saved: { type: 'boolean' },
          reason: { type: 'string' },
          quotaBlocked: { type: 'boolean' },
          blocked: { type: 'boolean' },
          unsavedChanges: { type: 'boolean' },
          current: { type: 'object', additionalProperties: true },
          baseline: { type: 'object', additionalProperties: true },
          dimension: { type: 'string' },
          used: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
          limit: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
          remaining: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
          need: { type: 'integer' },
          options: { type: 'array' },
          autoSavedPrevious: { type: 'boolean' },
          autoSaved: { type: 'boolean' },
          discardedDraft: { type: 'boolean' },
          previousSave: { type: 'object', additionalProperties: true },
          left: { type: 'boolean' },
          screenshotPath: { type: 'string' },
          recoveredFromDirtyDraft: {
            type: 'object',
            properties: {
              detected: { type: 'boolean' },
              dialogText: { type: 'string' },
              action: { type: 'string' },
              actionLabel: { type: 'string' },
              actions: { type: 'array', items: { type: 'string' } },
              policy: { type: 'string' },
              discardedDraft: { type: 'boolean' },
              restoredDraft: { type: 'boolean' },
              beforeScreenshot: { type: 'string' },
              afterScreenshot: { type: 'string' },
              dismissed: { type: 'boolean' },
              url: { type: 'string' },
              error: { type: 'string' },
            },
            additionalProperties: false,
          },
          message: { type: 'string' },
        },
        required: ['action'],
        additionalProperties: false,
      },
      render: (_args, value) => text(value),
    },
    async execute(args) {
      const action = args?.action ?? 'list'
      if (action === 'list') {
        const items = await bridge.request('article_list', {}, 120000)
        return { action: 'list', items: Array.isArray(items) ? items : [] }
      }
      if (action === 'inspect') {
        const params = {}
        const url = String(args?.url ?? '').trim()
        if (url) params.url = url
        if (args?.paragraphLimit !== undefined) params.paragraphLimit = args.paragraphLimit
        const result = await bridge.request('article_inspect', params, 120000)
        return { action: 'inspect', ...result }
      }
      if (action === 'new') {
        const result = await bridge.request(
          'article_new',
          { discard: args?.discard === true },
          120000,
        )
        return { action: 'new', ...result }
      }
      if (action === 'open') {
        const articleId = String(args?.articleId ?? '').trim()
        const url = String(args?.url ?? '').trim()
        if (!articleId && !url) {
          throw new Error('action=open 需要 articleId 或 url。')
        }
        const result = await bridge.request(
          'article_open',
          articleId
            ? { articleId, url: url || undefined, discard: args?.discard === true }
            : { url, discard: args?.discard === true },
          120000,
        )
        return { action: 'open', ...result }
      }
      if (action === 'update') {
        if (args?.confirm !== true) {
          throw new Error('action=update 需要 confirm=true；请先展示当前对象与内容并取得用户确认。')
        }
        const html = typeof args?.html === 'string' ? args.html : undefined
        const blocks = Array.isArray(args?.blocks)
          ? args.blocks.map((item) => String(item))
          : undefined
        if (!html && !blocks?.length) {
          throw new Error('action=update 需要非空的 html 或 blocks。')
        }
        const result = await bridge.request(
          'article_update',
          { html, blocks, cellIndexes: args.cellIndexes, confirm: true },
          120000,
        )
        return { action: 'update', ...result }
      }
      if (action === 'save') {
        const result = await bridge.request(
          'article_save',
          { confirm: args?.confirm === true },
          120000,
        )
        return { action: 'save', ...result }
      }
      throw new Error('xiumi_articles 的 action 必须是 list、inspect、new、open、update 或 save。')
    },
  }
}

function reviewTool(bridge) {
  return {
    name: 'xiumi_review',
    description:
      '做完一篇秀米图文后的质量审核与评分（不引入 LLM，按固定规则解释扣分）。' +
      '读取当前编辑器的标题、段落样式和正文，返回 100 分制 score、A/B/C/D 等级、五维评分明细、' +
      '具体 findings（问题 + 建议修改方案）和 metrics；只读，不保存、不修改。' +
      'agent 应把 score、findings 和建议修改方案展示给用户，再询问用户是否要按建议修改；' +
      '不要自行改动或保存。典型用法：xiumi_compose / xiumi_write 生成后立即调用本工具审核，' +
      '用户确认修改后再调用 article_update / xiumi_write。',
    parameters: {
      type: 'object',
      properties: {
        includeText: {
          type: 'boolean',
          description:
            '是否在 metrics.paragraphs 返回每段前 60 字；默认 true。设为 false 可只取评分与样式数据。',
        },
      },
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          score: { type: 'number' },
          grade: { type: 'string', enum: ['A', 'B', 'C', 'D'] },
          dimensions: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                key: { type: 'string' },
                name: { type: 'string' },
                score: { type: 'number' },
                max: { type: 'integer' },
                detail: { type: 'string' },
              },
              required: ['key', 'name', 'score', 'max', 'detail'],
              additionalProperties: false,
            },
          },
          findings: {
            type: 'array',
            items: {
              type: 'object',
              properties: {
                severity: { type: 'string', enum: ['high', 'medium', 'low'] },
                problem: { type: 'string' },
                suggestion: { type: 'string' },
              },
              required: ['severity', 'problem', 'suggestion'],
              additionalProperties: false,
            },
          },
          metrics: {
            type: 'object',
            properties: {
              title: { type: 'string' },
              url: { type: 'string' },
              articleId: { type: 'string' },
              cellCount: { type: 'integer' },
              emptySegments: { type: 'integer' },
              charCount: { type: 'integer' },
              paragraphCount: { type: 'integer' },
              fontSizes: { type: 'array', items: { type: 'number' } },
              colors: { type: 'array', items: { type: 'string' } },
              fontWeights: { type: 'array', items: { type: 'string' } },
              lineHeights: { type: 'array', items: { type: 'number' } },
              maxFontSize: { type: 'number' },
              bodyFontSize: { type: 'number' },
              hierarchyRatio: { type: 'number' },
              averageBodyChars: { type: 'number' },
              marginSpread: { type: 'number' },
              decorationSegments: { type: 'integer' },
              centeredSegments: { type: 'integer' },
              paragraphs: { type: 'array', items: { type: 'string' } },
            },
            required: [
              'title',
              'url',
              'articleId',
              'cellCount',
              'emptySegments',
              'charCount',
              'paragraphCount',
              'fontSizes',
              'colors',
              'fontWeights',
              'lineHeights',
              'maxFontSize',
              'bodyFontSize',
              'hierarchyRatio',
              'averageBodyChars',
              'marginSpread',
              'decorationSegments',
              'centeredSegments',
              'paragraphs',
            ],
            additionalProperties: false,
          },
        },
        required: ['score', 'grade', 'dimensions', 'findings', 'metrics'],
        additionalProperties: false,
      },
      render: (_args, value) => text(value),
    },
    async execute(args) {
      return bridge.request(
        'review_metrics',
        { includeText: args?.includeText !== false },
        120000,
      )
    },
  }
}

function editTool(bridge) {
  return {
    name: 'xiumi_edit',
    description:
      '在「已打开的文章」里就地增删改正文。' +
      '当用户要的是「改这篇」「在这个基础上改」时用它——先 xiumi_articles action=open 打开目标文章再编辑；' +
      '如果用户要的是「再写一篇」，那就正常走 xiumi_articles action=new + xiumi_write，不要用本工具。' +
      'action=inspect 只读列出正文 cell；replace_body 用一段 HTML 重写整篇正文' +
      '（多格文章必须先 inspect，明确 cellIndexes，只修改选中的格，保留其余组件）；' +
      'set_cell/clear_cell 按 cellIndex 改单格；append_cell 复用末尾空格追加。' +
      'html 里每个 <p> 必须自带内联 style 才有层次：小标题 18-20px、主色、加粗居中；' +
      '正文 15-16px、line-height 1.9、段距 14px。' +
      '所有编辑动作必须 confirm=true；必须先 inspect，文章变化后旧编号失效。save=true 会独立读回验收。' +
      '骨架与数值见 docs/TYPESETTING.md。',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          enum: ['inspect', 'replace_body', 'set_cell', 'clear_cell', 'append_cell'],
          description: '要执行的就地编辑动作。',
        },
        html: {
          type: 'string',
          description: '正文 HTML；replace_body 必填，set_cell/append_cell 可选（与 text 二选一）。',
        },
        text: {
          type: 'string',
          description: '纯文本内容；会被转义成单个 <p>，不能带样式。',
        },
        cellIndex: {
          type: 'integer',
          description: '目标 cell 下标（0 起，来自 action=inspect 的返回）。',
        },
        cellIndexes: { type: 'array', items: { type: 'integer' }, description: 'replace_body 的明确正文格列表，来自本次 inspect；未列出的组件不动。' },
        style: {
          type: 'object',
          description: '要合并进该 cell 的样式键值，例如 {"fontSize":"20px","color":"#C00000"}。',
        },
        save: {
          type: 'boolean',
          description: '编辑后是否立即保存并做保存后校验；默认 false。',
        },
        confirm: {
          type: 'boolean',
          description: '保存硬门禁：save=true 时必须同时传 confirm=true。',
        },
      },
      required: ['action'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          action: { type: 'string' },
          cellCount: { type: 'integer' },
          host: { type: 'integer' },
          cleared: { type: 'integer' },
          removedEmptyCells: { type: 'integer' },
          index: { type: 'integer' },
          cells: { type: 'array' },
          articleId: { type: 'string' },
          revision: { type: 'string' },
          url: { type: 'string' },
          save: { type: 'object' },
          reason: { type: 'string' },
          error: { type: 'string' },
          message: { type: 'string' },
        },
        required: ['message'],
        additionalProperties: false,
      },
      render: (_args, value) => text(value),
    },
    async execute(args) {
      const action = String(args.action || 'replace_body')
      const payload = { action }
      if (args.html !== undefined) payload.html = args.html
      if (args.text !== undefined) payload.text = args.text
      if (args.cellIndex !== undefined) payload.cellIndex = args.cellIndex
      if (args.cellIndexes !== undefined) payload.cellIndexes = args.cellIndexes
      if (args.style !== undefined) payload.style = args.style
      payload.confirm = args.confirm === true
      if (args.save === true) {
        if (args.confirm !== true) {
          return { ok: false, action, message: 'save=true 时必须同时传 confirm=true。' }
        }
        payload.save = true
      }
      const result = await bridge.request('article_edit', payload, 240000)
      const count = result?.cellCount
      return {
        ...result,
        message:
          action === 'inspect'
            ? `当前文章有 ${count ?? '?'} 个正文 cell。`
            : `已就地执行 ${action}，正文 cell 数 ${count ?? '?'}。`,
      }
    },
  }
}

function deleteCellsTool(bridge) {
  return {
    name: 'xiumi_delete_cells',
    description:
      '删除正文里的图片单元格（秀米正文中的图片是独立 cell，不能靠 replace_body 文本覆盖掉）。' +
      '用于清理反复插图后残留在文末的多余占位图，或删掉整篇里的所有图片重新插入。' +
      '走 Angular 模型：从父级 items 里摘除该 image cell。' +
      'dryRun=true 只统计不删除；limit 控制最多删几张（默认 200）。' +
      '删除后记得 xiumi_articles action=save 保存。',
    parameters: {
      type: 'object',
      properties: {
        limit: { type: 'integer', description: '最多删几张，默认 200。' },
        dryRun: { type: 'boolean', description: '只统计不删除。' },
      },
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          removed: { type: 'integer' },
          before: { type: 'integer' },
          remaining: { type: 'integer' },
          rounds: { type: 'integer' },
          dryRun: { type: 'boolean' },
          skipped: { type: 'array' },
          error: { type: 'string' },
          message: { type: 'string' },
        },
        required: ['message'],
        additionalProperties: false,
      },
      render: (_args, value) => text(value),
    },
    async execute(args) {
      const payload = {}
      if (args.limit !== undefined) payload.limit = args.limit
      if (args.dryRun === true) payload.dryRun = true
      const result = await bridge.request('delete_image_cells', payload, 300000)
      const removed = result?.removed ?? 0
      const remaining = result?.remaining ?? '?'
      return {
        ...result,
        message: payload.dryRun
          ? `当前正文有 ${result?.before ?? '?'} 张图片 cell（dryRun，未删除）。`
          : `已删除 ${removed} 张图片 cell，剩余 ${remaining} 张。`,
      }
    },
  }
}

function writeTool(bridge) {
  return {
    name: 'xiumi_write',
    description:
      '把标题与正文写入当前“新建”的秀米图文（必须先调用 xiumi_articles action=new）。' +
      '写入前应向用户展示标题与正文结构并确认；html 与 paragraphs 二选一。' +
      'stylePreset/colorTheme 缺省时读取设置页保存的主题；值为 auto 时按标题与正文自动匹配，' +
      '并在返回的 resolvedStyle/resolvedTheme/resolveReason 中说明原因。' +
      'profileId 来自 xiumi_reference，优先级高于风格/配色，会按预览链接学到的标题/正文/颜色/分隔线规则渲染。' +
      'save 默认为 true，须 confirm=true：写完保存，并从独立页面读回完整正文模型、样式、图片及顺序验收（saveVerified）。' +
      '要只预览、不保存必须显式传 save=false；save 生效需要 confirm=true。' +
      '返回的 saved 仅表示页面真实保存提示，saveVerified 才是保存后重读一致的校验；不得把 saved 当作已保存。' +
      '写入前会检查图文容量：剩余不足或只剩 2 篇以内时返回 quotaBlocked=true 与 options；必须展示给用户等待选择。',
    parameters: {
      type: 'object',
      properties: {
        title: { type: 'string', description: '图文标题。' },
        html: { type: 'string', description: '正文 HTML；与 paragraphs 二选一。' },
        paragraphs: {
          type: 'array',
          items: { type: 'string' },
          description: '正文纯文本段落数组；与 html 二选一，逐段键入。',
        },
        save: {
          type: 'boolean',
          description:
            '是否在写入后点击「保存」并做保存后校验，默认 true；要只预览不保存必须显式传 false。',
        },
        confirm: {
          type: 'boolean',
          description:
            '保存硬门禁：save=true 时必须同时传 confirm=true；确认前不得保存。',
        },
        stylePreset: {
          type: 'string',
          description:
            '风格 ID（minimal/literary/business/vivid/journal/tech/academic/finance/education/guochao/magazine/retro/dark/auto）；缺省读取设置页配置。',
        },
        colorTheme: {
          type: 'string',
          description:
            '配色 ID（vermilion/ink/forest/amber/slate/violet/mono/rose/teal/gold/indigo/plum/auto）；缺省读取设置页配置。',
        },
        profileId: {
          type: 'string',
          description: '参考排版档案 id；优先级高于 stylePreset，来自 xiumi_reference list。',
        },
      },
      required: ['title'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          title: { type: 'string' },
          textPreview: { type: 'string' },
          writtenChars: { type: 'integer' },
          charCount: { type: 'integer' },
          articleId: { type: 'string' },
          url: { type: 'string' },
          modelUpdated: { type: 'boolean' },
          saved: { type: 'boolean' },
          saveVerified: { type: 'boolean' },
          saveEvidence: { type: 'object' },
          screenshotPath: { type: 'string' },
          reason: { type: 'string' },
          message: { type: 'string' },
          stylePreset: { type: 'string' },
          colorTheme: { type: 'string' },
          resolvedStyle: { type: 'string' },
          resolvedTheme: { type: 'string' },
          resolveReason: { type: 'string' },
          appliedProfile: {
            type: 'object',
            properties: { id: { type: 'string' }, name: { type: 'string' } },
            additionalProperties: false,
          },
          appliedStyle: { type: 'string' },
          ok: { type: 'boolean' },
          quotaBlocked: { type: 'boolean' },
          dimension: { type: 'string' },
          used: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
          limit: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
          remaining: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
          need: { type: 'integer' },
          options: { type: 'array' },
        },
        required: ['message'],
        additionalProperties: false,
      },
      render: (_args, value) => text(value),
    },
    async execute(args) {
      if (args?.save !== false && args?.confirm !== true) throw new Error('默认会保存，请先确认写入并传 confirm=true；只预览请传 save=false。')
      const quota = await bridge.request('quota', { refresh: false }, 120000)
      const articles = quota?.articles || {}
      if (!articles.unknown && Number(articles.remaining) <= 2) {
        const candidates = []
        return {
          ok: false,
          quotaBlocked: true,
          dimension: 'articles',
          used: articles.used ?? null,
          limit: articles.limit ?? null,
          remaining: articles.remaining ?? null,
          need: 1,
          options: [
            { action: 'edit_existing', label: '在原有一篇上修改（推荐）', candidates },
            {
              action: 'replace_one',
              label: '删除一篇旧文后新建',
              dangerous: true,
              requiresConfirm: true,
              candidates,
            },
            { action: 'cancel', label: '先不新建' },
          ],
          message:
            `我的图文剩余容量不足：当前 ${articles.used}/${articles.limit}，剩余 ${articles.remaining} 篇。` +
            ' 请把 options 展示给用户等待选择；不得自行删除旧文或继续写入。',
          saved: false,
          title: '',
          textPreview: '',
          writtenChars: 0,
          stylePreset: '',
          colorTheme: '',
          resolvedStyle: '',
          resolvedTheme: '',
          resolveReason: '',
        }
      }
      const title = typeof args?.title === 'string' ? args.title : ''
      const html = typeof args?.html === 'string' ? args.html : undefined
      const paragraphs = Array.isArray(args?.paragraphs)
        ? args.paragraphs.map((item) => String(item))
        : undefined
      const configured = readConfig()
      const requestedStyle =
        typeof args?.stylePreset === 'string' && args.stylePreset.trim()
          ? args.stylePreset
          : configured.stylePreset
      const requestedColor =
        typeof args?.colorTheme === 'string' && args.colorTheme.trim()
          ? args.colorTheme
          : configured.colorTheme
      const contentText = [
        title,
        ...(paragraphs ?? []),
        htmlToText(html ?? ''),
      ]
        .filter(Boolean)
        .join('\n')
      let theme = resolveTheme(requestedStyle, requestedColor, {
        title,
        textPreview: contentText,
      })
      let appliedProfile
      if (typeof args?.profileId === 'string' && args.profileId.trim()) {
        const reference = await bridge.request(
          'reference',
          { action: 'show', id: args.profileId.trim() },
          30000,
        )
        theme = applyProfileTheme(theme, reference?.profile)
        appliedProfile = {
          id: reference?.profile?.id || args.profileId.trim(),
          name: reference?.profile?.name || '',
        }
      }
      const themedHtml = buildThemedHtml({
        title,
        html,
        paragraphs,
        theme,
        // 标题由 xiumi_write 单独写入 title 输入框；正文里不再重复标题。
        includeTitle: false,
      })
      const result = await bridge.request(
        'article_write',
        { title, html: themedHtml },
        120000,
      )
      const themeInfo = {
        stylePreset: theme.stylePreset,
        colorTheme: theme.colorTheme,
        resolvedStyle: theme.resolvedStyle,
        resolvedTheme: theme.resolvedTheme,
        resolveReason: theme.resolveReason,
        ...(appliedProfile ? { appliedProfile, appliedStyle: `profile:${appliedProfile.id}` } : {}),
      }
      if (args?.save !== false) {
        const saveResult = await bridge.request(
          'article_save',
          {
            confirm: args?.confirm === true,
            verify: true,
            expectedTitle: String(result?.title || title),
            expectedCharCount: Number(result?.charCount || 0),
          },
          120000,
        )
        const saved = Boolean(saveResult?.saved)
        const saveVerified = Boolean(saveResult?.saveVerified)
        let screenshotPath = String(saveResult?.screenshotPath || '')
        if (!saved || !saveVerified) {
          if (!screenshotPath) {
            try {
              const shot = await bridge.request(
                'screenshot',
                { name: 'write-save-failed' },
                30000,
              )
              screenshotPath = String(shot?.path || '')
            } catch {
              screenshotPath = ''
            }
          }
        }
        return {
          ...result,
          ...themeInfo,
          ok: saved && saveVerified,
          saved,
          saveVerified,
          saveEvidence: saveResult?.saveEvidence || {},
          articleId: String(saveResult?.articleId || result?.articleId || ''),
          ...(screenshotPath ? { screenshotPath } : {}),
          ...(saveResult?.reason ? { reason: saveResult.reason } : {}),
          message: saved && saveVerified
            ? '正文已保存，独立页面读回的文章身份、正文模型、样式、图片及顺序一致。'
            : `正文已写入，但保存未通过验证：${saveResult?.message || '未知原因'}。请在可见窗口检查，不要假定已保存。`,
        }
      }
      return {
        ...result,
        ...themeInfo,
        saved: false,
        saveVerified: false,
        message:
          `已写入编辑器（风格：${theme.resolvedStyleName}；配色：${theme.resolvedThemeName}），尚未保存。` +
          '请让用户在可见浏览器窗口中预览确认；确认后再保存。',
      }
    },
  }
}

function profileDirectory() {
  const home = process.env.USERPROFILE || process.env.HOME || ''
  return home ? join(home, '.xiumi-assistant', 'style-profiles') : ''
}

function validProfileId(value) {
  return typeof value === 'string' && /^[a-zA-Z0-9_-]{4,64}$/.test(value)
}

function readProfileFile(id) {
  if (!validProfileId(id)) return null
  const directory = profileDirectory()
  if (!directory) return null
  try {
    return JSON.parse(readFileSync(join(directory, `${id}.json`), 'utf8'))
  } catch {
    return null
  }
}

function writeProfileFile(profile) {
  if (!profile || !validProfileId(profile.id)) return false
  const directory = profileDirectory()
  if (!directory) return false
  try {
    mkdirSync(directory, { recursive: true })
    const target = join(directory, `${profile.id}.json`)
    const temp = join(directory, `.${profile.id}.${process.pid}.tmp`)
    writeFileSync(temp, JSON.stringify(profile, null, 2), { encoding: 'utf8', mode: 0o600 })
    renameSync(temp, target)
    return true
  } catch {
    return false
  }
}

function listRuntimeProfiles() {
  const directory = profileDirectory()
  if (!directory || !existsSync(directory)) return []
  const items = []
  try {
    for (const name of readdirSync(directory)) {
      if (!name.endsWith('.json')) continue
      const data = readProfileFile(name.slice(0, -5))
      if (!data || !validProfileId(data.id)) continue
      const style = data.style && typeof data.style === 'object' ? data.style : {}
      items.push({
        id: data.id,
        name: data.name || '',
        sourceUrl: data.sourceUrl || '',
        title: data.title || '',
        fetchedAt: data.fetchedAt || '',
        titleLevels: Array.isArray(style.titles) ? style.titles.length : 0,
        hasDividers: Array.isArray(style.dividers) && style.dividers.length > 0,
      })
    }
  } catch {
    return []
  }
  return items.sort((a, b) => String(b.fetchedAt).localeCompare(String(a.fetchedAt)))
}

function mergeProfileLists(primary, secondary) {
  const map = new Map()
  for (const item of [...primary, ...secondary]) {
    if (!item?.id || map.has(item.id)) continue
    map.set(item.id, item)
  }
  return [...map.values()].sort((a, b) => String(b.fetchedAt).localeCompare(String(a.fetchedAt)))
}

export function referenceTool(bridge) {
  return {
    name: 'xiumi_reference',
    description:
      '按秀米图文预览链接学习别人的排版，保存成可复用规则（不是整页 CSS）。' +
      'learn 只读打开链接，采集标题层级/正文字号行高段距/主色辅助色/分隔线/图片/装饰；' +
      'list 返回已保存档案；show 查看详情；remove 需要 confirm=true。' +
      '学到的 profileId 可传给 xiumi_compose / xiumi_write；learn 不保存、不修改源图文。',
    parameters: {
      type: 'object',
      properties: {
        action: {
          type: 'string',
          enum: ['learn', 'list', 'show', 'remove'],
          description: 'learn=学习预览链接；list=列出档案；show=查看；remove=删除。',
        },
        url: { type: 'string', description: 'learn 必填：秀米图文预览链接（http/https）。' },
        id: { type: 'string', description: 'show/remove 必填：档案 id。' },
        confirm: { type: 'boolean', description: 'remove 删除硬门禁。' },
      },
      required: ['action'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          action: { type: 'string' },
          profile: { type: 'object' },
          profiles: { type: 'array' },
          removed: { type: 'boolean' },
          id: { type: 'string' },
          fromCache: { type: 'boolean' },
          source: { type: 'string' },
          message: { type: 'string' },
        },
        required: ['action'],
        additionalProperties: false,
      },
      render: (_args, value) => text(value),
    },
    async execute(args) {
      const action = typeof args?.action === 'string' ? args.action : 'list'
      const params = { action }
      if (typeof args?.url === 'string') params.url = args.url
      if (typeof args?.id === 'string') params.id = args.id
      if (args?.confirm === true) params.confirm = true

      if (action === 'show' && typeof args?.id === 'string') {
        const local = readProfileFile(args.id)
        if (local) return { action, profile: local }
      }
      if (action === 'remove' && typeof args?.id === 'string') {
        if (args.confirm !== true) throw new Error('删除参考档案需要 confirm=true；未执行删除。')
        if (!validProfileId(args.id)) throw new Error('无效的参考档案 id。')
        let removed = false
        const path = profileDirectory()
        if (path) {
          try {
            unlinkSync(join(path, `${args.id}.json`))
            removed = true
          } catch {
            removed = false
          }
        }
        try {
          const result = await bridge.request('reference', params, 30000)
          removed = removed || Boolean(result?.removed)
        } catch {
          // 档案可能只存在于宿主运行时目录，桥接侧不存在不是错误。
        }
        return { action, id: args.id, removed }
      }

      let result
      try {
        result = await bridge.request(
          'reference',
          params,
          action === 'learn' ? 180000 : 30000,
        )
      } catch (error) {
        if (action === 'list') {
          return { action, profiles: listRuntimeProfiles() }
        }
        throw error
      }
      if (action === 'learn' && result?.profile?.id) {
        const stored = writeProfileFile(result.profile)
        return {
          action,
          ...result,
          profile: { ...result.profile, storedAt: stored ? 'runtime' : 'bundled-fallback' },
        }
      }
      if (action === 'list') {
        return {
          action,
          profiles: mergeProfileLists(listRuntimeProfiles(), result?.profiles || []),
        }
      }
      return { action, ...result }
    },
  }
}

function composeTool(bridge) {
  return {
    name: 'xiumi_compose',
    description:
      '双模式图文生成：mode=create 从零创作；mode=layout-only 只给已有稿件排版，' +
      '并在写入前对每段文本做零改动保真校验（失败则拒绝写入）。blocks 按顺序定义 heading/paragraph/quote/image；' +
      '每个文本块会生成独立的秀米 cell，图片按块顺序插入所在文本 cell 之后。' +
      '先展示标题与 blocks 并让用户确认；dryRun=true 只预检不写入；' +
      '真正写入必须 confirm=true。create 模式 save 默认 true：写完立即保存并重读标题/字数做保存后校验；' +
      '要只预览不保存必须显式传 save=false。layout-only 默认不保存，显式 save=true 才保存。' +
      'stylePreset/colorTheme 缺省读设置页；auto 会解析并返回 resolvedStyle/resolvedTheme/resolveReason。' +
      'profileId 来自 xiumi_reference，优先级最高，会把参考排版的标题/正文/颜色/分隔线规则套到每个块。',
    parameters: {
      type: 'object',
      properties: {
        mode: { type: 'string', enum: ['create', 'layout-only'] },
        title: { type: 'string', description: '标题；layout-only 时保留原标题，不使用此字段覆盖。' },
        blocks: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              type: { type: 'string', enum: ['heading', 'paragraph', 'quote', 'image'] },
              text: { type: 'string', description: '文本块内容（image 不用）。' },
              level: { type: 'integer', enum: [1, 2], description: 'heading 级别。' },
              file: { type: 'string', description: 'image 的本地图片路径（需先上传）。' },
              assetId: { type: 'string', description: 'image 的已上传图库图片 ID。' },
            },
            required: ['type'],
            additionalProperties: false,
          },
          description: '按顺序排列的块；image 的 file/assetId 二选一。',
        },
        stylePreset: { type: 'string' },
        colorTheme: { type: 'string' },
        profileId: {
          type: 'string',
          description: '参考排版档案 id；优先级高于 stylePreset，来自 xiumi_reference list。',
        },
        dryRun: { type: 'boolean', description: '只做保真预检，不写入。' },
        confirm: { type: 'boolean', description: '写入硬门禁；dryRun 时可省略。' },
        save: {
          type: 'boolean',
          description:
            'create 默认 true：写完立即保存并校验；要只预览不保存必须显式传 false。layout-only 默认 false，显式 true 才保存。',
        },
        album: { type: 'string', description: '上传图片使用的图库相册，默认「未标签图片」。' },
      },
      required: ['mode', 'blocks'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          mode: { type: 'string' },
          dryRun: { type: 'boolean' },
          fidelity: { type: 'string' },
          verifiedBlocks: { type: 'integer' },
          cellCount: { type: 'integer' },
          layoutLimit: { type: 'string' },
          imageCount: { type: 'integer' },
          firstDiff: {
            oneOf: [
              { type: 'null' },
              {
                type: 'object',
                properties: {
                  blockIndex: { type: 'integer' },
                  expected: { type: 'string' },
                  actual: { type: 'string' },
                  position: { type: 'integer' },
                },
                additionalProperties: false,
              },
            ],
          },
          generatedBlocks: { type: 'object' },
          htmlSummary: { type: 'string' },
          images: { type: 'array' },
          articleId: { type: 'string' },
          url: { type: 'string' },
          title: { type: 'string' },
          textPreview: { type: 'string' },
          charCount: { type: 'integer' },
          saved: { type: 'boolean' },
          saveVerified: { type: 'boolean' },
          saveEvidence: { type: 'object' },
          screenshotPath: { type: 'string' },
          reason: { type: 'string' },
          message: { type: 'string' },
          stylePreset: { type: 'string' },
          colorTheme: { type: 'string' },
          appliedProfile: {
            type: 'object',
            properties: { id: { type: 'string' }, name: { type: 'string' } },
            additionalProperties: false,
          },
          appliedStyle: { type: 'string' },
          resolvedStyle: { type: 'string' },
          resolvedTheme: { type: 'string' },
          resolveReason: { type: 'string' },
        },
        required: ['ok', 'mode', 'fidelity', 'verifiedBlocks', 'cellCount', 'message'],
        additionalProperties: false,
      },
      render: (_args, value) => text(value),
    },
    async execute(args) {
      const mode = args?.mode === 'layout-only' ? 'layout-only' : 'create'
      const blocks = Array.isArray(args?.blocks)
        ? args.blocks.map((block) => ({ ...block }))
        : []
      const title = typeof args?.title === 'string' ? args.title : ''
      const configured = readConfig()
      const requestedStyle =
        typeof args?.stylePreset === 'string' && args.stylePreset.trim()
          ? args.stylePreset
          : configured.stylePreset
      const requestedColor =
        typeof args?.colorTheme === 'string' && args.colorTheme.trim()
          ? args.colorTheme
          : configured.colorTheme
      const contentText = [
        title,
        ...blocks
          .filter((block) => block?.type !== 'image')
          .map((block) => String(block?.text ?? '')),
      ]
        .filter(Boolean)
        .join('\n')
      const theme = resolveTheme(requestedStyle, requestedColor, {
        title,
        textPreview: contentText,
      })
      const result = await bridge.request(
        'compose',
        {
          mode,
          title,
          blocks,
          theme,
          profileId: typeof args?.profileId === 'string' ? args.profileId : '',
          dryRun: args?.dryRun === true,
          confirm: args?.confirm === true,
          save: typeof args?.save === 'boolean' ? args.save : undefined,
          album: typeof args?.album === 'string' ? args.album : '',
        },
        300000,
      )
      return {
        ...result,
        stylePreset: theme.stylePreset,
        colorTheme: theme.colorTheme,
        resolvedStyle: theme.resolvedStyle,
        resolvedTheme: theme.resolvedTheme,
        resolveReason: theme.resolveReason,
      }
    },
  }
}

// 模板目录的用户态缓存（与 bridge.py 的 CATALOG_SCHEMA_VERSION / CATALOG_TTL_DAYS 保持一致）。
// bridge 进程在 Windows 下可能写不了用户主目录，宿主 Node 侧负责兜底持久化。
export function readCatalogFile(file) {
  try {
    const parsed = JSON.parse(readFileSync(file, 'utf8'))
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null
  } catch {
    return null
  }
}

export function catalogIsFresh(cached) {
  if (!cached || typeof cached !== 'object') return false
  if (Number(cached.schemaVersion || 0) !== 1) return false
  const stamp = Date.parse(String(cached.lastVerifiedAt || cached.capturedAt || ''))
  if (!Number.isFinite(stamp)) return false
  return Date.now() - stamp < 30 * 24 * 60 * 60 * 1000
}

export function writeCatalogFile(catalog, file = catalogPath()) {
  if (!catalog || typeof catalog !== 'object' || Array.isArray(catalog)) return false
  try {
    mkdirSync(dirname(file), { recursive: true })
    const temp = `${file}.tmp`
    writeFileSync(temp, JSON.stringify(catalog, null, 2), { encoding: 'utf8' })
    renameSync(temp, file)
    return true
  } catch {
    return false
  }
}

export function catalogTool(bridge) {
  return {
    name: 'xiumi_catalog',
    description:
      '读取秀米模板库目录结构（分组/分类名），带持久化缓存：默认 get 命中新鲜缓存时不打开模板库；' +
      'action=refresh 强制现场采集并与旧目录比对，只有结构变化（分类增删或总数变动超过 20%）才覆盖缓存。' +
      '缓存只保存公开的分类名/模板结构，不含账号、cookie 或登录态；不会保存或发布任何图文。',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', enum: ['get', 'refresh'], description: 'get 默认读缓存；refresh 强制现场采集。' },
      },
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          catalog: { type: 'object' },
          fromCache: { type: 'boolean' },
          source: { type: 'string' },
          changed: { type: 'boolean' },
          diff: { type: 'object' },
          cacheWritten: { type: 'boolean' },
          message: { type: 'string' },
        },
        required: ['message'],
        additionalProperties: false,
      },
      render: (_args, value) => text(value),
    },
    async execute(args) {
      const action = String(args?.action || 'get').toLowerCase()
      if (action !== 'get' && action !== 'refresh') {
        throw new Error('xiumi_catalog 的 action 必须是 get 或 refresh。')
      }
      if (action === 'get') {
        const cached = readCatalogFile(catalogPath())
        if (catalogIsFresh(cached)) {
          return {
            catalog: cached,
            fromCache: true,
            source: 'runtime',
            changed: false,
            diff: {},
            cacheWritten: false,
            message: '已命中用户态模板目录缓存，未打开模板库面板。',
          }
        }
      }
      const result = await bridge.request(
        'template_catalog',
        { action, refresh: action === 'refresh' },
        180000,
      )
      if (result && result.catalog) {
        // bridge 进程可能受 Windows 权限限制无法写用户主目录；宿主 Node 侧兜底持久化。
        const written = writeCatalogFile(result.catalog)
        if (written) {
          result.cacheWritten = true
          result.message = String(result.message || '')
            .replace(/ 警告：用户态缓存写入失败，本次结果未持久化。/g, '')
            .trim()
          if (result.source === 'bundled') {
            result.message = `${result.message} 已由宿主写入用户态缓存。`.trim()
          }
        } else if (!result.cacheWritten) {
          result.message = `${result.message || ''} 警告：用户态缓存写入失败，本次结果未持久化。`.trim()
        }
      }
      return result
    },
  }
}

function uploadTool(bridge) {
  return {
    name: 'xiumi_upload',
    description:
      '上传本地图片到秀米「我的图库」，供 xiumi_image_insert 插入正文。' +
      '只接受 png/jpg/jpeg/gif/webp/bmp，单文件上限 10MB；默认不覆盖同名图片。' +
      '上传会写入用户图库，必须显式传 confirm=true；不会保存或发布图文。' +
      '一次最多 20 张；结果逐项返回 ok/id/url/error，并给出图库容量。' +
      '上传前会检查图库容量：剩余不足或仅剩 5 张以内时返回 quotaBlocked=true 和 options；' +
      '此时必须把 options 展示给用户等待选择，绝不自行删除图片。',
    parameters: {
      type: 'object',
      properties: {
        paths: {
          type: 'array',
          items: { type: 'string' },
          description: '本地图片绝对或相对路径数组，最多 20 张。',
        },
        album: { type: 'string', description: '可选：目标图库相册名，缺省为「未标签图片」。' },
        confirm: { type: 'boolean', description: '必须为 true，确认写入我的图库。' },
      },
      required: ['paths'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          uploaded: { type: 'integer' },
          total: { type: 'integer' },
          failed: { type: 'integer' },
          items: { type: 'array' },
          capacity: { oneOf: [{ type: 'object' }, { type: 'null' }] },
          quotaBlocked: { type: 'boolean' },
          dimension: { type: 'string' },
          used: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
          limit: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
          remaining: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
          need: { type: 'integer' },
          options: { type: 'array' },
          autoSavedPrevious: { type: 'boolean' },
          autoSaved: { type: 'boolean' },
          discardedDraft: { type: 'boolean' },
          previousSave: { type: 'object', additionalProperties: true },
          left: { type: 'boolean' },
          screenshotPath: { type: 'string' },
          recoveredFromDirtyDraft: {
            type: 'object',
            properties: {
              detected: { type: 'boolean' },
              dialogText: { type: 'string' },
              action: { type: 'string' },
              actionLabel: { type: 'string' },
              actions: { type: 'array', items: { type: 'string' } },
              policy: { type: 'string' },
              discardedDraft: { type: 'boolean' },
              restoredDraft: { type: 'boolean' },
              beforeScreenshot: { type: 'string' },
              afterScreenshot: { type: 'string' },
              dismissed: { type: 'boolean' },
              url: { type: 'string' },
              error: { type: 'string' },
            },
            additionalProperties: false,
          },
          message: { type: 'string' },
        },
        required: ['message'],
        additionalProperties: false,
      },
      render: (_args, value) => text(value),
    },
    async execute(args) {
      return bridge.request(
        'upload_images',
        {
          paths: args?.paths,
          album: args?.album,
          confirm: args?.confirm === true,
        },
        180000,
      )
    },
  }
}

function imageInsertTool(bridge) {
  return {
    name: 'xiumi_image_insert',
    description:
      '把「我的图库」里的一张图片插入当前图文。用 imageId（xiumi_upload 返回）、path 文件名或 index 指定图片；' +
      '用 afterText 指定插入到哪段文字之后（按文案顺序配图），或用 position=top/end/整数指定文本单元格。' +
      '插入会修改当前图文，必须显式传 confirm=true；不会保存或发布。' +
      '插入前应先展示图片与目标段落让用户确认，插入后只返回编辑器状态，需用户在可见窗口预览。',
    parameters: {
      type: 'object',
      properties: {
        imageId: { type: 'string', description: '图库图片 asset_id（优先）。' },
        selector: { type: 'string', description: '图库图片标识：asset_id、文件名或序号。' },
        path: { type: 'string', description: '本地文件名，用于匹配图库中的 display_name。' },
        index: { type: 'integer', description: '图库列表中的序号。' },
        anchorText: { type: 'string', description: '插入到包含这段文字的单元格之后。' },
        position: {
          type: 'string',
          description: 'top/end，或文本单元格序号；与 anchorText 二选一。',
        },
        album: { type: 'string', description: '可选图库相册名。' },
        confirm: { type: 'boolean', description: '必须为 true，确认修改当前图文。' },
      },
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          inserted: { type: 'boolean' },
          reason: { type: 'string' },
          image: { type: 'object' },
          target: { type: 'object' },
          editor: { type: 'object' },
          message: { type: 'string' },
        },
        required: ['inserted', 'message'],
        additionalProperties: false,
      },
      render: (_args, value) => text(value),
    },
    async execute(args) {
      return bridge.request(
        'image_insert',
        {
          imageId: args?.imageId,
          selector: args?.selector,
          path: args?.path,
          index: args?.index,
          afterText: args?.anchorText,
          position: args?.position,
          album: args?.album,
          confirm: args?.confirm === true,
        },
        180000,
      )
    },
  }
}

function imageManifestTool(bridge) {
  return {
    name: 'xiumi_image_manifest',
    description:
      '按 manifest 批量上传并插入图片：manifest 可为文件路径或对象，entries 含 path 与 anchorText。' +
      '图片数与 anchor 数不一致、或某个 anchor 在当前正文找不到时，明确返回 inserted=false 与 ledger，不伪造成功。' +
      '会修改图文，必须显式传 confirm=true；不会保存或发布；插入后需用户在可见窗口按文案顺序核对。' +
      '批量上传前检查图库容量，不足时返回 quotaBlocked=true 与 options；必须展示给用户等待选择。',
    parameters: {
      type: 'object',
      properties: {
        manifest: {
          description: 'manifest JSON 文件路径，或 {images:[...],anchors:[...]} / {items:[...]} 对象/数组。',
        },
        album: { type: 'string', description: '可选图库相册名。' },
        confirm: { type: 'boolean', description: '必须为 true，确认批量修改图文。' },
      },
      required: ['manifest'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          inserted: { type: 'boolean' },
          manifestConsistent: { type: 'boolean' },
          insertedCount: { type: 'integer' },
          failedCount: { type: 'integer' },
          ledger: { type: 'array' },
          editor: { type: 'object' },
          quotaBlocked: { type: 'boolean' },
          dimension: { type: 'string' },
          used: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
          limit: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
          remaining: { oneOf: [{ type: 'integer' }, { type: 'null' }] },
          need: { type: 'integer' },
          options: { type: 'array' },
          autoSavedPrevious: { type: 'boolean' },
          autoSaved: { type: 'boolean' },
          discardedDraft: { type: 'boolean' },
          previousSave: { type: 'object', additionalProperties: true },
          left: { type: 'boolean' },
          screenshotPath: { type: 'string' },
          recoveredFromDirtyDraft: {
            type: 'object',
            properties: {
              detected: { type: 'boolean' },
              dialogText: { type: 'string' },
              action: { type: 'string' },
              actionLabel: { type: 'string' },
              actions: { type: 'array', items: { type: 'string' } },
              policy: { type: 'string' },
              discardedDraft: { type: 'boolean' },
              restoredDraft: { type: 'boolean' },
              beforeScreenshot: { type: 'string' },
              afterScreenshot: { type: 'string' },
              dismissed: { type: 'boolean' },
              url: { type: 'string' },
              error: { type: 'string' },
            },
            additionalProperties: false,
          },
          message: { type: 'string' },
        },
        required: ['inserted', 'message'],
        additionalProperties: false,
      },
      render: (_args, value) => text(value),
    },
    async execute(args) {
      return bridge.request(
        'image_manifest',
        {
          manifest: args?.manifest,
          album: args?.album,
          confirm: args?.confirm === true,
        },
        300000,
      )
    },
  }
}

function templateTool(bridge) {
  return {
    name: 'xiumi_template',
    description:
      '秀米模板库（只处理免费模板）。action=list 打开模板库并列出免费模板，' +
      'action=search 按关键词过滤，action=apply 把指定免费模板套用到当前图文。' +
      'VIP/付费模板会被跳过并在结果里返回数量与原因；不会主动保存或发布。' +
      '套用前应先展示模板标题/缩略图并让用户确认；套用后只返回编辑器状态，需用户在可见窗口预览。',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', enum: ['list', 'search', 'apply'] },
        category: { type: 'string', description: '可选分类，如「标题」「卡片」「图片」；缺省为全部。' },
        query: { type: 'string', description: 'search 的关键词；list 时可选。' },
        templateId: { type: 'string', description: 'apply 时指定模板 ID（优先于 title/index）。' },
        title: { type: 'string', description: 'apply 时用模板标题关键词定位。' },
        index: { type: 'integer', description: 'apply 时用 list 返回的 index 定位。' },
      },
      required: ['action'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          url: { type: 'string' },
          title: { type: 'string' },
          category: { type: 'string' },
          query: { type: 'string' },
          items: { type: 'array' },
          total: { type: 'integer' },
          skippedPaid: { type: 'integer' },
          skippedReasons: { type: 'array' },
          applied: { type: 'boolean' },
          template: { type: 'object' },
          editor: { type: 'object' },
          message: { type: 'string' },
        },
        required: ['message'],
        additionalProperties: false,
      },
      render: (_args, value) => text(value),
    },
    async execute(args) {
      const action = String(args?.action || '').toLowerCase()
      if (action === 'list' || action === 'search') {
        const query = typeof args?.query === 'string' ? args.query.trim() : ''
        if (action === 'search' && !query) throw new Error('xiumi_template search 需要 query。')
        return bridge.request(
          'template_list',
          { category: args?.category, query },
          120000,
        )
      }
      if (action === 'apply') {
        if (!args?.templateId && !args?.title && args?.index === undefined) {
          throw new Error('xiumi_template apply 需要 templateId、title 或 index 之一。')
        }
        return bridge.request(
          'template_apply',
          {
            templateId: args?.templateId,
            title: args?.title,
            index: args?.index,
          },
          120000,
        )
      }
      throw new Error('xiumi_template 的 action 必须是 list、search 或 apply。')
    },
  }
}

function componentTool(bridge) {
  return {
    name: 'xiumi_component',
    description:
      '插入秀米模板库里的免费组件并替换占位文字，用于喜报/标题/分割线等真实模板装饰。' +
      '用 templateId，或 query + index 定位组件；components 可一次按顺序插入多个。' +
      'setText 可直接改新组件的占位文字（original 可写「点击输入文字」「年度最佳小编奖」等，匹配为包含关系），' +
      '只写 Angular 模型 cell.text，不重建 cell、不改 style；不保存、不发布。' +
      '插入前应把组件标题和要替换的文字展示给用户确认，插入后让用户在可见窗口预览。',
    parameters: {
      type: 'object',
      properties: {
        templateId: { type: 'string', description: '免费组件模板 ID，如 85580。' },
        query: { type: 'string', description: '按关键词搜索模板，与 index 配合。' },
        index: { type: 'integer', description: 'query 搜索结果中的序号（从 0 开始）。' },
        components: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              templateId: { type: 'string' },
              query: { type: 'string' },
              index: { type: 'integer' },
              setText: {
                type: 'array',
                items: {
                  type: 'object',
                  properties: {
                    original: { type: 'string' },
                    text: { type: 'string' },
                    cellIndex: { type: 'integer' },
                  },
                  required: ['text'],
                  additionalProperties: false,
                },
              },
            },
            additionalProperties: false,
          },
          description: '按顺序插入的多个组件；每项可带自己的 setText。',
        },
        setText: {
          type: 'array',
          items: {
            type: 'object',
            properties: {
              original: { type: 'string' },
              text: { type: 'string' },
              cellIndex: { type: 'integer' },
            },
            required: ['text'],
            additionalProperties: false,
          },
          description: '替换占位文字的映射；不传 original 时按顺序填新增文字 cell。',
        },
      },
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          ok: { type: 'boolean' },
          inserted: { type: 'integer' },
          cellIndexes: { type: 'array', items: { type: 'integer' } },
          filled: { type: 'array' },
          cellCountBefore: { type: 'integer' },
          cellCountAfter: { type: 'integer' },
          componentCellCountBefore: { type: 'integer' },
          componentCellCountAfter: { type: 'integer' },
          components: { type: 'array' },
          screenshotPath: { type: 'string' },
          message: { type: 'string' },
        },
        required: ['message'],
        additionalProperties: false,
      },
      render: (_args, value) => text(value),
    },
    async execute(args) {
      if (args?.components && Array.isArray(args.components)) {
        if (!args.components.length) throw new Error('xiumi_component 的 components 不能为空。')
        return bridge.request('component_insert', { components: args.components }, 300000)
      }
      if (!args?.templateId && !args?.query) {
        throw new Error('xiumi_component 需要 templateId，或 query + index。')
      }
      return bridge.request(
        'component_insert',
        {
          templateId: args.templateId,
          query: args.query,
          index: args.index,
          setText: args.setText,
        },
        300000,
      )
    },
  }
}

function assetTool(bridge) {
  return {
    name: 'xiumi_asset',
    description:
      '秀米素材库（只处理免费素材）。action=categories 列出素材分类；' +
      'action=list 列出某分类下的免费素材；action=insert 把指定免费素材插入当前光标位置。' +
      'VIP/付费素材会跳过并计数；不会主动保存或发布。' +
      '插入前应展示素材分类/缩略图并让用户确认；插入后只返回编辑器状态，需用户在可见窗口预览。',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', enum: ['categories', 'list', 'insert'] },
        category: { type: 'string', description: '素材分类，如「形状」「插画」「图标」。' },
        subcategory: { type: 'string', description: '可选的二级分类，如「基本形状」。' },
        index: { type: 'integer', description: 'insert 时用 list 返回的 index 定位，默认 0。' },
      },
      required: ['action'],
      additionalProperties: false,
    },
    output: {
      schema: {
        type: 'object',
        properties: {
          url: { type: 'string' },
          categories: { type: 'array' },
          category: { type: 'string' },
          subcategory: { type: 'string' },
          items: { type: 'array' },
          total: { type: 'integer' },
          skippedPaid: { type: 'integer' },
          skippedReasons: { type: 'array' },
          inserted: { type: 'boolean' },
          asset: { type: 'object' },
          editor: { type: 'object' },
          message: { type: 'string' },
        },
        required: ['message'],
        additionalProperties: false,
      },
      render: (_args, value) => text(value),
    },
    async execute(args) {
      const action = String(args?.action || '').toLowerCase()
      if (action === 'categories') {
        return bridge.request('asset_categories', {}, 120000)
      }
      if (action === 'list') {
        return bridge.request(
          'asset_list',
          { category: args?.category, subcategory: args?.subcategory },
          120000,
        )
      }
      if (action === 'insert') {
        return bridge.request(
          'asset_insert',
          {
            category: args?.category,
            subcategory: args?.subcategory,
            index: args?.index === undefined ? 0 : args.index,
          },
          120000,
        )
      }
      throw new Error('xiumi_asset 的 action 必须是 categories、list 或 insert。')
    },
  }
}

function quotaTool(bridge) {
  return {
    name: 'xiumi_quota',
    description:
      '秀米账号容量管理。action=status 只读查询图库图片与「我的图文」的已用/上限；' +
      '数据来自秀米账号接口与页面，读不到时返回 unknown:true 与原因，不填假数字；进程内缓存 60 秒，refresh=true 强制重读。' +
      'action=cleanup 清理指定条目腾出容量；破坏性操作，必须 confirm=true 且 items 非空，' +
      '只删除调用方明确指定的条目，图片若声明 usedByArticles 非空会被拒绝，逐项返回 deleted/reason，部分失败如实报告。' +
      '执行 cleanup 前必须把待删条目展示给用户并让用户明确确认；不要自行选择或删除用户内容。',
    parameters: {
      type: 'object',
      properties: {
        action: { type: 'string', enum: ['status', 'cleanup'] },
        refresh: { type: 'boolean', description: 'status 时 true 强制重新读取，忽略 60 秒缓存。' },
        dimension: { type: 'string', enum: ['images', 'articles'] },
        items: {
          type: 'array',
          items: { oneOf: [{ type: 'string' }, { type: 'object' }] },
          description: 'cleanup 必填：图片 assetId/文件名，或图文 articleId；也可传 {id, usedByArticles}。',
        },
        confirm: { type: 'boolean', description: 'cleanup 必须为 true，确认执行删除。' },
      },
      required: ['action'],
      additionalProperties: false,
    },
    output: {
      schema: { type: 'object', additionalProperties: true },
      render: (_args, value) => text(value),
    },
    async execute(args) {
      const action = args?.action ?? 'status'
      if (action === 'cleanup') {
        if (args?.confirm !== true) throw new Error('cleanup 需要 confirm=true；未执行删除。')
        if (!Array.isArray(args?.items) || args.items.length === 0) {
          throw new Error('cleanup 需要非空 items；未执行删除。')
        }
        return bridge.request(
          'quota_cleanup',
          { dimension: args?.dimension, items: args?.items, confirm: true },
          180000,
        )
      }
      return bridge.request('quota', { refresh: args?.refresh === true }, 120000)
    },
  }
}

/** 制作指南有效期：超过这个时长视为知识过期，必须重新阅读。 */
export const GUIDE_TTL_MS = 60 * 60 * 1000

/**
 * 用户硬性要求摘要（出处 docs/HARD_RULES.md）。
 * 每次 guide 都随结果返回，确保创作前一定看到；保持精简，避免上下文膨胀。
 */
export const HARD_RULES_SUMMARY = [
  'R1 标题/章节标题/分割线必须用秀米模板库真实组件（xiumi_template 搜 → xiumi_component 插），不许手打文字样式顶替；禁止手打 ━━━ ✦ ❖ ★ 假扮装饰。模板库确实没有该类型时，必须明确告知用户后再手写。',
  'R2 组件配色必须与推文主题一致：先定这篇的主题色/调性，再挑协调的组件；换主题就换组件，不许沿用上一篇的配色。具体哪个色系由主题决定，没有固定绑定。',
  'R3 每一段文本一个独立 cell（xiumi_write 传多段 paragraphs 时内部逐段后插空行）。',
  'R4 图片必须按段落位置落位（xiumi_image_insert afterText 锚点），不许全堆在文末；多图必须串行——插一张后重新 xiumi_edit inspect 再定位下一张。',
  'R5 背景必须铺满整篇；秀米纸张背景插件改不了，做不到要明说，或用带底色的卡片组件包正文。',
  'R6 不要素：标题要有装饰组件、章节间要有分割或卡片，但花哨要有节奏。',
  'R7 不许反复 close/open 浏览器：登录一次常驻，站内跳转用 xiumi_browser back/home/editor/list。',
  'R8 用户说「改这篇」走 xiumi_articles open + xiumi_edit；说「再写一篇」才 new。用新建顶替改稿会留垃圾图文。',
  'R9 创作前必须先读本指南（本工具）；60 分钟内有效。',
  'R10 写完必须 xiumi_articles save(confirm=true) 且 saveVerified=true，否则不许进入下一轮修改。',
  'R11 保存后必须 xiumi_review 打分，把 score/grade/findings/建议给用户，再问是否修改。',
  'R12 卡住时 xiumi_browser screenshot，把图给用户看，不要空转。',
  'R13 账号密码不进日志/stdout/源码。',
  'R14 发布前 node scripts/privacy-check.mjs 必须 PASS(0 findings, 0 warnings)。',
  'R15 一个任务只开一次浏览器；合并测试；必要验证不许省。',
  'R16 不做冗余功能。',
  'R17 不经用户明确同意不碰 GitHub（不建仓库/不加 remote/不 push/不改设置）。',
]

export function guideTool(bridge, library, gate) {
  // 完整规范原文约 8KB，只在明确要 detail=full 时查询；默认给结构化规则 + 硬性要求摘要，避免每次调用都灌满上下文。
  const guidance = args => {
    const base = {
      ...library.guide(args),
      hardRules: HARD_RULES_SUMMARY,
      hardRulesCount: HARD_RULES_SUMMARY.length,
      hardRulesDoc: 'docs/HARD_RULES.md',
      hardRulesNotice: '以上 hardRules 是用户明确提出的硬性要求（R1–R17），创作前必须逐条遵守；违反任何一条都算交付不合格。',
    }
    if (args?.detail !== 'full') return base
    const full = { ...base }
    try {
      full.productionGuide = readFileSync(join(HERE, 'docs', 'PRODUCTION_GUIDE.md'), 'utf8')
    } catch { /* 缺文件时忽略 */ }
    try {
      full.hardRulesFull = readFileSync(join(HERE, 'docs', 'HARD_RULES.md'), 'utf8')
    } catch { /* 缺文件时忽略 */ }
    return full
  }
  return {
    name: 'xiumi_guide',
    description: '【硬规定：每次创作或修改前必须先调用本工具 action=guide】返回内容里的 hardRules 是用户明确要求的硬性规则（R1–R17），创作时必须逐条遵守——尤其 R1 标题/章节/分割线只能用秀米模板库组件不许手打、R2 组件配色须与主题一致、R3 每段独立 cell、R4 图片按段落落位。同时给出选型、写作结构、图片编排、验收步骤；detail=full 额外附完整制作指南与 HARD_RULES.md 全文。未先 guide 时 xiumi_write/xiumi_compose/xiumi_edit/xiumi_component 与新建/保存会被直接拒绝。action=learn 学习用户给的外部推文并融入通用技巧，不保存原文/标题/图片素材；重复链接不增证据，自己作品不算新样例。status 查看积累；feedback 记录已实际验证的技巧效果；mark_own 标记旧作品避免自我强化。',
    parameters: {
      type: 'object', properties: {
        action: { type: 'string', enum: ['guide', 'learn', 'status', 'feedback', 'mark_own'] },
        url: { type: 'string' }, intent: { type: 'string', description: '本次图文类型、阅读目标和限制；不需要粘贴参考原文。' },
        detail: { type: 'string', enum: ['summary', 'full'], description: 'action=guide 时返回详细程度：summary 默认只给规则，full 额外附完整排版规范原文。' },
        categories: { type: 'array', items: { type: 'string' } },
        limit: { type: 'integer' }, articleId: { type: 'string' },
        ruleIds: { type: 'array', items: { type: 'string' } },
        outcome: { type: 'string', enum: ['success', 'failure'] },
      }, required: ['action'], additionalProperties: false,
    },
    output: { schema: { type: 'object', additionalProperties: true }, render: (_args, value) => text(value) },
    async execute(args) {
      if (args.action === 'status') return { action: args.action, ...library.status() }
      if (args.action === 'guide') {
        const value = { action: args.action, ...guidance(args) }
        if (gate) {
          gate.at = Date.now()
          gate.intent = String(args.intent || '')
          gate.ruleCount = Number(value.sourceCount ?? value.rules?.length ?? 0)
        }
        return value
      }
      if (args.action === 'mark_own') return { action: args.action, result: library.rememberOwn(args) }
      if (args.action === 'feedback') return { action: args.action, result: library.feedback(args) }
      if (!args.url) throw new Error('学习需要 url；只提取布局和结构，不抓取或复用原文及图片。')
      const result = await bridge.request('reference', { action: 'learn', url: args.url }, 180000)
      return { action: args.action, learning: result.learning, profileId: result.profile?.id,
        guide: guidance(args), ...(result.learningWarning ? { learningWarning: result.learningWarning } : {}) }
    },
  }
}

/** 产出内容的工具：未读制作指南时一律拒绝。 */
const PRODUCE_TOOLS = new Set(['xiumi_write', 'xiumi_compose', 'xiumi_edit', 'xiumi_component'])
const PRODUCE_ARTICLE_ACTIONS = new Set(['new', 'save', 'update'])

/** Serialize complete tool operations; include cancellation and the caller's bound target. */
export function guardedTool(tool, bridge, library, gate) {
  const execute = tool.execute
  if (tool.parameters?.properties && ['xiumi_edit', 'xiumi_write', 'xiumi_compose', 'xiumi_articles', 'xiumi_component', 'xiumi_template', 'xiumi_asset', 'xiumi_image_insert', 'xiumi_image_manifest'].includes(tool.name)) {
    tool.parameters.properties.expectedRevision = { type: 'string', description: '最近一次 inspect 返回的 revision；文章变化时拒绝旧计划。' }
    if (!tool.parameters.properties.articleId) tool.parameters.properties.articleId = { type: 'string', description: '期望操作的文章 ID；须与绑定目标一致。' }
  }
  if (tool.output?.schema?.properties) Object.assign(tool.output.schema.properties, {
    revision: { type: 'string' }, articleId: { type: 'string' }, cellCount: { type: 'integer' }, cells: { type: 'array' },
    host: { type: 'integer' }, cleared: { type: 'integer' }, removedEmptyCells: { type: 'integer' }, index: { type: 'integer' },
    saveVerified: { type: 'boolean' }, saveFailureReasons: { type: 'array', items: { type: 'string' } },
    saveEvidence: { type: 'object', additionalProperties: true }, reopenRequired: { type: 'boolean' },
    learning: { type: 'object', additionalProperties: true }, learningWarning: { type: 'string' },
    observation: { type: 'object', additionalProperties: true },
    guidance: { type: 'object', additionalProperties: true }, layoutLimit: { type: 'string' }, imageCount: { type: 'integer' },
  })
  if (['xiumi_write', 'xiumi_compose', 'xiumi_edit', 'xiumi_component'].includes(tool.name)) {
    tool.description += ' 【创作前必做】先调 xiumi_guide action=guide，读返回的 hardRules（用户硬性规则 R1–R17）并逐条遵守：R1 标题/章节/分割线只能用秀米模板库组件、R2 配色与主题一致、R3 每段独立 cell、R4 图片按段落落位。'
  }
  tool.execute = (args, exec) => {
    const producesContent = PRODUCE_TOOLS.has(tool.name)
      || (tool.name === 'xiumi_articles' && PRODUCE_ARTICLE_ACTIONS.has(args?.action))
    if (producesContent && gate) {
      const at = Number(gate.at) || 0
      if (!at) {
        throw new Error('还没读制作指南和硬性规则，拒绝创作或修改。这是硬规定：请先调用 xiumi_guide action=guide（intent 写明本次题材、读者与限制），读完返回的 hardRules（R1–R17）再开始创作、新建、保存或修改。')
      }
      if (Date.now() - at > GUIDE_TTL_MS) {
        throw new Error('上次阅读制作指南和硬性规则已超过 60 分钟，知识可能过期。这是硬规定：请重新调用 xiumi_guide action=guide 后再创作或修改。')
      }
    }
    const candidate = exec?.agent?.id ?? exec?.agent?.session?.id
    const owner = typeof candidate === 'string' ? candidate : 'shared'
    const boundWrite = ['xiumi_write', 'xiumi_compose', 'xiumi_edit', 'xiumi_component', 'xiumi_template', 'xiumi_asset', 'xiumi_image_insert', 'xiumi_image_manifest'].includes(tool.name) || (tool.name === 'xiumi_articles' && ['open', 'new', 'save', 'update'].includes(args?.action))
    if (owner === 'shared' && boundWrite) throw new Error('缺少 DSH 对话身份，拒绝共享目标写入；请从正常 Agent 工具调用。')
    const target = {}
    if (args?.articleId) target.articleId = String(args.articleId)
    if (args?.expectedRevision) target.revision = String(args.expectedRevision)
    return bridge.runExclusive(async () => {
      const result = await execute(args, exec)
      if (tool.name === 'xiumi_compose' && args?.dryRun === true) result.guidance = library.guide({ intent: args.title || '' })
      return result
    }, { signal: exec?.signal, owner, target })
  }
  return tool
}

export function apply(ctx, config) {
  const settings = { ...DEFAULTS, ...(config ?? {}) }
  const library = new LearningLibrary()
  const bridge = new Bridge(settings, library)
  // 硬规定：本次会话必须先读制作指南。gate.at 为 0 时所有产出型工具拒绝执行。
  const gate = { at: 0, intent: '', ruleCount: 0 }
  const register = tool => ctx.tools.register(guardedTool(tool, bridge, library, gate))

  ctx.effect(() => {
    const disposers = [
      register(browserTool(bridge)),
      register(loginTool(bridge, settings)),
      register(articlesTool(bridge)),
      register(reviewTool(bridge)),
      register(writeTool(bridge)),
      register(editTool(bridge)),
      register(deleteCellsTool(bridge)),
      register(composeTool(bridge)),
      register(referenceTool(bridge)),
      register(catalogTool(bridge)),
      register(templateTool(bridge)),
      register(componentTool(bridge)),
      register(assetTool(bridge)),
      register(quotaTool(bridge)),
      register(uploadTool(bridge)),
      register(imageInsertTool(bridge)),
      register(imageManifestTool(bridge)),
      register(guideTool(bridge, library, gate)),
      ctx.webServer.register({
        kind: 'prefix',
        path: '/__xiumi',
        handler: ((handler) => (req, res) => /\/__xiumi\/(open|login)(?:\?|$)/.test(req.url || '')
          ? bridge.runExclusive(() => handler(req, res), { owner: 'settings' })
          : handler(req, res))(createWebHandler(bridge, undefined, {
          getOrigin: () =>
            typeof ctx.webServer.getOrigin === 'function'
              ? ctx.webServer.getOrigin()
              : '',
        })),
      }),
    ]
    return async () => {
      for (const dispose of disposers) {
        try {
          await dispose()
        } catch {
          /* ignore */
        }
      }
      await bridge.dispose()
    }
  })
}
