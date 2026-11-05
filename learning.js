/** Persistent, content-free layout evidence for the Xiumi agent. Node ESM only. */
import {
  closeSync, existsSync, fsyncSync, mkdirSync, openSync, readFileSync,
  renameSync, unlinkSync, writeFileSync,
} from 'node:fs'
import { createHash, randomUUID } from 'node:crypto'
import { homedir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const VERSION = 1
const SEED_FILES = ['learning-seeds.json', 'learning-seeds-a.json', 'learning-seeds-b.json']
const LIMITATIONS = [
  '结构与样式规则是抽象推断，不代表理解文章语义，也不代表已经视觉验证。',
  '仅统计互不重复的外部文章来源；自己的作品和使用成功反馈不构成新的外部证据。',
  '不同风格的条件与样例分别保留，数值不跨文章强制求平均。',
  '长篇纯图片中的文字层级、图片内部排版和语义顺序无法从 DOM 样式确认；需要查看原图并人工核验。',
]
const CHECKLIST = [
  '按本次目标选择适用规则，核对 whenToUse 和 pitfalls，不整套照搬单一参考。',
  '先建立章节和信息顺序，再安排标题、正文、图片与图注；确认每张图服务于相邻内容。',
  '在手机宽度预览检查标题换行、段落密度、边距、卡片溢出、图注及颜色对比。',
  '对长图和纯图片逐张查看清晰度及可读性；DOM 数值不足时明确标记待视觉核验。',
  '保存后重读正文结构并检查预览；将实际使用结果反馈给对应规则。',
]
const STYLE_NUMBERS = {
  fontSize: [5, 200], fontWeight: [100, 1000], lineHeight: [0.5, 400],
  letterSpacing: [-20, 50], paragraphGap: [0, 500], firstLineIndent: [0, 200],
  marginTop: [0, 1000], marginBottom: [0, 1000], padding: [0, 500],
  paddingTop: [0, 500], paddingBottom: [0, 500], paddingLeft: [0, 500],
  paddingRight: [0, 500], borderWidth: [0, 100], borderLeftWidth: [0, 100],
  borderRadius: [0, 500], width: [0, 10000], height: [0, 100000],
  gap: [0, 500], level: [1, 6], columns: [1, 6],
}
const STYLE_ENUMS = {
  align: ['left', 'right', 'center', 'justify'], textAlign: ['left', 'right', 'center', 'justify'],
  borderStyle: ['none', 'solid', 'dashed', 'dotted', 'double'],
  style: ['none', 'solid', 'dashed', 'dotted', 'double', 'thin', 'grid', 'pattern'],
  tag: ['h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'p', 'section', 'div'],
  numberingStyle: ['none', 'decimal', 'roman', 'circle', 'chinese', 'numbered'],
  quoteStyle: ['none', 'box', 'left', 'indent'],
}
const KINDS = {
  title: 'heading', heading: 'heading', h1: 'heading', h2: 'heading', h3: 'heading',
  paragraph: 'paragraph', text: 'paragraph', p: 'paragraph', body: 'paragraph',
  image: 'image', img: 'image', caption: 'caption', divider: 'divider',
  card: 'card', imageGroup: 'image-group', 'image-group': 'image-group', spacer: 'spacer',
}
const DEFINITIONS = {
  'title-hierarchy': {
    category: 'hierarchy', label: '建立标题层级',
    whenToUse: '有章节与至少两级信息层次的图文；先确定层级，再选字号。',
    howToApply: [
      '把本稿分成主标题、章节标题、正文三层；同一级统一字号、字重和位置。',
      '参考来源的标题/正文字号比选择克制或醒目的层级，先做一个章节，再用手机宽度预览换行。',
    ],
    pitfalls: ['字号比仅是结构推断；不要为模仿样例把所有短句都设为标题。', '长标题、纯图片标题需单独检查换行与辨识度。'],
  },
  'short-paragraphs': {
    category: 'typography', label: '按阅读单元拆段',
    whenToUse: '正文较密、需要手机连续阅读的解释或叙述内容。',
    howToApply: ['按一个论点或一个动作拆成一段，保留因果和引用完整性。', '用已观察到的段落长度分布作密度参照；长段先重写再排版，检查断段后逻辑是否连贯。'],
    pitfalls: ['短段落只是可读性候选条件，不能从字符数推断文意。', '公式、诗歌、列表和引用不宜机械按字数截断。'],
  },
  'image-text-adjacency': {
    category: 'imagery', label: '组织相邻图文',
    whenToUse: '图片承担例证、步骤或结果说明，且正文和图片可分别编辑。',
    howToApply: ['先说明读者应从图片看什么，再把图放在对应段落附近，并补充必要图注。', '保留一个图文组内较小间距，组间使用较大间距；预览确认图与解释不会被装饰拆开。'],
    pitfalls: ['DOM 邻接只能证明位置，不能证明图文语义相关。', '不得复制参考图；使用已获授权的本稿素材。'],
  },
  'image-groups': {
    category: 'imagery', label: '把同类图片编成组',
    whenToUse: '有多张属于同一主题或同一步骤的图片，且缩小后仍然可读。',
    howToApply: ['按本稿语义划分图片组，同组统一裁切比例、间距和图注样式。', '先验证单列，再考虑双列；在手机预览中逐张检查主体和细字，必要时恢复单列。'],
    pitfalls: ['连续图片不能自动视作语义组图，需核对内容。', '信息图、带小字的截图和纵向长图不宜仅为整齐强行拼列。'],
  },
  'whitespace-rhythm': {
    category: 'spacing', label: '用留白表达分组',
    whenToUse: '章节、段落或图文组之间需要形成清晰阅读节奏。',
    howToApply: ['先定义组内、段间和章间三级间距，同类间距保持一致。', '参考来源的间距档位选一个起点；在手机宽度检查相邻元素的归属，再调整过密或过空的部分。'],
    pitfalls: ['宽松和紧凑风格分别选择，不把不同来源的间距求平均。', '大边距可能挤压正文，须结合容器宽度查看。'],
  },
  'card-grouping': {
    category: 'cards', label: '用卡片包裹完整信息单元',
    whenToUse: '提示、步骤、引用或数据块需要从连续正文中分组。',
    howToApply: ['以一个完整主题为单位建卡片，先加入内边距，再选择浅底或边框作为分组信号。', '同类卡片使用相同样式；检查文字对比、图片宽度和手机端溢出，避免层层嵌套。'],
    pitfalls: ['发现背景色或边框只说明视觉容器存在，不能证明其中内容应归为一组。', '并非每段都需要卡片，连续叙事过度卡片化会打断阅读。'],
  },
  'section-dividers': {
    category: 'rhythm', label: '在章节转折处使用分隔',
    whenToUse: '章节切换需要除标题和留白以外的节奏提示。',
    howToApply: ['先用标题与章间留白建立结构，仍难辨认时在真正的章节边界加分隔。', '同稿限制为一种主分隔样式，调整上下间距，让分隔从属于章节而非抢占正文。'],
    pitfalls: ['连续装饰不等于有效结构；不要在每个短段后插分隔。', '分隔线的样式和密度需视觉验证。'],
  },
  'restrained-palette': {
    category: 'color', label: '分配颜色角色',
    whenToUse: '需要统一正文、标题、强调和卡片背景的颜色。',
    howToApply: ['先固定正文色与背景色，再选一个主要强调色；仅为确实不同的信息角色增加辅助色。', '把来源颜色作为候选色，不复制品牌识别；检查小字、浅底卡片与暗色区域的对比。'],
    pitfalls: ['出现次数不能证明色彩对比合格或配色有普适效果。', '不能仅靠颜色表达关键分类。'],
  },
}
const CATEGORY_ALIASES = {
  hierarchy: 'hierarchy', title: 'hierarchy', heading: 'hierarchy', '标题': 'hierarchy', '层级': 'hierarchy', '文字层级': 'typography',
  typography: 'typography', body: 'typography', paragraph: 'typography', '正文': 'typography', '段落': 'typography',
  imagery: 'imagery', image: 'imagery', images: 'imagery', '图片': 'imagery', '组图': 'imagery', '图文': 'imagery', '图文布局': 'imagery', 'image-layout': 'imagery',
  spacing: 'spacing', whitespace: 'spacing', '留白': 'spacing', '间距': 'spacing',
  cards: 'cards', card: 'cards', '卡片': 'cards', '边框': 'cards',
  rhythm: 'rhythm', divider: 'rhythm', '分隔': 'rhythm', '节奏': 'rhythm',
  color: 'color', palette: 'color', '颜色': 'color', '配色': 'color',
  structure: 'structure', layout: 'structure', '结构': 'structure', '章节': 'structure', '叙事': 'structure',
  'asset-boundary': 'asset-boundary', '文字层': 'asset-boundary', '可编辑': 'asset-boundary',
  components: 'components', '组件': 'components',
  interaction: 'interaction', '交互布局': 'interaction', '交互': 'interaction', '滑动': 'interaction',
}

const clone = (value) => JSON.parse(JSON.stringify(value))
const object = (value) => value && typeof value === 'object' && !Array.isArray(value) ? value : {}
const unique = (values) => [...new Set(values)]
const hash = (value) => createHash('sha256').update(value).digest('hex').slice(0, 20)
const safeId = (value) => typeof value === 'string' && /^[\p{L}\p{N}_.:/-]{1,160}$/u.test(value) ? value : ''
function number(value, minimum = 0, maximum = 1000000) {
  if (typeof value === 'string' && !/^[-+]?\d+(?:\.\d+)?$/.test(value)) return undefined
  if (typeof value !== 'number' && typeof value !== 'string') return undefined
  const parsed = Number(value)
  return Number.isFinite(parsed) && parsed >= minimum && parsed <= maximum ? Math.round(parsed * 100) / 100 : undefined
}
function color(value) {
  if (typeof value !== 'string') return undefined
  let v = value.trim().toLowerCase()
  if (v === 'transparent') return v
  if (/^#[\da-f]{3}$/.test(v)) v = '#' + [...v.slice(1)].map((x) => x + x).join('')
  if (/^#[\da-f]{6}(?:[\da-f]{2})?$/.test(v)) return v
  const match = v.match(/^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})(?:\s*,\s*(0|1|0?\.\d+))?\s*\)$/)
  if (!match || match.slice(1, 4).some((x) => Number(x) > 255)) return undefined
  return match[4] === undefined
    ? '#' + match.slice(1, 4).map((x) => Number(x).toString(16).padStart(2, '0')).join('')
    : `rgba(${Number(match[1])},${Number(match[2])},${Number(match[3])},${Number(match[4])})`
}
function style(raw) {
  const input = object(raw)
  const out = {}
  for (const [key, range] of Object.entries(STYLE_NUMBERS)) {
    const parsed = number(input[key], ...range)
    if (parsed !== undefined) out[key] = parsed
  }
  for (const key of ['color', 'background', 'backgroundColor', 'borderColor', 'borderLeftColor']) {
    const parsed = color(input[key])
    if (parsed) out[key] = parsed
  }
  for (const [key, allowed] of Object.entries(STYLE_ENUMS)) {
    if (allowed.includes(input[key])) out[key] = input[key]
  }
  if (typeof input.hasDecoration === 'boolean') out.hasDecoration = input.hasDecoration
  return out
}

/** Drop all free text, samples, HTML, authors, image locations and unknown fields. */
function featuresFrom(profile, observation) {
  const p = object(profile)
  const o = object(observation)
  const raw = object(p.style)
  const out = { style: {} }
  if (typeof o.incomplete === 'boolean' || typeof o.truncated === 'boolean') out.sampling = {
    incomplete: o.incomplete === true, truncated: o.truncated === true, visuallyVerified: false,
  }
  if (Array.isArray(raw.titles)) out.style.titles = raw.titles.slice(0, 8).map(style).filter((x) => Object.keys(x).length)
  const body = style(raw.body)
  if (Object.keys(body).length) out.style.body = body
  const palette = {}
  for (const key of ['primary', 'secondary', 'text', 'background', 'surface']) {
    const parsed = color(object(raw.palette)[key])
    if (parsed) palette[key] = parsed
  }
  const accents = unique((Array.isArray(raw.palette?.accents) ? raw.palette.accents : []).map(color).filter(Boolean)).slice(0, 8)
  if (accents.length) palette.accents = accents
  if (Object.keys(palette).length) out.style.palette = palette
  for (const key of ['images', 'decor', 'decorations', 'card']) {
    const item = style(raw[key])
    for (const numeric of ['count', 'imageCount', 'svgCount', 'badgeCount', 'dividers', 'borderedElements', 'cards', 'imageTextBlocks']) {
      const parsed = number(object(raw[key])[numeric])
      if (parsed !== undefined) item[numeric] = parsed
    }
    if (Object.keys(item).length) out.style[key] = item
  }
  if (Array.isArray(raw.dividers)) out.style.dividers = raw.dividers.slice(0, 16).map(style).filter((x) => Object.keys(x).length)
  for (const key of ['charCount', 'imageCount', 'paragraphCount', 'headingCount']) {
    const parsed = number(p[key] ?? o[key])
    if (parsed !== undefined) out[key] = parsed
  }
  if (Array.isArray(o.sequence)) {
    out.sequence = o.sequence.slice(0, 1000).map((entry) => {
      const input = object(entry)
      const kind = KINDS[input.kind]
      if (!kind) return null
      const next = { kind }
      for (const key of ['textLength', 'imageCount', 'level']) {
        const parsed = number(input[key])
        if (parsed !== undefined) next[key] = parsed
      }
      const numericStyle = style(input.style)
      if (Object.keys(numericStyle).length) next.style = numericStyle
      return next
    }).filter(Boolean)
  }
  if (!Object.keys(out.style).length) delete out.style
  return out
}

function sourceIdentity(value, explicitArticleId) {
  if (typeof value !== 'string') throw new Error('学习来源需要公开秀米或微信文章 URL。')
  let parsed
  try { parsed = new URL(value) } catch { throw new Error('学习来源 URL 无效。') }
  const host = parsed.hostname.toLowerCase()
  const xiumi = host === 'xiumi.us' || host.endsWith('.xiumi.us') || host === 'xiumius.cn' || host.endsWith('.xiumius.cn')
  if (!['http:', 'https:'].includes(parsed.protocol) || (!xiumi && host !== 'mp.weixin.qq.com') || parsed.port) {
    throw new Error('学习来源仅允许无自定义端口的公开 http(s) 秀米或微信文章 URL。')
  }
  const idMatch = (parsed.pathname + parsed.hash).match(/\/paper\/(?:for\/)?(\d+)(?:\/|$|[?#])/) ||
    (xiumi ? parsed.pathname.match(/\/board\/v5\/(?:[^/]+\/)+(\d+)(?:\/|$)/) : null)
  let articleId = idMatch?.[1] || safeId(String(explicitArticleId ?? ''))
  if (!articleId && xiumi) {
    for (const key of ['paperId', 'paper_id', 'articleId', 'itemId', 'id']) {
      const v = parsed.searchParams.get(key)
      if (v && /^\d{1,40}$/.test(v)) { articleId = v; break }
    }
  }
  if (!articleId && host === 'mp.weixin.qq.com') {
    const sn = parsed.searchParams.get('sn')
    if (sn && /^[a-zA-Z\d_-]{8,80}$/.test(sn)) articleId = `wx-${sn}`
    else {
      const mid = parsed.searchParams.get('mid')
      const idx = parsed.searchParams.get('idx') || '1'
      // mid alone can repeat across accounts: hash the account id without storing it.
      if (mid && /^\d{1,40}$/.test(mid) && /^\d{1,6}$/.test(idx)) {
        articleId = `wx-${hash(`${parsed.searchParams.get('__biz') || ''}:${mid}:${idx}`)}`
      }
    }
  }
  parsed.username = ''
  parsed.password = ''
  parsed.search = ''
  parsed.hash = ''
  parsed.pathname = parsed.pathname.replace(/\/+$/, '') || '/'
  // Xiumi editor/preview links for the same numeric article share one source.
  if (xiumi && /^\d+$/.test(articleId) && !parsed.pathname.includes('/board/v5/')) {
    parsed.hostname = 'xiumi.us'
    parsed.pathname = `/studio/v5`
    parsed.hash = `/paper/for/${articleId}`
  }
  const url = parsed.toString()
  const key = xiumi && /^\d+$/.test(articleId) ? `xiumi:${articleId}` : `${url}|${articleId}`
  return { id: `source-${hash(key)}`, url, articleId }
}

function emptyState() {
  return { version: VERSION, revision: 0, updatedAt: null, sources: [], rules: [], ownArticles: [], feedback: {} }
}
function readJson(path, label) {
  try { return JSON.parse(readFileSync(path, 'utf8').replace(/^\uFEFF/, '')) }
  catch (error) { throw new Error(`${label}无法读取或 JSON 已损坏，原文件未覆盖：${path}（${error.code || error.name}）`) }
}
function readState(path) {
  if (!existsSync(path)) return emptyState()
  const value = readJson(path, '学习库')
  if (!value || value.version !== VERSION || !Number.isSafeInteger(value.revision) || value.revision < 0 ||
    !Array.isArray(value.sources) || !Array.isArray(value.rules) || !Array.isArray(value.ownArticles) ||
    !value.feedback || typeof value.feedback !== 'object' || Array.isArray(value.feedback) ||
    value.sources.some((s) => !s || !safeId(s.id) || typeof s.url !== 'string' || !s.features || typeof s.features !== 'object') ||
    value.rules.some((r) => !r || !safeId(r.id) || typeof r.technique !== 'string' || typeof r.category !== 'string' ||
      typeof r.whenToUse !== 'string' || !Array.isArray(r.howToApply) || r.howToApply.some((a) => typeof a !== 'string') ||
      !Array.isArray(r.pitfalls) || r.pitfalls.some((a) => typeof a !== 'string') ||
      !Array.isArray(r.sourceIds) || r.sourceIds.some((id) => !safeId(id))) ||
    value.ownArticles.some((a) => !a || (typeof a.articleId !== 'string') || typeof a.url !== 'string') ||
    Object.values(value.feedback).some((f) => !f || !Number.isSafeInteger(f.success) || !Number.isSafeInteger(f.failure) || f.success < 0 || f.failure < 0)) {
    throw new Error(`学习库格式无效或版本不支持，原文件未覆盖：${path}`)
  }
  return value
}

// Only curated seed guidance may supply text. Observations always use the templates above.
function guidanceText(value) {
  if (typeof value !== 'string' || /<\/?[a-z]|https?:\/\/|data:|javascript:/i.test(value)) return ''
  return value.replace(/[\u0000-\u001f]/g, ' ').trim().slice(0, 1200)
}
function textList(value) {
  return unique((Array.isArray(value) ? value : typeof value === 'string' ? [value] : []).map(guidanceText).filter(Boolean)).slice(0, 20)
}
function confidenceNumber(value, fallback = 0.4) {
  if (value === 'high') return 0.75
  if (value === 'medium') return 0.55
  if (value === 'low') return 0.3
  return number(value, 0, 1) ?? fallback
}
function measurementTree(value, depth = 0) {
  if (depth > 5) return undefined
  if (typeof value === 'number') return number(value, -100000, 1000000)
  if (typeof value === 'boolean') return value
  if (typeof value === 'string') return color(value)
  if (Array.isArray(value)) return value.slice(0, 40).map((item) => measurementTree(item, depth + 1)).filter((item) => item !== undefined)
  const result = {}
  for (const [key, item] of Object.entries(object(value)).slice(0, 80)) {
    if (!/^[a-z][a-z\d]{0,59}$/i.test(key) || /(?:author|url|html|sample|note|content|token|password)/i.test(key) || /^(?:text|title|body|caption)$/i.test(key)) continue
    const cleaned = measurementTree(item, depth + 1)
    if (cleaned !== undefined && (typeof cleaned !== 'object' || Object.keys(cleaned).length)) result[key] = cleaned
  }
  return Object.keys(result).length ? result : undefined
}
function seedEvidence(value) {
  const evidence = []
  for (const entry of Array.isArray(value) ? value.slice(0, 60) : []) {
    const r = object(entry)
    const next = {}
    if (safeId(r.id)) next.id = r.id
    if (typeof r.type === 'string' && /^[a-z][a-z-]{0,59}$/.test(r.type)) next.type = r.type
    const measurements = measurementTree(r.measurements)
    if (measurements) next.measurements = measurements
    if (Object.keys(next).length) evidence.push(next)
  }
  return evidence
}
function isOwnIn(state, identity) {
  return state.ownArticles.some((item) => identity.articleId && item.articleId
    ? item.articleId === identity.articleId
    : Boolean(identity.url && item.url === identity.url))
}
function mergeRule(rules, candidate) {
  let target = rules.find((r) => r.id === candidate.id ||
    (r.category === candidate.category && r.technique === candidate.technique))
  if (!target) { rules.push(clone(candidate)); return candidate.id }
  target.sourceIds = unique([...target.sourceIds, ...candidate.sourceIds])
  target.inferred = target.inferred && candidate.inferred
  target.baseConfidence = Math.max(target.baseConfidence || 0, candidate.baseConfidence || 0)
  if (candidate.curatedEvidence?.length) {
    const all = [...(target.curatedEvidence || []), ...candidate.curatedEvidence]
    target.curatedEvidence = all.filter((v, i) => all.findIndex((x) => JSON.stringify(x) === JSON.stringify(v)) === i).slice(0, 120)
  }
  target.aliases = unique([...(target.aliases || []), candidate.id, ...(candidate.aliases || [])]).filter((id) => id !== target.id)
  const variant = { whenToUse: candidate.whenToUse, howToApply: candidate.howToApply, pitfalls: candidate.pitfalls }
  const current = { whenToUse: target.whenToUse, howToApply: target.howToApply, pitfalls: target.pitfalls }
  if (JSON.stringify(variant) !== JSON.stringify(current)) {
    const all = [...(target.variants || []), variant]
    target.variants = all.filter((v, i) => all.findIndex((x) => JSON.stringify(x) === JSON.stringify(v)) === i).slice(0, 80)
  }
  return target.id
}

function inferredRules(features, requested, sourceId) {
  const result = []
  const s = features.style || {}
  const sequence = features.sequence || []
  const paragraphs = sequence.filter((x) => x.kind === 'paragraph' && x.textLength > 0)
  const add = (technique, variant = 'observed') => {
    const def = DEFINITIONS[technique]
    if (!def) return
    result.push({
      id: `inferred:${technique}:${variant}`, category: def.category,
      technique: `${technique}:${variant}`, label: def.label,
      whenToUse: def.whenToUse, howToApply: def.howToApply, pitfalls: def.pitfalls,
      sourceIds: [sourceId], inferred: true, baseConfidence: 0.35, variants: [],
    })
  }
  const bodySize = s.body?.fontSize || paragraphs.find((p) => p.style?.fontSize)?.style.fontSize
  const titleSize = (s.titles || []).find((t) => t.fontSize)?.fontSize || sequence.find((x) => x.kind === 'heading' && x.style?.fontSize)?.style.fontSize
  if (bodySize && titleSize && titleSize > bodySize) {
    const ratio = titleSize / bodySize
    add('title-hierarchy', ratio < 1.4 ? 'gentle' : ratio > 1.8 ? 'expressive' : 'moderate')
  }
  if (paragraphs.length >= 2) {
    const sorted = paragraphs.map((p) => p.textLength).sort((a, b) => a - b)
    const median = sorted[Math.floor(sorted.length / 2)]
    if (median <= 200) add('short-paragraphs', median <= 80 ? 'compact' : 'narrative')
  }
  if (sequence.some((x, i) => x.kind === 'image' && [sequence[i - 1]?.kind, sequence[i + 1]?.kind].some((k) => ['paragraph', 'caption'].includes(k)))) add('image-text-adjacency')
  if (sequence.some((x, i) => x.kind === 'image-group' || (x.kind === 'image' && sequence[i + 1]?.kind === 'image'))) add('image-groups')
  const gap = s.body?.paragraphGap ?? s.body?.marginBottom
  if (gap !== undefined && gap > 0) add('whitespace-rhythm', gap <= 12 ? 'tight' : gap > 24 ? 'generous' : 'regular')
  const card = { ...s.decorations, ...s.decor, ...s.card }
  const cardVisible = card.cards > 0 || card.borderedElements > 0 || card.borderWidth > 0 ||
    (card.background && card.background !== 'transparent') || (card.backgroundColor && card.backgroundColor !== 'transparent')
  if (cardVisible || sequence.some((x) => x.kind === 'card')) add('card-grouping')
  if (s.dividers?.length || card.dividers > 0 || sequence.some((x) => x.kind === 'divider')) add('section-dividers')
  if (s.palette?.primary && (s.palette.text || s.palette.background)) add('restrained-palette')
  // A caller may nominate a known technique only when its structural trigger was observed.
  // Unknown prose/objects are intentionally ignored rather than becoming persistent rules.
  const nominated = (Array.isArray(requested) ? requested : []).filter((x) => typeof x === 'string')
  return result.sort((a, b) => Number(nominated.some((x) => b.technique.startsWith(x))) - Number(nominated.some((x) => a.technique.startsWith(x))))
}

export class LearningLibrary {
  constructor({ directory = join(homedir(), '.xiumi-assistant', 'learning'), seedDirectory = join(dirname(fileURLToPath(import.meta.url)), 'data'), clock = () => new Date() } = {}) {
    this.directory = resolve(directory)
    this.seedDirectory = seedDirectory === null ? null : resolve(seedDirectory)
    this.path = join(this.directory, 'library.json')
    this.clock = clock
  }

  _now() {
    const value = this.clock()
    const date = value instanceof Date ? value : new Date(value)
    if (!Number.isFinite(date.getTime())) throw new Error('学习库 clock 返回了无效时间。')
    return date.toISOString()
  }

  _seeds(state) {
    const sources = []
    const rules = []
    if (!this.seedDirectory) return { sources, rules }
    for (const filename of SEED_FILES) {
      const path = join(this.seedDirectory, filename)
      if (!existsSync(path)) continue
      const value = readJson(path, '学习种子')
      if (!Array.isArray(value) && (!value || typeof value !== 'object' || !Array.isArray(value.rules))) {
        throw new Error(`学习种子格式无效：${path}`)
      }
      const rawRules = Array.isArray(value) ? value : value.rules
      const rawSources = Array.isArray(value.sources) ? value.sources : []
      const sourceMap = new Map()
      const addSource = (entry) => {
        const input = typeof entry === 'string' ? { url: entry } : object(entry)
        let identity
        try { identity = sourceIdentity(input.url || input.sourceUrl, input.articleId) } catch { return null }
        if (isOwnIn(state, identity)) return null
        if (input.id) sourceMap.set(input.id, identity.id)
        sourceMap.set(identity.url, identity.id)
        if (!sources.some((s) => s.id === identity.id)) sources.push({ ...identity, origin: 'seed', features: {}, inferred: true })
        return identity.id
      }
      rawSources.forEach(addSource)
      for (const rawRule of rawRules) {
        const r = object(rawRule)
        const technique = guidanceText(r.technique)
        const id = safeId(r.id) || (technique ? `seed-${hash(technique)}` : '')
        const howToApply = textList(r.howToApply)
        const whenToUse = textList(r.whenToUse).join('；')
        if (!id || !technique || !howToApply.length || !whenToUse) continue
        const sourceIds = []
        for (const url of Array.isArray(r.sourceUrls) ? r.sourceUrls : []) {
          const sid = addSource(url)
          if (sid) sourceIds.push(sid)
        }
        const refs = Array.isArray(r.sourceIds) ? r.sourceIds : Array.isArray(r.evidence?.sourceIds) ? r.evidence.sourceIds : []
        for (const ref of refs) if (sourceMap.has(ref)) sourceIds.push(sourceMap.get(ref))
        mergeRule(rules, {
          id, category: CATEGORY_ALIASES[r.category] || guidanceText(r.category) || 'general', technique,
          whenToUse, howToApply, pitfalls: textList(r.pitfalls), sourceIds: unique(sourceIds),
          inferred: true, baseConfidence: confidenceNumber(r.confidence), variants: [],
          curatedEvidence: seedEvidence(r.evidence),
        })
      }
    }
    return { sources, rules }
  }

  _view(state = readState(this.path)) {
    const seeded = this._seeds(state)
    const sources = clone(seeded.sources)
    for (const source of state.sources) {
      if (isOwnIn(state, source)) continue
      const index = sources.findIndex((s) => s.id === source.id)
      if (index === -1) sources.push(clone(source))
      else sources[index] = clone(source)
    }
    const valid = new Set(sources.map((s) => s.id))
    const rules = []
    for (const raw of [...seeded.rules, ...state.rules]) {
      const next = clone(raw)
      next.sourceIds = next.sourceIds.filter((id) => valid.has(id))
      // Keep a curated unverified rule, but remove inferred rules whose only evidence was own work.
      if (!next.sourceIds.length && next.id.startsWith('inferred:')) continue
      mergeRule(rules, next)
    }
    const finalized = rules.map((r) => {
      const keys = [r.id, ...(r.aliases || [])]
      const feedback = keys.reduce((acc, key) => {
        const f = state.feedback[key] || { success: 0, failure: 0 }
        acc.success += f.success; acc.failure += f.failure
        return acc
      }, { success: 0, failure: 0 })
      const sourceCount = r.sourceIds.length
      const partialSources = sources.filter(s => r.sourceIds.includes(s.id) && (s.features?.sampling?.incomplete || s.features?.sampling?.truncated)).map(s => s.id)
      const evidenceConfidence = sourceCount ? Math.min(0.8, Math.max(r.baseConfidence || 0.35, 0.35 + Math.log2(sourceCount + 1) * 0.12)) : 0.2
      const adjustment = Math.max(-0.2, Math.min(0.1, (feedback.success - feedback.failure * 2) * 0.02))
      return {
        id: r.id, category: r.category, technique: r.technique, ...(r.label ? { label: r.label } : {}),
        whenToUse: r.whenToUse, howToApply: r.howToApply, pitfalls: r.pitfalls,
        variants: r.variants || [], aliases: r.aliases || [],
        sourceUrls: unique(sources.filter((s) => r.sourceIds.includes(s.id)).map((s) => s.url)),
        evidence: {
          sourceCount, independentSourceCount: sourceCount, sourceIds: r.sourceIds,
          basis: r.inferred ? 'structural-inference' : 'curated-abstraction',
          visuallyVerified: false, feedback,
          partialSourceIds: partialSources,
          curatedEvidence: r.curatedEvidence || [],
          observations: sources.filter((s) => r.sourceIds.includes(s.id)).map((s) => ({ sourceId: s.id, features: s.features })),
        },
        inferred: r.inferred,
        confidence: Math.round(Math.max(0.1, Math.min(0.85, evidenceConfidence + adjustment - (partialSources.length ? 0.08 : 0))) * 100) / 100,
      }
    })
    return { revision: state.revision, sourceCount: sources.length, sources, rules: finalized, ownCount: state.ownArticles.length }
  }

  _write(mutator) {
    mkdirSync(this.directory, { recursive: true, mode: 0o700 })
    const lockPath = join(this.directory, 'library.lock')
    let lock
    try { lock = openSync(lockPath, 'wx', 0o600) }
    catch (error) {
      if (error.code === 'EEXIST') throw new Error(`学习库正在写入；请稍后重试。若上次进程异常退出，请检查锁文件：${lockPath}`)
      throw error
    }
    let temporary
    try {
      const state = readState(this.path)
      const result = mutator(state)
      if (result.changed === false) return { ...result.value, revision: state.revision }
      state.revision += 1
      state.updatedAt = this._now()
      temporary = join(this.directory, `.library-${process.pid}-${randomUUID()}.tmp`)
      const fd = openSync(temporary, 'wx', 0o600)
      try { writeFileSync(fd, JSON.stringify(state, null, 2), 'utf8'); fsyncSync(fd) }
      finally { closeSync(fd) }
      // No in-place overwrite fallback: failed rename must leave the previous library intact.
      renameSync(temporary, this.path)
      temporary = null
      return { ...result.value, revision: state.revision }
    } finally {
      if (temporary) { try { unlinkSync(temporary) } catch {} }
      closeSync(lock)
      unlinkSync(lockPath)
    }
  }

  observe({ url, observation = {}, profile = {}, techniques = [], origin = 'external' } = {}) {
    const identity = sourceIdentity(url || profile?.sourceUrl || observation?.url, observation?.articleId || profile?.articleId)
    const state = readState(this.path)
    if (origin !== 'external' || isOwnIn(state, identity)) {
      return { learned: false, reason: origin !== 'external' ? 'non-external-origin' : 'own-article', sourceId: identity.id, ruleIds: [], revision: state.revision }
    }
    const features = featuresFrom(profile, observation)
    const candidates = inferredRules(features, techniques, identity.id)
    if (!candidates.length) return { learned: false, reason: 'insufficient-structural-evidence', sourceId: identity.id, ruleIds: [], revision: state.revision, limitations: LIMITATIONS }
    return this._write((current) => {
      if (isOwnIn(current, identity)) return { changed: false, value: { learned: false, reason: 'own-article', sourceId: identity.id, ruleIds: [] } }
      const view = this._view(current)
      const existing = current.sources.find((s) => s.id === identity.id)
      const sourceExists = view.sources.some((s) => s.id === identity.id)
      const oldRules = new Set(view.rules.filter((r) => r.evidence.sourceIds.includes(identity.id)).flatMap((r) => [r.id, ...r.aliases]))
      const changed = !existing || JSON.stringify(existing.features) !== JSON.stringify(features) || candidates.some((r) => !oldRules.has(r.id))
      if (!changed) return { changed: false, value: { learned: false, reason: 'duplicate-source', sourceId: identity.id, ruleIds: candidates.map((r) => r.id), sourceCount: view.sourceCount } }
      const now = this._now()
      const source = { ...identity, origin: 'external', features, firstSeenAt: existing?.firstSeenAt || now, lastSeenAt: now }
      if (existing) current.sources[current.sources.indexOf(existing)] = source
      else current.sources.push(source)
      // A refreshed page can change its layout. Retract this source from stale inferred
      // variants instead of keeping support that no longer matches its stored features.
      for (const rule of current.rules) {
        if (!candidates.some((r) => r.id === rule.id || r.technique === rule.technique)) {
          rule.sourceIds = rule.sourceIds.filter((id) => id !== identity.id)
        }
      }
      current.rules = current.rules.filter((r) => r.sourceIds.length)
      const ruleIds = candidates.map((r) => mergeRule(current.rules, r))
      return { value: { learned: true, newSource: !sourceExists, sourceId: identity.id, ruleIds: unique(ruleIds), sourceCount: view.sourceCount + Number(!sourceExists), inferred: true, visuallyVerified: false } }
    })
  }

  guide({ intent = '', categories = [], limit = 12 } = {}) {
    const view = this._view()
    const selectedCategories = unique((Array.isArray(categories) ? categories : []).filter((c) => typeof c === 'string').map((c) => CATEGORY_ALIASES[c] || c))
    const query = String(intent || '').slice(0, 2000).toLowerCase()
    const intentCategories = unique(Object.entries(CATEGORY_ALIASES).filter(([key]) => query.includes(key)).map(([, value]) => value))
    const tokens = query.split(/[\s,，。;；:/]+/).filter((t) => t.length > 1)
    const score = (rule) => {
      const content = `${rule.id} ${rule.technique} ${rule.whenToUse} ${rule.howToApply.join(' ')}`.toLowerCase()
      return (intentCategories.includes(rule.category) ? 8 : 0) + tokens.reduce((sum, t) => sum + (content.includes(t) ? 2 : 0), 0) + rule.confidence + Math.min(0.5, rule.evidence.sourceCount * 0.02)
    }
    const count = Math.trunc(number(limit, 1, 50) ?? 12)
    const rules = view.rules.filter((r) => !selectedCategories.length || selectedCategories.includes(r.category))
      .map((rule) => ({ rule, score: score(rule) })).sort((a, b) => b.score - a.score || a.rule.id.localeCompare(b.rule.id))
      .slice(0, count).map(({ rule }) => rule)
    return {
      revision: view.revision, sourceCount: view.sourceCount,
      matchedSourceCount: unique(rules.flatMap((r) => r.evidence.sourceIds)).length,
      steps: rules.flatMap((r) => r.howToApply.map((action) => ({ ruleId: r.id, action, whenToUse: r.whenToUse, pitfalls: r.pitfalls }))),
      rules: rules.map(rule => ({ ...rule, variants: rule.variants.slice(0, 6), sourceUrls: rule.sourceUrls.slice(0, 12),
        evidence: { sourceCount: rule.evidence.sourceCount, independentSourceCount: rule.evidence.independentSourceCount,
          sourceIds: rule.evidence.sourceIds.slice(0, 20), basis: rule.evidence.basis, visuallyVerified: false,
          feedback: rule.evidence.feedback, partialSourceIds: rule.evidence.partialSourceIds.slice(0, 20),
          curatedEvidence: rule.evidence.curatedEvidence.slice(0, 8) } })),
      limitations: [...LIMITATIONS, ...(!rules.length ? ['当前没有匹配规则；请提供可提取结构的外部样例，或扩大 categories。'] : [])],
      verificationChecklist: [...CHECKLIST],
    }
  }

  status() {
    const state = readState(this.path)
    const view = this._view(state)
    return {
      version: VERSION, revision: state.revision, updatedAt: state.updatedAt,
      directory: this.directory, path: this.path, exists: existsSync(this.path),
      sourceCount: view.sourceCount, learnedSourceCount: view.sources.filter((s) => s.origin === 'external').length,
      seedSourceCount: view.sources.filter((s) => s.origin === 'seed').length,
      ruleCount: view.rules.length, ownCount: state.ownArticles.length,
      feedbackCount: Object.values(state.feedback).reduce((sum, f) => sum + f.success + f.failure, 0),
      limitations: [...LIMITATIONS],
    }
  }

  list() { return this._view() }

  rememberOwn({ articleId, url } = {}) {
    const identity = url ? sourceIdentity(url, articleId) : { articleId: safeId(String(articleId || '')), url: '' }
    if (!identity.articleId && !identity.url) throw new Error('标记自己的作品需要 articleId 或有效 URL。')
    return this._write((state) => {
      if (isOwnIn(state, identity)) return { changed: false, value: { remembered: true, alreadyKnown: true, removedSourceCount: 0 } }
      state.ownArticles.push({ articleId: identity.articleId, url: identity.url, rememberedAt: this._now() })
      const removed = state.sources.filter((s) => isOwnIn(state, s)).map((s) => s.id)
      state.sources = state.sources.filter((s) => !removed.includes(s.id))
      for (const rule of state.rules) rule.sourceIds = rule.sourceIds.filter((id) => !removed.includes(id))
      state.rules = state.rules.filter((r) => r.sourceIds.length)
      return { value: { remembered: true, alreadyKnown: false, removedSourceCount: removed.length } }
    })
  }

  isOwn({ articleId, url } = {}) {
    const identity = url ? sourceIdentity(url, articleId) : { articleId: safeId(String(articleId || '')), url: '' }
    return isOwnIn(readState(this.path), identity)
  }

  feedback({ ruleIds = [], outcome, note } = {}) {
    if (!['success', 'failure'].includes(outcome)) throw new Error('feedback.outcome 只能是 success 或 failure。')
    const requested = unique((Array.isArray(ruleIds) ? ruleIds : []).filter((id) => safeId(id)))
    // note is deliberately transient: callers may accidentally put article text or URLs in it.
    void note
    return this._write((state) => {
      const view = this._view(state)
      const ids = unique(requested.map((id) => view.rules.find((r) => r.id === id || r.aliases.includes(id))?.id).filter(Boolean))
      for (const id of ids) {
        const current = state.feedback[id] || { success: 0, failure: 0 }
        current[outcome] += 1
        state.feedback[id] = current
      }
      return { changed: ids.length > 0, value: { recorded: ids.length > 0, ruleIds: ids, unknownRuleIds: requested.filter((id) => !view.rules.some((r) => r.id === id || r.aliases.includes(id))), sourceCount: view.sourceCount, noteStored: false } }
    })
  }
}
