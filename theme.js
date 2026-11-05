/**
 * 秀米助手的风格与配色定义。
 *
 * 这里只有一份规则来源：Host 的 /__xiumi/theme 接口把同一份表交给设置页，
 * xiumi_write 也用它生成 HTML，避免客户端和宿主各维护一份副本导致漂移。
 *
 * 所有颜色都使用十六进制；stylePreset / colorTheme 的 "auto" 由 resolveTheme
 * 根据标题和正文解析，并把理由一起返回，保证用户能看到“为什么是这套”。
 */
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { homedir } from 'node:os'
import { join } from 'node:path'

const SANS =
  'system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif'
const SERIF =
  'Georgia, "Songti SC", "STSong", "Noto Serif CJK SC", "Times New Roman", serif'
const MONO =
  '"SFMono-Regular", Consolas, "Liberation Mono", Menlo, "Courier New", monospace'
const KAI = '"Kaiti SC", "KaiTi", "STKaiti", "Noto Serif CJK SC", cursive'

export const STYLE_GROUPS = [
  { id: 'classic', name: '经典组' },
  { id: 'pro', name: '专业组' },
  { id: 'special', name: '特色组' },
]

export const AUTO_STYLE = {
  id: 'auto',
  name: '智能风格',
  group: 'auto',
  badge: '智能',
  description: '不选时按文章内容自动匹配',
  icon: 'sparkles',
}

export const AUTO_COLOR = {
  id: 'auto',
  name: '自动配色',
  description: '自动',
  swatch:
    'conic-gradient(from 210deg, #C95E57, #D98324, #3D6B4E, #0F766E, #3730A3, #7E3F5C, #C95E57)',
}

const RAW_STYLES = [
  {
    id: 'minimal',
    name: '简约',
    group: 'classic',
    description: '大留白、无装饰、标题左对齐、字号克制',
    title: { size: 22, weight: 600, family: SANS, align: 'left', lineHeight: 1.35, letterSpacing: 0, color: 'text' },
    body: { size: 15, family: SANS, lineHeight: 1.8, paragraphSpacing: 18 },
    card: { border: 'none', width: 0, radius: 0, padding: 32, background: 'transparent' },
    accent: 'none',
    divider: 'none',
    quote: 'none',
    numbering: false,
    sticker: false,
    columns: false,
    paper: false,
    dark: false,
  },
  {
    id: 'literary',
    name: '文艺',
    group: 'classic',
    description: '标题衬线、细分割线、行距舒展、首段引语',
    title: { size: 24, weight: 500, family: SERIF, align: 'center', lineHeight: 1.45, letterSpacing: 1, color: 'text' },
    body: { size: 15, family: SERIF, lineHeight: 2.0, paragraphSpacing: 22 },
    card: { border: 'none', width: 0, radius: 0, padding: 34, background: 'transparent' },
    accent: 'none',
    divider: 'thin',
    quote: 'left',
    numbering: false,
    sticker: false,
    columns: false,
    paper: false,
    dark: false,
  },
  {
    id: 'business',
    name: '商务',
    group: 'classic',
    description: '深色粗体标题、编号小标题、引用框规范',
    title: { size: 24, weight: 700, family: SANS, align: 'left', lineHeight: 1.3, letterSpacing: 0, color: 'text' },
    body: { size: 15, family: SANS, lineHeight: 1.7, paragraphSpacing: 16 },
    card: { border: 'none', width: 0, radius: 0, padding: 32, background: 'transparent' },
    accent: 'bar',
    divider: 'thin',
    quote: 'box',
    numbering: true,
    sticker: false,
    columns: false,
    paper: false,
    dark: false,
  },
  {
    id: 'vivid',
    name: '活力',
    group: 'classic',
    description: '彩色强调块、粗描边卡片、图标位',
    title: { size: 26, weight: 800, family: SANS, align: 'left', lineHeight: 1.25, letterSpacing: 0, color: 'text' },
    body: { size: 15, family: SANS, lineHeight: 1.7, paragraphSpacing: 16 },
    card: { border: 'solid', width: 3, radius: 12, padding: 26, background: 'surface', shadow: true },
    accent: 'block',
    divider: 'none',
    quote: 'box',
    numbering: false,
    sticker: false,
    columns: false,
    paper: false,
    dark: false,
  },
  {
    id: 'journal',
    name: '手账',
    group: 'classic',
    description: '虚线边框、贴纸角标、手写体标题',
    title: { size: 25, weight: 500, family: KAI, align: 'left', lineHeight: 1.4, letterSpacing: 1, color: 'text' },
    body: { size: 15, family: KAI, lineHeight: 1.9, paragraphSpacing: 18 },
    card: { border: 'dashed', width: 2, radius: 6, padding: 28, background: 'transparent' },
    accent: 'none',
    divider: 'dashed',
    quote: 'left',
    numbering: false,
    sticker: true,
    columns: false,
    paper: true,
    dark: false,
  },
  {
    id: 'tech',
    name: '科技',
    group: 'pro',
    description: '等宽字标题、细网格线、蓝紫渐变强调',
    title: { size: 23, weight: 700, family: MONO, align: 'left', lineHeight: 1.35, letterSpacing: 0.5, color: 'text' },
    body: { size: 14.5, family: SANS, lineHeight: 1.75, paragraphSpacing: 16 },
    card: { border: 'solid', width: 1, radius: 8, padding: 28, background: 'surface' },
    accent: 'gradient',
    divider: 'grid',
    quote: 'box',
    numbering: false,
    sticker: false,
    columns: false,
    paper: false,
    dark: false,
  },
  {
    id: 'academic',
    name: '学术',
    group: 'pro',
    description: '严谨编号、图表注规范、引用块缩进',
    title: { size: 23, weight: 700, family: SERIF, align: 'left', lineHeight: 1.35, letterSpacing: 0, color: 'text' },
    body: { size: 15, family: SERIF, lineHeight: 1.85, paragraphSpacing: 18 },
    card: { border: 'none', width: 0, radius: 0, padding: 32, background: 'transparent' },
    accent: 'none',
    divider: 'thin',
    quote: 'indent',
    numbering: true,
    sticker: false,
    columns: false,
    paper: false,
    dark: false,
  },
  {
    id: 'finance',
    name: '财经',
    group: 'pro',
    description: '深蓝灰调、数据块高亮、要点加粗前置',
    title: { size: 24, weight: 700, family: SANS, align: 'left', lineHeight: 1.3, letterSpacing: 0, color: 'primary' },
    body: { size: 15, family: SANS, lineHeight: 1.7, paragraphSpacing: 16 },
    card: { border: 'solid', width: 1, radius: 2, padding: 28, background: 'surface' },
    accent: 'bar',
    divider: 'thin',
    quote: 'box',
    numbering: true,
    sticker: false,
    columns: false,
    paper: false,
    dark: false,
  },
  {
    id: 'education',
    name: '教育',
    group: 'pro',
    description: '圆角卡片、步骤编号、浅色底块',
    title: { size: 22, weight: 700, family: SANS, align: 'left', lineHeight: 1.35, letterSpacing: 0, color: 'text' },
    body: { size: 15, family: SANS, lineHeight: 1.8, paragraphSpacing: 18 },
    card: { border: 'solid', width: 1, radius: 18, padding: 26, background: 'surface' },
    accent: 'block',
    divider: 'none',
    quote: 'box',
    numbering: true,
    sticker: false,
    columns: false,
    paper: false,
    dark: false,
  },
  {
    id: 'guochao',
    name: '国潮',
    group: 'special',
    description: '传统纹样分隔、朱红金描边、印章落款',
    title: { size: 26, weight: 700, family: SERIF, align: 'center', lineHeight: 1.4, letterSpacing: 2, color: 'primary' },
    body: { size: 15, family: SERIF, lineHeight: 1.9, paragraphSpacing: 20 },
    card: { border: 'double', width: 3, radius: 0, padding: 30, background: 'transparent' },
    accent: 'stamp',
    divider: 'pattern',
    quote: 'box',
    numbering: false,
    sticker: false,
    columns: false,
    paper: false,
    dark: false,
  },
  {
    id: 'magazine',
    name: '杂志',
    group: 'special',
    description: '超大标题、图文分栏感、图注斜体',
    title: { size: 34, weight: 800, family: SERIF, align: 'left', lineHeight: 1.15, letterSpacing: -1, color: 'text' },
    body: { size: 14.5, family: SANS, lineHeight: 1.7, paragraphSpacing: 16 },
    card: { border: 'none', width: 0, radius: 0, padding: 30, background: 'transparent' },
    accent: 'bar',
    divider: 'thin',
    quote: 'box',
    numbering: false,
    sticker: false,
    columns: true,
    paper: false,
    dark: false,
  },
  {
    id: 'retro',
    name: '复古',
    group: 'special',
    description: '暖黄纸感、双线边框、旧印刷数字',
    title: { size: 26, weight: 700, family: MONO, align: 'center', lineHeight: 1.35, letterSpacing: 1, color: 'primary' },
    body: { size: 15, family: SERIF, lineHeight: 1.9, paragraphSpacing: 20 },
    card: { border: 'double', width: 3, radius: 0, padding: 30, background: 'transparent' },
    accent: 'stamp',
    divider: 'double',
    quote: 'box',
    numbering: false,
    sticker: false,
    columns: false,
    paper: true,
    dark: false,
  },
  {
    id: 'dark',
    name: '暗黑',
    group: 'special',
    description: '深底浅字、霓虹强调、适合深度长文',
    title: { size: 26, weight: 700, family: SANS, align: 'left', lineHeight: 1.35, letterSpacing: 0, color: 'text' },
    body: { size: 15, family: SANS, lineHeight: 1.8, paragraphSpacing: 18 },
    card: { border: 'solid', width: 1, radius: 10, padding: 30, background: 'surface', shadow: true },
    accent: 'gradient',
    divider: 'thin',
    quote: 'box',
    numbering: false,
    sticker: false,
    columns: false,
    paper: false,
    dark: true,
  },
]

export const STYLE_PRESETS = RAW_STYLES
export const STYLE_MAP = Object.fromEntries(RAW_STYLES.map((item) => [item.id, item]))

export const COLOR_THEMES = [
  { id: 'vermilion', name: '朱砂', primary: '#C95E57', secondary: '#E9967A', background: '#FFF9F6', text: '#2B2B2B' },
  { id: 'ink', name: '墨蓝', primary: '#1F3A5F', secondary: '#4A6FA5', background: '#F7F9FC', text: '#1A1A1A' },
  { id: 'forest', name: '森绿', primary: '#3D6B4E', secondary: '#7FA98C', background: '#F6FAF7', text: '#222222' },
  { id: 'amber', name: '暖橙', primary: '#D98324', secondary: '#F0B457', background: '#FFFBF3', text: '#2B2B2B' },
  { id: 'slate', name: '灰蓝', primary: '#4A5568', secondary: '#8896AB', background: '#F8FAFC', text: '#1A202C' },
  { id: 'violet', name: '紫罗兰', primary: '#6B4E9B', secondary: '#A88FD0', background: '#FAF8FE', text: '#232323' },
  { id: 'mono', name: '黑白', primary: '#111111', secondary: '#666666', background: '#FFFFFF', text: '#111111' },
  { id: 'rose', name: '玫红', primary: '#C2185B', secondary: '#F8BBD0', background: '#FFF5F7', text: '#2B2B2B' },
  { id: 'teal', name: '青碧', primary: '#0F766E', secondary: '#5EEAD4', background: '#F0FDFA', text: '#1A2E2B' },
  { id: 'gold', name: '金褐', primary: '#8B6B3D', secondary: '#D4B483', background: '#FDFBF5', text: '#2B2620' },
  { id: 'indigo', name: '靛青', primary: '#3730A3', secondary: '#818CF8', background: '#F5F5FF', text: '#1E1B4B' },
  { id: 'plum', name: '梅紫', primary: '#7E3F5C', secondary: '#C99BB0', background: '#FBF6F8', text: '#2B2026' },
]

COLOR_THEMES.forEach((item) => {
  item.swatch = item.primary
})

export const COLOR_MAP = Object.fromEntries(COLOR_THEMES.map((item) => [item.id, item]))

export const DEFAULT_STYLE_PRESET = 'auto'
export const DEFAULT_COLOR_THEME = 'auto'

const AUTO_KEYWORD_RULES = [
  {
    keywords: ['复盘', '总结', '年度', '报告'],
    stylePreset: 'academic',
    colorTheme: 'ink',
  },
  {
    keywords: ['教程', '步骤', '入门', '指南'],
    stylePreset: 'education',
    colorTheme: 'teal',
  },
  {
    keywords: ['数据', '财报', '增长', '指标'],
    stylePreset: 'finance',
    colorTheme: 'slate',
  },
  {
    keywords: ['随笔', '感悟', '生活', '旅行'],
    stylePreset: 'literary',
    colorTheme: 'gold',
  },
  {
    keywords: ['公告', '通知', '活动'],
    stylePreset: 'business',
    colorTheme: 'vermilion',
  },
  {
    keywords: ['科技', 'AI', '产品', '发布'],
    stylePreset: 'tech',
    colorTheme: 'indigo',
  },
  {
    keywords: ['节日', '传统', '文化'],
    stylePreset: 'guochao',
    colorTheme: 'vermilion',
  },
]

function homeDir() {
  return homedir()
}

function normalizeKeywordList(value) {
  if (Array.isArray(value)) {
    return value.map((item) => String(item || '').trim()).filter(Boolean)
  }
  if (typeof value === 'string') {
    return value
      .split(/[,，、\s]+/)
      .map((item) => item.trim())
      .filter(Boolean)
  }
  return []
}

function normalizeRule(raw) {
  if (!raw || typeof raw !== 'object') return null
  const stylePreset = normalizeStyleId(
    raw.stylePreset ?? raw.style ?? raw.styleId ?? raw.style_id,
  )
  const colorTheme = normalizeColorId(
    raw.colorTheme ?? raw.color ?? raw.theme ?? raw.colorId ?? raw.color_id,
  )
  if (stylePreset === 'auto' && colorTheme === 'auto') return null
  const keywords = normalizeKeywordList(
    raw.keywords ?? raw.titleKeywords ?? raw.title_keywords ?? raw.bodyKeywords,
  )
  const pattern = typeof raw.pattern === 'string' && raw.pattern.trim()
    ? raw.pattern.trim()
    : ''
  if (!keywords.length && !pattern) return null
  return {
    stylePreset,
    colorTheme,
    keywords,
    pattern,
    minScore: Number.isFinite(raw.minScore) ? Number(raw.minScore) : 3,
    reason: typeof raw.reason === 'string' ? raw.reason : '',
    source: String(raw.source || raw.name || '规则库'),
  }
}

function collectRuleFile(value, out) {
  if (!value) return
  if (Array.isArray(value)) {
    for (const item of value) collectRuleFile(item, out)
    return
  }
  if (typeof value !== 'object') return
  if (Array.isArray(value.styles)) {
    collectRuleFile(value.styles, out)
    return
  }
  if (Array.isArray(value.rules)) {
    collectRuleFile(value.rules, out)
    return
  }
  const rule = normalizeRule(value)
  if (rule) out.push(rule)
}

function readJsonFile(filePath) {
  try {
    if (!existsSync(filePath)) return null
    return JSON.parse(readFileSync(filePath, 'utf8'))
  } catch {
    return null
  }
}

/**
 * 读取用户规则库：~/.xiumi-assistant/styles.json 为主。
 * 另外可选读取 ~/.gzh-content/styles*.json —— 那是早期同名脚本留下的目录，
 * 目录不存在时静默跳过，不影响任何新用户；保留只是为了老数据的平滑迁移。
 */
function readStyleRules() {
  const out = []
  try {
    collectRuleFile(readJsonFile(join(homeDir(), '.xiumi-assistant', 'styles.json')), out)
    const legacyDir = join(homeDir(), '.gzh-content')
    collectRuleFile(readJsonFile(join(legacyDir, 'styles.json')), out)
    const dir = join(legacyDir, 'styles')
    if (existsSync(dir)) {
      for (const file of readdirSync(dir).slice(0, 80)) {
        if (!/\.json$/i.test(file)) continue
        collectRuleFile(readJsonFile(join(dir, file)), out)
      }
    }
  } catch {
    // 规则库只是增强项；读不到时继续走关键词规则，不影响主流程。
  }
  return out
}

function matchKeyword(text, keyword) {
  if (!text || !keyword) return false
  if (/^[\x00-\x7F]+$/.test(keyword)) {
    return text.toLowerCase().includes(keyword.toLowerCase())
  }
  return text.includes(keyword)
}

function ruleScore(rule, title, body) {
  let score = 0
  let bodyHits = 0
  let matched = ''
  for (const keyword of rule.keywords) {
    if (matchKeyword(title, keyword)) {
      score = Math.max(score, 3)
      if (!matched) matched = keyword
    }
    if (matchKeyword(body, keyword)) {
      score += 1
      bodyHits += 1
      if (!matched) matched = keyword
    }
  }
  if (rule.pattern) {
    try {
      const re = new RegExp(rule.pattern, 'i')
      if (re.test(title)) {
        score = Math.max(score, 3)
        if (!matched) matched = rule.pattern
      }
      if (re.test(body)) {
        score += 1
        bodyHits += 1
        if (!matched) matched = rule.pattern
      }
    } catch {
      // 无效正则忽略。
    }
  }
  return { score, bodyHits, matched }
}

function ruleReason(rule, matched) {
  if (rule.reason) return rule.reason
  const styleNameValue = styleName(rule.stylePreset)
  const colorNameValue = colorName(rule.colorTheme)
  return `规则库命中「${matched || rule.source}」，匹配${styleNameValue} + ${colorNameValue}`
}

function keywordMatch(title, body) {
  for (const rule of AUTO_KEYWORD_RULES) {
    for (const keyword of rule.keywords) {
      if (matchKeyword(title, keyword)) {
        return { ...rule, matched: keyword, where: '标题' }
      }
    }
  }
  for (const rule of AUTO_KEYWORD_RULES) {
    for (const keyword of rule.keywords) {
      if (matchKeyword(body, keyword)) {
        return { ...rule, matched: keyword, where: '正文' }
      }
    }
  }
  return null
}

/**
 * 解析 auto：规则库优先，其次标题关键词，其次正文关键词，最后兜底。
 * 返回的 reason 是一句可展示的解释，不会只给一个黑盒结果。
 */
export function resolveAutoTheme(title = '', textPreview = '') {
  const titleText = String(title || '')
  const bodyText = String(textPreview || '')

  let best = null
  for (const rule of readStyleRules()) {
    const result = ruleScore(rule, titleText, bodyText)
    if (
      result.score >= rule.minScore ||
      (result.bodyHits >= 2 && result.score >= 2)
    ) {
      if (!best || result.score > best.score) {
        best = { rule, result }
      }
    }
  }
  if (best) {
    return {
      stylePreset: best.rule.stylePreset,
      colorTheme: best.rule.colorTheme,
      reason: ruleReason(best.rule, best.result.matched),
      source: 'rules',
    }
  }

  const matched = keywordMatch(titleText, bodyText)
  if (matched) {
    const styleNameValue = styleName(matched.stylePreset)
    const colorNameValue = colorName(matched.colorTheme)
    return {
      stylePreset: matched.stylePreset,
      colorTheme: matched.colorTheme,
      reason: `${matched.where}含「${matched.matched}」，匹配${styleNameValue} + ${colorNameValue}`,
      source: 'keywords',
    }
  }

  return {
    stylePreset: 'minimal',
    colorTheme: 'vermilion',
    reason: '未命中关键词，使用简约 + 朱砂兜底',
    source: 'fallback',
  }
}

export function normalizeStyleId(value) {
  const id = String(value || 'auto').trim()
  if (id === 'auto') return 'auto'
  return STYLE_MAP[id] ? id : 'auto'
}

export function normalizeColorId(value) {
  const id = String(value || 'auto').trim()
  if (id === 'auto') return 'auto'
  return COLOR_MAP[id] ? id : 'auto'
}

export function styleName(id) {
  if (id === 'auto') return AUTO_STYLE.name
  return STYLE_MAP[id]?.name ?? '简约'
}

export function colorName(id) {
  if (id === 'auto') return AUTO_COLOR.name
  return COLOR_MAP[id]?.name ?? '朱砂'
}

export function themeCatalog() {
  return {
    groups: STYLE_GROUPS,
    styles: STYLE_PRESETS,
    colors: COLOR_THEMES,
    autoStyle: AUTO_STYLE,
    autoColor: AUTO_COLOR,
    defaults: { stylePreset: DEFAULT_STYLE_PRESET, colorTheme: DEFAULT_COLOR_THEME },
  }
}

function cloneThemeBase(style, color) {
  const dark = Boolean(style.dark)
  const background = dark ? '#141414' : color.background
  const text = dark ? '#F4F4F6' : color.text
  const surface = dark ? 'rgba(255,255,255,0.065)' : 'rgba(0,0,0,0.035)'
  return {
    style,
    color,
    dark,
    palette: {
      primary: color.primary,
      secondary: color.secondary,
      background,
      text,
      surface,
      baseBackground: color.background,
    },
  }
}

/**
 * 把 stylePreset / colorTheme（可能是 auto）解析成真正可用的主题。
 * 即使两个都是 auto，也会在 content 里查规则库和关键词，并返回 resolved* 与理由。
 */
export function resolveTheme(stylePreset, colorTheme, content = {}) {
  const wantStyle = normalizeStyleId(stylePreset)
  const wantColor = normalizeColorId(colorTheme)
  const wantsAuto = wantStyle === 'auto' || wantColor === 'auto'
  const auto = wantsAuto
    ? resolveAutoTheme(content.title || '', content.textPreview || '')
    : null

  const resolvedStyle = wantStyle === 'auto' ? auto.stylePreset : wantStyle
  const resolvedTheme = wantColor === 'auto' ? auto.colorTheme : wantColor
  const style = STYLE_MAP[resolvedStyle] || STYLE_MAP.minimal
  const color = COLOR_MAP[resolvedTheme] || COLOR_MAP.vermilion

  let resolveReason
  if (!auto) {
    resolveReason = `已手动选择${style.name} + ${color.name}`
  } else if (wantStyle === 'auto' && wantColor === 'auto') {
    resolveReason = auto.reason
  } else if (wantStyle === 'auto') {
    resolveReason = `按内容匹配${style.name}（配色已手动设为${color.name}）`
  } else {
    resolveReason = `按内容匹配${color.name}（风格已手动设为${style.name}）`
  }

  const base = cloneThemeBase(style, color)
  return {
    stylePreset: resolvedStyle,
    colorTheme: resolvedTheme,
    resolvedStyle,
    resolvedTheme,
    resolvedStyleName: style.name,
    resolvedThemeName: color.name,
    resolveReason,
    reason: resolveReason,
    ...base,
  }
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function hexToRgba(hex, alpha) {
  const value = String(hex || '').replace('#', '')
  const full =
    value.length === 3
      ? value.split('').map((char) => char + char).join('')
      : value.padEnd(6, '0').slice(0, 6)
  const number = Number.parseInt(full, 16)
  if (!Number.isFinite(number)) return `rgba(0,0,0,${alpha})`
  const r = (number >> 16) & 255
  const g = (number >> 8) & 255
  const b = number & 255
  return `rgba(${r},${g},${b},${alpha})`
}

function renderDivider(style, palette) {
  const color = palette.primary
  if (style.divider === 'none') return ''
  if (style.divider === 'double') {
    return `<div style="height:0;border-top:3px double ${color};margin:16px 0 20px;opacity:.8;"></div>`
  }
  if (style.divider === 'dashed') {
    return `<div style="height:0;border-top:2px dashed ${color};margin:16px 0 20px;opacity:.6;"></div>`
  }
  if (style.divider === 'grid') {
    return `<div style="height:8px;margin:16px 0 20px;background:repeating-linear-gradient(90deg,${color} 0 1px,transparent 1px 12px);opacity:.55;"></div>`
  }
  if (style.divider === 'pattern') {
    return `<div style="height:9px;margin:16px 0 20px;background:repeating-linear-gradient(45deg,${color} 0 2px,transparent 2px 9px);opacity:.5;"></div>`
  }
  return `<div style="height:1px;margin:16px 0 20px;background:${hexToRgba(color, 0.35)};"></div>`
}

function renderAccent(style, palette) {
  if (style.accent === 'bar') {
    return `<div style="width:64px;height:5px;border-radius:99px;background:${palette.primary};margin:14px 0 18px;"></div>`
  }
  if (style.accent === 'gradient') {
    return `<div style="width:100%;height:5px;background:linear-gradient(90deg,${palette.primary},${palette.secondary});margin:14px 0 18px;border-radius:99px;"></div>`
  }
  if (style.accent === 'block') {
    return `<div style="display:inline-block;background:${palette.primary};color:#fff;padding:7px 14px;border-radius:4px;font-size:12px;letter-spacing:2px;margin:14px 0 18px;">◆ 主题</div>`
  }
  return ''
}

function renderSticker(style, palette) {
  if (style.id === 'guochao') {
    return `<div style="position:absolute;right:18px;top:18px;width:44px;height:44px;border:2px solid #C9A24B;color:${palette.primary};display:flex;align-items:center;justify-content:center;font-size:18px;font-weight:700;transform:rotate(-6deg);font-family:${SERIF};">印</div>`
  }
  if (style.sticker) {
    return `<div style="position:absolute;right:14px;top:14px;background:${palette.secondary};color:#fff;padding:7px 10px;font-size:12px;transform:rotate(7deg);font-family:${KAI};">手账</div>`
  }
  return ''
}

function renderParagraph(textValue, index, style, palette) {
  const content = escapeHtml(textValue)
  const number = String(index + 1).padStart(2, '0')
  let extra = ''
  if (index === 0 && style.quote === 'left') {
    extra = `border-left:3px solid ${palette.primary};padding-left:14px;`
  } else if (style.quote === 'box') {
    extra = `background:${palette.surface};border-left:3px solid ${palette.primary};padding:12px 14px;border-radius:6px;`
  } else if (style.quote === 'indent') {
    extra = 'text-indent:2em;'
  }
  const paragraphStyle = `margin:0 0 ${style.body.paragraphSpacing}px;font-size:${style.body.size}px;line-height:${style.body.lineHeight};color:${palette.text};${extra}`
  if (style.numbering) {
    return `<div style="display:flex;gap:12px;align-items:flex-start;margin:0 0 ${style.body.paragraphSpacing}px;"><span style="flex:0 0 auto;display:inline-block;min-width:24px;font-family:${MONO};font-size:12px;color:${palette.primary};padding-top:3px;">${number}</span><p style="${paragraphStyle}margin:0;">${content}</p></div>`
  }
  return `<p style="${paragraphStyle}">${content}</p>`
}

/**
 * 生成带主题的正文 HTML。标题会作为正文标题块写入（秀米标题输入框另外填写），
 * 这样保存后的文章仍能保留标题字号/字重与主题强调。
 */
export function applyProfileTheme(theme, profile) {
  if (!theme || !theme.style || !theme.palette) {
    throw new Error('applyProfileTheme 需要一个已解析的主题')
  }
  const style = profile?.style && typeof profile.style === 'object' ? profile.style : {}
  const body = style.body && typeof style.body === 'object' ? style.body : {}
  const palette = style.palette && typeof style.palette === 'object' ? style.palette : {}
  const titles = Array.isArray(style.titles) ? style.titles : []
  const next = JSON.parse(JSON.stringify(theme))
  const title = titles.find((item) => Number(item?.fontSize) > 0)
  if (title) {
    next.style.title.size = Number(title.fontSize) || next.style.title.size
    if (title.fontWeight) next.style.title.weight = title.fontWeight
    if (Number(title.lineHeight) > 0) next.style.title.lineHeight = Number(title.lineHeight)
    next.style.title.color = 'primary'
  }
  if (Number(body.fontSize) > 0) next.style.body.size = Number(body.fontSize)
  if (Number(body.lineHeight) > 0) next.style.body.lineHeight = Number(body.lineHeight)
  if (Number(body.paragraphGap) >= 0) next.style.body.paragraphSpacing = Number(body.paragraphGap)
  if (Number(body.letterSpacing) >= 0) next.style.body.letterSpacing = Number(body.letterSpacing)
  if (palette.primary) next.palette.primary = palette.primary
  if (palette.text) next.palette.text = palette.text
  if (palette.background) next.palette.background = palette.background
  if (Array.isArray(palette.accents) && palette.accents.length) {
    next.palette.surface = hexToRgba(palette.accents[0], 0.1)
  }
  if (Array.isArray(style.dividers) && style.dividers.length && next.style.divider === 'none') {
    next.style.divider = 'thin'
  }
  next.profile = {
    id: profile?.id || '',
    name: profile?.name || '',
    missing: Array.isArray(profile?.missing) ? profile.missing : [],
  }
  return next
}

export function buildThemedHtml({ title, html, paragraphs, theme, includeTitle = true }) {
  if (!theme || !theme.style || !theme.palette) {
    throw new Error('buildThemedHtml 需要一个已解析的主题')
  }
  const style = theme.style
  const palette = theme.palette
  const dark = Boolean(theme.dark)
  const titleColor =
    style.title.color === 'primary' ? palette.primary : palette.text
  const titleStyle = [
    `font-family:${style.title.family}`,
    `font-size:${style.title.size}px`,
    `font-weight:${style.title.weight}`,
    `line-height:${style.title.lineHeight}`,
    `text-align:${style.title.align}`,
    `letter-spacing:${style.title.letterSpacing}px`,
    `color:${titleColor}`,
    `margin:0 0 14px`,
  ].join(';')

  let body = ''
  if (Array.isArray(paragraphs) && paragraphs.length) {
    body = paragraphs
      .map((item, index) => renderParagraph(item, index, style, palette))
      .join('')
  } else if (typeof html === 'string' && html.trim()) {
    body = `<div style="font-size:${style.body.size}px;line-height:${style.body.lineHeight};color:${palette.text};">${html}</div>`
  }

  if (style.columns && body) {
    body = `<div style="column-count:2;column-gap:32px;">${body}</div>`
  }

  let inner = ''
  if (includeTitle && title) {
    inner += `<h1 style="${titleStyle}">${escapeHtml(title)}</h1>`
  }
  inner += renderAccent(style, palette)
  inner += renderDivider(style, palette)
  inner += body
  if (style.id === 'guochao' || style.sticker) {
    inner = renderSticker(style, palette) + inner
  }

  let border = '0'
  if (style.card.border !== 'none') {
    const borderColor =
      style.id === 'guochao' ? '#C9A24B' : dark ? 'rgba(255,255,255,0.18)' : hexToRgba(palette.primary, 0.45)
    border = `${style.card.width}px ${style.card.border} ${borderColor}`
  }
  const background =
    style.card.background === 'surface' ? palette.surface : palette.background
  const shadow = style.card.shadow
    ? dark
      ? '0 12px 32px rgba(0,0,0,0.35)'
      : '0 12px 32px rgba(0,0,0,0.08)'
    : 'none'
  const sectionStyle = [
    `box-sizing:border-box`,
    `position:relative`,
    `overflow:hidden`,
    `background:${background}`,
    `color:${palette.text}`,
    `padding:${style.card.padding}px`,
    `border:${border}`,
    `border-radius:${style.card.radius}px`,
    `box-shadow:${shadow}`,
    `font-family:${style.body.family}`,
    `font-size:${style.body.size}px`,
    `line-height:${style.body.lineHeight}`,
    `word-break:break-word`,
  ].join(';')
  return `<section style="${sectionStyle}">${inner}</section>`
}

function parseColor(value) {
  const textValue = String(value || '').trim()
  if (/^#[0-9a-f]{3}$/i.test(textValue) || /^#[0-9a-f]{6}$/i.test(textValue)) {
    const hex = textValue.slice(1)
    const full =
      hex.length === 3 ? hex.split('').map((char) => char + char).join('') : hex
    const number = Number.parseInt(full, 16)
    return {
      r: (number >> 16) & 255,
      g: (number >> 8) & 255,
      b: number & 255,
    }
  }
  const match = textValue.match(/rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i)
  if (match) {
    return { r: Number(match[1]), g: Number(match[2]), b: Number(match[3]) }
  }
  return null
}

export function suggestColorTheme(primary) {
  const target = parseColor(primary)
  if (!target) return 'vermilion'
  let best = COLOR_THEMES[0]
  let bestDistance = Number.POSITIVE_INFINITY
  for (const item of COLOR_THEMES) {
    const candidate = parseColor(item.primary)
    if (!candidate) continue
    const distance =
      (target.r - candidate.r) ** 2 +
      (target.g - candidate.g) ** 2 +
      (target.b - candidate.b) ** 2
    if (distance < bestDistance) {
      bestDistance = distance
      best = item
    }
  }
  return best.id
}

export function htmlToText(value) {
  return String(value || '')
    .replace(/<script[\s\S]*?<\/script>/gi, ' ')
    .replace(/<style[\s\S]*?<\/style>/gi, ' ')
    .replace(/<[^>]*>/g, ' ')
    .replace(/&nbsp;/gi, ' ')
    .replace(/&amp;/gi, '&')
    .replace(/&lt;/gi, '<')
    .replace(/&gt;/gi, '>')
    .replace(/\s+/g, ' ')
    .trim()
}
