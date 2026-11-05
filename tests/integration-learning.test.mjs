import test from 'node:test'
import assert from 'node:assert/strict'
import { Bridge } from '../index.js'

// Exercise the actual onLine method with in-memory collaborators. Never call
// request/ensure, launch the Python child, or instantiate a persistent library.
function fakeLibrary({ observeError, rememberError } = {}) {
  const calls = []
  const own = new Set()
  return {
    calls,
    observe(value) {
      calls.push({ method: 'observe', value: structuredClone(value) })
      if (observeError) throw new Error(observeError)
      const articleId = value.observation?.articleId || value.profile?.articleId
      return own.has(articleId)
        ? { learned: false, reason: 'own-article', sourceId: `source-${articleId}`, ruleIds: [], revision: calls.length }
        : { learned: true, sourceId: `source-${articleId || 'reference'}`, ruleIds: ['inferred:image-text-adjacency:observed'], revision: calls.length }
    },
    rememberOwn(value) {
      calls.push({ method: 'rememberOwn', value: structuredClone(value) })
      if (rememberError) throw new Error(rememberError)
      own.add(String(value.articleId))
      return { remembered: true, revision: calls.length }
    },
  }
}

function fixture(library = fakeLibrary()) {
  const bridge = new Bridge({}, library)
  bridge.ensure = () => assert.fail('Integration tests must not start a child process')
  let nextId = 0
  function dispatch(method, params, result, { owner = 'test-owner', context, ok = true, error } = {}) {
    const id = ++nextId
    let resolved, rejected, cleanupCount = 0
    bridge.pending.set(id, {
      method, params, owner, settled: false, timer: null,
      cleanup() { cleanupCount += 1 },
      resolve(value) { resolved = value }, reject(value) { rejected = value },
    })
    bridge.onLine(JSON.stringify({ id, ok, result, context, error }))
    assert.equal(bridge.pending.has(id), false)
    assert.equal(cleanupCount, 1)
    assert.equal(bridge.child, null)
    return { result: resolved, error: rejected }
  }
  return { bridge, library, dispatch }
}

function sample(articleId = '123') {
  const profile = {
    charCount: 160, imageCount: 1,
    style: {
      titles: [{ fontSize: 24, fontWeight: 700 }],
      body: { fontSize: 15, lineHeight: 1.8, paragraphGap: 18 },
      palette: { primary: '#245da8', text: '#222222', background: '#ffffff' },
      decor: { cards: 1, dividers: 1 },
    },
  }
  return {
    url: `https://v.xiumius.cn/board/v5/example/${articleId}`,
    origin: 'external',
    observation: {
      articleId,
      profile,
      sequence: [
        { kind: 'heading', textLength: 12, style: { fontSize: 24, fontWeight: 700 } },
        { kind: 'paragraph', textLength: 80, style: { fontSize: 15, lineHeight: 1.8 } },
        { kind: 'image', imageCount: 1, style: { width: 600, height: 400 } },
        { kind: 'paragraph', textLength: 80, style: { fontSize: 15 } },
      ],
      incomplete: false, truncated: false, visuallyVerified: false,
    },
  }
}

test('reference learn forwards observation.profile plus the original structural sequence', () => {
  const { dispatch, library } = fixture()
  const input = sample()
  const legacyProfile = { sourceUrl: input.url, style: { body: { fontSize: 99 } } }
  const { result, error } = dispatch('reference', { action: 'learn', url: input.url }, { profile: legacyProfile, observation: input.observation })
  assert.equal(error, undefined)
  assert.equal(library.calls.length, 1)
  assert.equal(library.calls[0].method, 'observe')
  assert.deepEqual(library.calls[0].value.profile, input.observation.profile)
  assert.deepEqual(library.calls[0].value.observation.sequence, input.observation.sequence)
  assert.equal(library.calls[0].value.url, input.url)
  assert.equal(result.learning.learned, true)
})

test('reference without new DOM observation preserves the existing profile fallback', () => {
  const { dispatch, library } = fixture()
  const profile = { sourceUrl: sample().url, style: sample().observation.profile.style }
  dispatch('reference', { action: 'learn' }, { profile })
  assert.equal(library.calls.length, 1)
  assert.deepEqual(library.calls[0].value.profile, profile)
  assert.equal(library.calls[0].value.url, profile.sourceUrl)
})

test('style_study consumes every observation with its abstract profile and removes internal samples', () => {
  const { dispatch, library } = fixture()
  const first = sample('123'), second = sample('456')
  const fallback = { style: { body: { fontSize: 16, paragraphGap: 20 } } }
  delete second.observation.profile
  second.profile = fallback
  const { result } = dispatch('style_study', { sources: [first.url, second.url] }, {
    sources: [{ url: first.url, status: 'success' }, { url: second.url, status: 'success' }],
    learningObservations: [first, second],
  })
  assert.equal(library.calls.length, 2)
  assert.deepEqual(library.calls[0].value.profile, first.observation.profile)
  assert.deepEqual(library.calls[1].value.profile, fallback)
  assert.deepEqual(library.calls[1].value.observation.sequence, second.observation.sequence)
  assert.equal(result.learning.observations.length, 2)
  assert.ok(result.learning.observations.every((entry) => entry.learned === true))
  assert.equal('learningObservations' in result, false)
  assert.equal(JSON.stringify(result).includes('learningObservations'), false)
  assert.equal(result.sources.length, 2)
})

test('article_open and external article_inspect automatically consume layout observations', () => {
  for (const method of ['article_open', 'article_inspect']) {
    const { dispatch, library } = fixture()
    const input = sample('123')
    const { result } = dispatch(method, { url: input.url }, { articleId: '123', url: input.url, learningObservations: [input] })
    assert.equal(library.calls.length, 1, method)
    assert.equal(library.calls[0].method, 'observe', method)
    assert.deepEqual(library.calls[0].value.profile, input.observation.profile, method)
    assert.equal(result.learning.observations[0].learned, true, method)
    assert.equal('learningObservations' in result, false, method)
  }
})

test('saving a known own draft remembers its ID before observing and adds no external evidence', () => {
  const { bridge, dispatch, library } = fixture()
  bridge.ownDrafts.add('test-owner')
  const input = sample('789')
  const { result } = dispatch('article_save', {}, { articleId: '789', url: input.url, learningObservations: [input] }, {
    context: { articleId: '789', url: input.url },
  })
  assert.deepEqual(library.calls.map((call) => call.method), ['rememberOwn', 'observe'])
  assert.equal(library.calls[0].value.articleId, '789')
  assert.equal(result.learning.observations[0].learned, false)
  assert.equal(result.learning.observations[0].reason, 'own-article')
  assert.equal('learningObservations' in result, false)
})

test('article_new and compose create establish ownership before processing returned observations', () => {
  for (const [method, params] of [['article_new', {}], ['compose', { mode: 'create', dryRun: false }]]) {
    const { bridge, dispatch, library } = fixture()
    const input = sample('789')
    const { result } = dispatch(method, params, { articleId: '789', url: input.url, learningObservations: [input] }, {
      context: { articleId: '789', url: input.url, newArticle: true },
    })
    assert.equal(bridge.ownDrafts.has('test-owner'), true, method)
    assert.deepEqual(library.calls.map((call) => call.method), ['rememberOwn', 'observe'], method)
    assert.equal(result.learning.observations[0].reason, 'own-article', method)
  }
})

test('an owner with a previous own draft does not mislabel a newly opened external article', () => {
  const { bridge, dispatch, library } = fixture()
  bridge.ownDrafts.add('test-owner')
  bridge.targets.set('test-owner', { articleId: '789', url: sample('789').url })
  const input = sample('123')
  const { result } = dispatch('article_open', { url: input.url }, { articleId: '123', url: input.url, learningObservations: [input] }, {
    context: { articleId: '123', url: input.url },
  })
  assert.deepEqual(library.calls.map((call) => call.method), ['observe'])
  assert.equal(result.learning.observations[0].learned, true)
  assert.equal(bridge.ownDrafts.has('test-owner'), false)
})

test('inspecting an external article while editing an own draft preserves both ownership and external evidence', () => {
  const { bridge, dispatch, library } = fixture()
  bridge.ownDrafts.add('test-owner')
  const working = { articleId: '789', url: sample('789').url }
  bridge.targets.set('test-owner', working)
  const input = sample('123')
  const { result } = dispatch('article_inspect', { url: input.url }, { articleId: '123', url: input.url, learningObservations: [input] }, { context: working })
  assert.equal(library.calls.some((call) => call.method === 'rememberOwn' && call.value.articleId === '123'), false)
  assert.equal(result.learning.observations[0].learned, true)
  assert.equal(bridge.ownDrafts.has('test-owner'), true)
})

test('a blocked article_open preserves the current own draft and does not acquire a new target', () => {
  const { bridge, dispatch, library } = fixture()
  bridge.ownDrafts.add('test-owner')
  const working = { articleId: '789', url: sample('789').url }
  bridge.targets.set('test-owner', working)
  const { result } = dispatch('article_open', { url: sample('123').url }, { blocked: true, ok: false, reason: 'unsavedDraft' }, {
    context: { articleId: '123', url: sample('123').url },
  })
  assert.equal(result.blocked, true)
  assert.equal(bridge.ownDrafts.has('test-owner'), true)
  assert.deepEqual(bridge.targets.get('test-owner'), working)
  assert.equal(library.calls.length, 0)
})

test('learning failure is a warning and never becomes reported learning success', () => {
  for (const method of ['reference', 'style_study', 'article_open', 'article_inspect']) {
    const { dispatch, library } = fixture(fakeLibrary({ observeError: 'learning store is corrupt' }))
    const input = sample('123')
    const payload = method === 'reference'
      ? { profile: input.observation.profile, observation: input.observation }
      : { articleId: '123', url: input.url, learningObservations: [input] }
    const { result, error } = dispatch(method, { action: 'learn', url: input.url }, payload)
    assert.equal(error, undefined, method)
    assert.equal(library.calls.filter((call) => call.method === 'observe').length, 1, method)
    assert.match(result.learningWarning, /corrupt/, method)
    assert.equal(result.learning, undefined, method)
    assert.equal('learningObservations' in result, false, method)
  }
})

test('failed own registration stops observation and exposes the warning', () => {
  const { bridge, dispatch, library } = fixture(fakeLibrary({ rememberError: 'cannot register own article' }))
  bridge.ownDrafts.add('test-owner')
  const input = sample('789')
  const { result } = dispatch('article_save', {}, { articleId: '789', url: input.url, learningObservations: [input] })
  assert.deepEqual(library.calls.map((call) => call.method), ['rememberOwn'])
  assert.match(result.learningWarning, /cannot register/)
  assert.equal(result.learning, undefined)
  assert.equal('learningObservations' in result, false)
})

test('failed bridge replies and reference show/list do not create learning evidence', () => {
  const { dispatch, library } = fixture()
  const rejected = dispatch('style_study', {}, undefined, { ok: false, error: 'page could not be read' })
  assert.match(rejected.error.message, /could not be read/)
  assert.equal(rejected.result, undefined)
  for (const action of ['show', 'list']) dispatch('reference', { action }, { profile: sample().observation.profile })
  assert.equal(library.calls.length, 0)
})
