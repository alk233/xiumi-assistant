#!/usr/bin/env node
/**
 * Privacy / packaging gate for the xiumi-assistant bundle.
 *
 * Checks every text file that would ship in the npm package, plus the
 * repository metadata files. The scanner intentionally builds its own
 * sensitive literals from fragments so this file can itself be scanned by
 * the same rules without matching them.
 *
 * Usage: node scripts/privacy-check.mjs
 * Exit code: 0 = pass, 1 = at least one finding.
 */
import { existsSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { basename, extname, join, relative, sep } from 'node:path'
import { fileURLToPath } from 'node:url'

const ROOT = fileURLToPath(new URL('..', import.meta.url))
const SELF = relative(ROOT, fileURLToPath(import.meta.url)).split(sep).join('/')

const TEXT_EXTENSIONS = new Set([
  '.js', '.mjs', '.cjs', '.py', '.json', '.md', '.yml', '.yaml',
  '.txt', '.svg', '.css', '.html', '.gitignore', '.LICENSE',
])

const SKIP_DIRS = new Set([
  '.git', 'node_modules', '.tmp', '__pycache__', '.venv', 'dist', '.idea', '.vscode',
  '.pytest_cache',
])

const REQUIRED_PACKAGE_FILES = [
  'index.js', 'bridge.py', 'safety.py', 'observations.py', 'learning.js', 'client.js', 'theme.js', 'config.js',
  'cordis.patch.yml', 'locale/*.json', 'icon.svg', 'README.md', 'LICENSE',
  'data/**', 'docs/*.md', 'docs/research/*.md', 'scripts/*.mjs', 'scripts/*.py',
]

const ALLOWED_CATALOG_KEYS = new Set([
  'schemaVersion', 'capturedAt', 'lastVerifiedAt', 'sourceUrl', 'entryCount',
  'searchHint', 'sortOrder', 'tags', 'groups', 'group',
  'categories', 'category', 'templates', 'template', 'items',
  'id', 'templateId', 'title', 'name', 'free', 'isFree',
])

const SENSITIVE_LITERALS = {
  phone: ['1787', '0689', '647'].join(''),
  hostname: ['PC-2026', '0917FBCL'].join(''),
  admin: ['Admin', 'istrator'].join(''),
  userHome: ['C:', '\\\\', 'Users'].join(''),
  internalSkill: ['\\.', 'agents', '[\\\\/]', 'skills'].join(''),
  dshProfile: ['\\.', 'dsh', '[\\\\/]', 'profiles'].join(''),
  authFile: ['\\.', 'xiumi_auth', '\\.json'].join(''),
}

const RULES = [
  {
    id: 'absolute-drive-path',
    label: '盘符绝对路径',
    pattern: /(?:^|[^A-Za-z])[A-Za-z]:[\\/]/m,
    hint: '使用 ~、%USERPROFILE% 或配置/环境变量，不写死盘符。',
  },
  {
    id: 'windows-user-home',
    label: 'Windows 用户目录',
    pattern: new RegExp(SENSITIVE_LITERALS.userHome + '[\\\\/]', 'i'),
    hint: 'README/注释不要暴露本机用户目录。',
  },
  {
    id: 'admin-user',
    label: 'Administrator 用户名',
    pattern: new RegExp(SENSITIVE_LITERALS.admin, 'i'),
    hint: '移除本机用户名。',
  },
  {
    id: 'hostname',
    label: '本机主机名',
    pattern: new RegExp(SENSITIVE_LITERALS.hostname, 'i'),
    hint: '移除主机名。',
  },
  {
    id: 'internal-skill-path',
    label: '内部 skill 路径',
    pattern: new RegExp(SENSITIVE_LITERALS.internalSkill, 'i'),
    hint: '不要引用本机内部 skill 目录。',
  },
  {
    id: 'dsh-profile-path',
    label: 'DSH profile 路径',
    pattern: new RegExp(SENSITIVE_LITERALS.dshProfile, 'i'),
    hint: '不要写死 DSH profile 路径。',
  },
  {
    id: 'known-phone',
    label: '已知手机号',
    pattern: new RegExp(SENSITIVE_LITERALS.phone, 'i'),
    hint: '移除账号手机号。',
  },
  {
    id: 'legacy-auth-file',
    label: '旧登录态文件名',
    pattern: new RegExp(SENSITIVE_LITERALS.authFile, 'i'),
    hint: '不要引用旧版导出的登录文件。',
  },
  {
    id: 'session-literal',
    label: '凭据/会话字面量',
    pattern: /\b(?:storage_state|cookies?)\s*[:=]\s*[\[{'"`]/i,
    hint: '不要加载或内联登录态/cookie 值。',
  },
  {
    id: 'token-literal',
    label: 'token/session 字面量',
    pattern: /(?:session|token|authorization)\s*[:=]\s*['"`][^'"`]{8,}['"`]/i,
    hint: '移除 token/session 字面量。',
  },
  {
    id: 'password-literal',
    label: '非空密码字面量',
    pattern: /(?:password|passwd|pwd)\s*[:=]\s*['"][^'"]{1,}['"]/i,
    hint: '密码只能来自用户配置或调用参数，不能写进源码。',
  },
  {
    id: 'account-literal',
    label: '非空账号字面量',
    pattern: /\baccount\s*[:=]\s*['"][^'"]{4,}['"]/i,
    hint: '账号只能来自用户配置或调用参数。',
  },
]

const findings = []
const warnings = []
const scanned = []
const skipped = []

function rel(path) {
  return relative(ROOT, path).split(sep).join('/')
}

function walk(dir) {
  for (const name of readdirSync(dir)) {
    const full = join(dir, name)
    const info = statSync(full)
    if (info.isDirectory()) {
      if (SKIP_DIRS.has(name)) {
        skipped.push(rel(full) + '/')
        continue
      }
      walk(full)
      continue
    }
    if (info.isFile()) scanFile(full)
  }
}

function isTextFile(path) {
  const base = basename(path)
  if (base === 'LICENSE' || base === '.gitignore') return true
  return TEXT_EXTENSIONS.has(extname(path).toLowerCase())
}

function scanFile(path) {
  const name = rel(path)
  if (!isTextFile(path)) return
  if (name === SELF) {
    // The scanner contains the detection patterns by design. Its own rules
    // are verified by this run and by manual review; skip self-matching.
    return
  }
  const content = readFileSync(path, 'utf8')
  scanned.push(name)

  const lines = content.split(/\r?\n/)
  for (const rule of RULES) {
    lines.forEach((line, index) => {
      if (rule.pattern.test(line)) {
        findings.push({
          file: name,
          line: index + 1,
          rule: rule.id,
          label: rule.label,
          excerpt: line.trim().slice(0, 160),
          hint: rule.hint,
        })
      }
    })
  }

  if (/(?:^|[\\/])config\.json$/i.test(name)) {
    findings.push({
      file: name,
      rule: 'config-file',
      label: '配置文件入库',
      excerpt: name,
      hint: 'config.json 不能随包发布。',
    })
  }
  if (/\.(?:log|env)$/i.test(name) || name.endsWith('.env.local')) {
    findings.push({
      file: name,
      rule: 'log-or-env',
      label: '日志/环境文件入库',
      excerpt: name,
      hint: '日志和 .env 不能随包发布。',
    })
  }
}

function checkCatalog() {
  const candidates = [
    join(ROOT, 'data', 'template-catalog.json'),
  ]
  const existing = candidates.filter((path) => existsSync(path))
  if (existing.length === 0) {
    warnings.push('data/template-catalog.json 尚未生成（t9 模板目录缓存）；规则已预留，生成后自动扫描。')
    return
  }
  for (const path of existing) {
    const name = rel(path)
    let data
    try {
      data = JSON.parse(readFileSync(path, 'utf8'))
    } catch (error) {
      findings.push({
        file: name,
        rule: 'catalog-json',
        label: '模板目录 JSON 无法解析',
        excerpt: String(error.message || error),
        hint: '修复 JSON。',
      })
      continue
    }
    const visit = (value, trail) => {
      if (Array.isArray(value)) {
        value.forEach((item, index) => visit(item, `${trail}[${index}]`))
        return
      }
      if (value && typeof value === 'object') {
        for (const [key, child] of Object.entries(value)) {
          if (!ALLOWED_CATALOG_KEYS.has(key)) {
            findings.push({
              file: name,
              rule: 'catalog-key',
              label: '模板目录含非白名单字段',
              excerpt: `${trail}.${key}`,
              hint: '模板目录只允许分类名/id/标题/是否免费等静态字段。',
            })
          }
          visit(child, `${trail}.${key}`)
        }
      }
    }
    visit(data, '$')
  }
}

function checkPackage() {
  const path = join(ROOT, 'package.json')
  if (!existsSync(path)) {
    findings.push({
      file: 'package.json', rule: 'package-missing', label: '缺少 package.json',
      excerpt: 'package.json', hint: '补回 package.json。',
    })
    return
  }
  const pkg = JSON.parse(readFileSync(path, 'utf8'))
  if (!pkg.files || !Array.isArray(pkg.files)) {
    findings.push({
      file: 'package.json', rule: 'package-files', label: '缺少 files 清单',
      excerpt: 'files', hint: '声明发布文件清单。',
    })
    return
  }
  for (const required of REQUIRED_PACKAGE_FILES) {
    const needle = required.replace('*', '')
    if (!pkg.files.some((entry) => entry === required || String(entry).includes(needle))) {
      findings.push({
        file: 'package.json', rule: 'package-files', label: 'files 缺少运行资源',
        excerpt: required, hint: `把 ${required} 加入 files。`,
      })
    }
  }
  if (pkg.author !== 'alk233') {
    findings.push({
      file: 'package.json', rule: 'package-author', label: 'author 不是 alk233',
      excerpt: String(pkg.author), hint: '使用 author=alk233。',
    })
  }
  const emailPattern = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/
  if (emailPattern.test(JSON.stringify(pkg))) {
    findings.push({
      file: 'package.json', rule: 'package-email', label: 'package 含邮箱',
      excerpt: 'author/email', hint: '不要写私人邮箱。',
    })
  }
}

function checkReadme() {
  const path = join(ROOT, 'README.md')
  if (!existsSync(path)) {
    warnings.push('README.md 不存在。')
    return
  }
  const content = readFileSync(path, 'utf8')
  for (const section of ['设置页用法', '主题与智能模式', '模板目录缓存', '凭据与安全', '已知限制']) {
    if (!content.includes(section)) {
      warnings.push(`README.md 缺少「${section}」小节。`)
    }
  }
}

function tree() {
  const rows = []
  const visit = (dir, depth) => {
    const items = readdirSync(dir)
      .filter((name) => !SKIP_DIRS.has(name))
      .sort((a, b) => a.localeCompare(b))
    for (const name of items) {
      const full = join(dir, name)
      const info = statSync(full)
      const prefix = '  '.repeat(depth)
      if (info.isDirectory()) {
        rows.push(`${prefix}${name}/`)
        visit(full, depth + 1)
      } else {
        rows.push(`${prefix}${name}  (${info.size} B)`)
      }
    }
  }
  visit(ROOT, 0)
  return rows.join('\n')
}

walk(ROOT)
checkCatalog()
checkPackage()
checkReadme()

const status = findings.length === 0 ? 'PASS' : 'FAIL'
console.log('=== xiumi-assistant privacy check ===')
console.log(`root: ${ROOT}`)
console.log(`files scanned: ${scanned.length}`)
console.log(`skipped dirs: ${skipped.join(', ') || '(none)'}`)
console.log('')
console.log('--- checks ---')
const ruleStatus = RULES.map((rule) => {
  const count = findings.filter((item) => item.rule === rule.id).length
  return `${count === 0 ? 'PASS' : 'FAIL'}  ${rule.label} (${rule.id})${count ? ` — ${count}` : ''}`
})
console.log(ruleStatus.join('\n'))
console.log('')
if (findings.length) {
  console.log('--- findings ---')
  for (const item of findings) {
    console.log(`${item.file}:${item.line || '-'} [${item.rule}] ${item.label}`)
    console.log(`  ${item.excerpt}`)
    console.log(`  fix: ${item.hint}`)
  }
  console.log('')
}
if (warnings.length) {
  console.log('--- warnings ---')
  for (const warning of warnings) console.log(`WARN  ${warning}`)
  console.log('')
}
console.log('--- project tree (package-visible) ---')
console.log(tree())
console.log('')
console.log(`RESULT: ${status} (${findings.length} findings, ${warnings.length} warnings)`)
process.exit(findings.length === 0 ? 0 : 1)
