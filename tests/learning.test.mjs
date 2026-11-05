import test from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, mkdtempSync, readFileSync, writeFileSync, mkdirSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { LearningLibrary } from '../learning.js'

function fixture(t) {
  const base = mkdtempSync(join(tmpdir(), 'xiumi-learning-'))
  t.after(() => rmSync(base, { recursive: true, force: true }))
  const directory = join(base, 'learning')
  const seedDirectory = join(base, 'seeds')
  const options = { directory, seedDirectory, clock: () => new Date('2026-11-04T00:00:00Z') }
  return { base, directory, seedDirectory, options, library: new LearningLibrary(options) }
}
function observation() {
  return {
    articleId: '123', title: '绝密标题', author: '私密作者', html: '<p>保密正文</p>',
    sequence: [
      { kind: 'heading', text: '绝密标题', textLength: 8, style: { fontSize: 24 } },
      { kind: 'paragraph', text: '保密正文', textLength: 60, style: { fontSize: 15 } },
      { kind: 'image', src: 'https://example.com/private-image.png', textLength: 0 },
      { kind: 'paragraph', text: '另一段正文', textLength: 70 },
      { kind: 'divider' },
    ],
  }
}
function profile() {
  return {
    title: '绝密标题', name: '私密作者', samples: ['不可持久化样例'], notes: '不可持久化备注',
    imageCount: 1, charCount: 140,
    style: {
      titles: [{ fontSize: 24, fontWeight: '700', text: '绝密标题' }],
      body: { fontSize: 15, lineHeight: 1.8, paragraphGap: 18, html: '<b>保密正文</b>' },
      palette: { primary: '#f00', text: '#222222', background: '#ffffff', malicious: 'url(https://example.com/leak)' },
      images: { borderRadius: 4, src: 'https://example.com/private-image.png' },
      decor: { cards: 2, backgroundColor: '#fafafa', notes: '不可持久化备注' },
      arbitrary: { payload: '保密正文' },
    },
  }
}
const url = 'https://xiumi.us/studio/v5#/paper/for/123'
const observe = (library, extra = {}) => library.observe({ url, observation: observation(), profile: profile(), ...extra })

test('read-only construction/status/list/guide do not create a directory', (t) => {
  const { library, directory } = fixture(t)
  assert.equal(library.status().sourceCount, 0)
  assert.deepEqual(library.list().rules, [])
  assert.ok(library.guide().verificationChecklist.length)
  assert.equal(existsSync(directory), false)
})
test('partial sampling remains visible evidence and reduces confidence until a full refresh', (t) => {
  const {library} = fixture(t)
  observe(library, {observation:{...observation(),incomplete:true,truncated:true}})
  const partial=library.list().rules.find(r=>r.evidence.partialSourceIds.length)
  assert.ok(partial)
  assert.equal(partial.evidence.observations[0].features.sampling.visuallyVerified,false)
  observe(library, {observation:{...observation(),incomplete:false,truncated:false}})
  const full=library.list().rules.find(r=>r.id===partial.id)
  assert.equal(full.evidence.partialSourceIds.length,0)
  assert.ok(full.confidence>partial.confidence)
})
test('guide keeps evidence counts without repeating every stored source sequence', (t) => {
  const {library}=fixture(t)
  observe(library)
  assert.ok(library.list().rules[0].evidence.observations[0].features.sequence.length)
  const guide=library.guide()
  assert.ok(guide.rules.length)
  assert.equal(guide.rules[0].evidence.observations,undefined)
  assert.equal(guide.matchedSourceCount,1)
})

test('persist only structural whitelist; reject original prose, image URLs and techniques prose', (t) => {
  const { library } = fixture(t)
  const result = observe(library, { url: 'https://user:pass@xiumi.us/studio/v5?token=secret#/paper/for/123', techniques: [{ technique: '保密正文', howToApply: '不可持久化样例' }, 'raw正文'] })
  assert.equal(result.learned, true)
  const text = readFileSync(library.path, 'utf8')
  for (const forbidden of ['绝密标题', '私密作者', '保密正文', '不可持久化', 'private-image', 'example.com', 'token', 'secret', 'user:pass', '<p>', 'samples', 'notes']) assert.equal(text.includes(forbidden), false, forbidden)
  const saved = JSON.parse(text)
  assert.equal(saved.sources[0].features.style.palette.primary, '#ff0000')
  assert.equal(saved.sources[0].features.sequence[1].textLength, 60)
  assert.equal(saved.sources[0].url, url)
  assert.ok(library.guide().steps.every((s) => s.action.length > 10))
})

test('deduplicate normalized URLs and do not count feedback as independent evidence', (t) => {
  const { library } = fixture(t)
  const first = observe(library)
  const second = observe(library, { url: 'https://xiumi.us/app/preview_v2?itemId=123&token=drop' })
  assert.equal(second.learned, false)
  assert.equal(second.reason, 'duplicate-source')
  assert.equal(second.sourceId, first.sourceId)
  assert.equal(library.status().sourceCount, 1)
  library.feedback({ ruleIds: first.ruleIds, outcome: 'success', note: '保密正文 https://example.com/private-image.png' })
  assert.equal(library.status().sourceCount, 1)
  assert.equal(readFileSync(library.path, 'utf8').includes('保密正文'), false)
})

test('own articles never add evidence; late own marking retracts earlier external support', (t) => {
  const { library } = fixture(t)
  observe(library)
  const result = library.rememberOwn({ articleId: '123', url })
  assert.equal(result.removedSourceCount, 1)
  assert.equal(library.isOwn({ articleId: '123' }), true)
  assert.equal(observe(library).reason, 'own-article')
  assert.equal(library.status().sourceCount, 0)
  assert.equal(library.list().rules.length, 0)
  assert.equal(observe(library, { url: 'https://xiumi.us/studio/v5#/paper/for/456', observation: { ...observation(), articleId: '456' }, origin: 'own' }).reason, 'non-external-origin')
})

test('restart restores rules and multiple instances reread current disk', (t) => {
  const { library, options } = fixture(t)
  const another = new LearningLibrary(options)
  const first = observe(library)
  observe(another, { url: 'https://xiumi.us/studio/v5#/paper/for/456', observation: { ...observation(), articleId: '456' } })
  const restarted = new LearningLibrary(options)
  assert.equal(restarted.status().sourceCount, 2)
  restarted.feedback({ ruleIds: [first.ruleIds[0]], outcome: 'failure' })
  assert.equal(library.status().sourceCount, 2)
  assert.equal(library.list().rules.find((r) => r.id === first.ruleIds[0]).evidence.feedback.failure, 1)
})

test('corrupt or invalid storage is explicit and is not overwritten', (t) => {
  const { library, directory } = fixture(t)
  mkdirSync(directory)
  for (const invalid of ['{ broken', JSON.stringify({ version: 999 }), JSON.stringify({ version: 1, revision: 1, sources: [null], rules: [], ownArticles: [], feedback: {} })]) {
    writeFileSync(library.path, invalid)
    assert.throws(() => library.status(), /损坏|格式无效/)
    assert.throws(() => observe(library), /损坏|格式无效/)
    assert.throws(() => library.rememberOwn({ articleId: '123' }), /损坏|格式无效/)
    assert.equal(readFileSync(library.path, 'utf8'), invalid)
  }
})

test('different hierarchy variants stay separate, and refreshed sources retract stale support', (t) => {
  const { library } = fixture(t)
  observe(library)
  const expressive = profile()
  expressive.style.titles[0].fontSize = 36
  observe(library, { url: 'https://xiumi.us/studio/v5#/paper/for/456', observation: { ...observation(), articleId: '456' }, profile: expressive })
  const hierarchy = library.guide({ categories: ['hierarchy'] }).rules
  assert.equal(hierarchy.length, 2)
  assert.ok(hierarchy.every((r) => r.evidence.sourceCount === 1))
  observe(library, { profile: expressive })
  const refreshed = library.guide({ categories: ['hierarchy'] }).rules
  assert.equal(refreshed.length, 1)
  assert.equal(refreshed[0].technique, 'title-hierarchy:expressive')
  assert.equal(refreshed[0].evidence.sourceCount, 2)
})

test('a held file lock refuses a write without altering the current library', (t) => {
  const { library, directory } = fixture(t)
  observe(library)
  const previous = readFileSync(library.path, 'utf8')
  writeFileSync(join(directory, 'library.lock'), 'held-by-test')
  assert.throws(() => library.rememberOwn({ articleId: '123' }), /正在写入/)
  assert.equal(readFileSync(library.path, 'utf8'), previous)
})

test('guide ranks target categories, filters categories, and reflects failure without new sources', (t) => {
  const { library } = fixture(t)
  observe(library)
  const guide = library.guide({ intent: '组织图片组图和图文', limit: 1 })
  assert.equal(guide.rules[0].category, 'imagery')
  assert.ok(guide.steps.length)
  const selected = library.guide({ categories: ['标题'], limit: 4 })
  assert.ok(selected.rules.length)
  assert.ok(selected.rules.every((r) => r.category === 'hierarchy'))
  const before = selected.rules[0].confidence
  library.feedback({ ruleIds: [selected.rules[0].id], outcome: 'failure' })
  const after = library.guide({ categories: ['hierarchy'] }).rules[0]
  assert.ok(after.confidence < before)
  assert.equal(after.evidence.sourceCount, 1)
  assert.equal(after.evidence.visuallyVerified, false)
})

test('load array and object seeds with independent source evidence and no filesystem writes', (t) => {
  const { library, directory, seedDirectory } = fixture(t)
  mkdirSync(seedDirectory)
  const rule = { id: 'seed-card', category: '卡片', technique: 'group-information', whenToUse: '提示信息需要分组时', howToApply: ['把一个完整主题放入一个卡片，检查内边距。'], pitfalls: ['不要把每个短句都变成卡片。'], sourceUrls: ['https://a.xiumi.us/board/v5/a?token=x'], confidence: 'medium', evidence: [{ type: 'dom', measurements: { cardPaddingPx: 20, bodyText: '保密正文', imageUrl: 'https://example.com/private-image.png' } }] }
  writeFileSync(join(seedDirectory, 'learning-seeds-a.json'), JSON.stringify([rule]))
  writeFileSync(join(seedDirectory, 'learning-seeds-b.json'), JSON.stringify({ sources: [{ id: 'source-b', url: 'https://mp.weixin.qq.com/s/example-b?token=y' }], rules: [{ ...rule, id: 'seed-card-b', sourceUrls: ['https://mp.weixin.qq.com/s/example-b'], howToApply: ['对步骤卡片保持同类样式，并检查顺序。'] }] }))
  assert.equal(library.status().sourceCount, 2)
  const rules = library.list().rules
  assert.equal(rules.length, 1)
  assert.equal(rules[0].evidence.sourceCount, 2)
  assert.equal(rules[0].variants.length, 1)
  assert.equal(library.guide({ categories: ['cards'] }).rules.length, 1)
  assert.equal(rules[0].evidence.curatedEvidence[0].measurements.cardPaddingPx, 20)
  assert.equal(JSON.stringify(rules[0].evidence).includes('保密正文'), false)
  assert.equal(existsSync(directory), false)
  library.feedback({ ruleIds: ['seed-card-b'], outcome: 'success' })
  assert.equal(library.list().rules[0].evidence.feedback.success, 1)
  library.rememberOwn({ url: 'https://mp.weixin.qq.com/s/example-b' })
  assert.equal(library.status().sourceCount, 1)
})

test('query-only WeChat articles have distinct identities without saving queries', (t) => {
  const { library } = fixture(t)
  const o = { ...observation(), articleId: undefined }
  observe(library, { url: 'https://mp.weixin.qq.com/s?__biz=private-account&mid=1&idx=1&sn=abcd1234', observation: o })
  observe(library, { url: 'https://mp.weixin.qq.com/s?__biz=private-account&mid=2&idx=1&sn=abcd5678', observation: o })
  assert.equal(library.status().sourceCount, 2)
  const text = readFileSync(library.path, 'utf8')
  assert.equal(text.includes('private-account'), false)
  assert.equal(text.includes('__biz'), false)
  assert.equal(text.includes('mid='), false)
  library.rememberOwn({ url: 'https://mp.weixin.qq.com/s?sn=abcd1234' })
  assert.equal(library.status().sourceCount, 1)
})

test('Xiumi public board aliases share evidence and own-article identity by numeric id', (t) => {
  const { library } = fixture(t)
  const o = { ...observation(), articleId: undefined }
  const first = observe(library, { url: 'https://v.xiumius.cn/board/v5/example/688224355?track=drop', observation: o })
  const second = observe(library, { url: 'https://a.xiumius.cn/board/v5/example/688224355', observation: o })
  assert.equal(second.sourceId, first.sourceId)
  assert.equal(library.status().sourceCount, 1)
  library.rememberOwn({ url: 'https://r.xiumius.cn/board/v5/example/688224355' })
  assert.equal(library.isOwn({ url: 'https://d.xiumius.cn/board/v5/example/688224355' }), true)
  assert.equal(library.status().sourceCount, 0)
})

test('unsupported inputs do not become evidence, and unsafe origins are rejected', (t) => {
  const { library, directory } = fixture(t)
  assert.throws(() => observe(library, { url: 'https://xiumi.us.evil.example/article' }), /仅允许/)
  assert.throws(() => observe(library, { url: 'file:///private/example' }), /仅允许/)
  const result = library.observe({ url, profile: { imageCount: 12 }, observation: { sequence: [{ kind: 'image' }] } })
  assert.equal(result.reason, 'insufficient-structural-evidence')
  assert.ok(result.limitations.some((s) => s.includes('长篇纯图片')))
  assert.equal(existsSync(directory), false)
})
