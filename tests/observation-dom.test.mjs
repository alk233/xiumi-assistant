import test from 'node:test'
import assert from 'node:assert/strict'
import vm from 'node:vm'
import { readFileSync, mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { LearningLibrary } from '../learning.js'

const pythonSource = readFileSync(new URL('../observations.py', import.meta.url), 'utf8')
const script = pythonSource.split('LAYOUT_OBSERVATION_JS = r"""')[1].split('"""')[0]
// Compile the exact browser function; no browser, network, shell automation or DOM packages.
new vm.Script(`(${script})`)

class Element {
  constructor(tag, { attrs = {}, style = {}, x = 0, y = 0, width = 700, height = 30, text = '' } = {}) {
    this.nodeType = 1
    this.localName = tag
    this.tagName = tag.toUpperCase()
    this.attrs = attrs
    this.style = style
    this.rect = { x, y, left: x, top: y, width, height, right: x + width, bottom: y + height }
    this.children = []
    this.childNodes = text ? [{ nodeType: 3, nodeValue: text }] : []
    this.parentElement = null
    if (tag === 'img') {
      this.complete = true
      this.naturalWidth = width
      this.naturalHeight = height
      this.src = 'https://private.example/PRIVATE_IMAGE_URL.jpg'
      this.alt = 'PRIVATE_IMAGE_DESCRIPTION'
    }
  }
  append(...children) {
    for (const child of children) {
      child.parentElement = this
      this.children.push(child)
      this.childNodes.push(child)
    }
    return this
  }
  getAttribute(name) { return this.attrs[name] ?? null }
  matches(selector) {
    return selector.split(',').some((part) => {
      const s = part.trim()
      if (s === '*') return true
      if (s.startsWith('.')) return String(this.attrs.class || '').split(' ').includes(s.slice(1))
      if (s.startsWith('#')) return this.attrs.id === s.slice(1)
      const attribute = s.match(/^\[([\w-]+)="([^"]+)"\]$/)
      if (attribute) return this.attrs[attribute[1]] === attribute[2]
      return this.localName.toLowerCase() === s.toLowerCase()
    })
  }
  closest(selector) {
    for (let node = this; node; node = node.parentElement) if (node.matches(selector)) return node
    return null
  }
  contains(other) {
    for (let node = other; node; node = node.parentElement) if (node === this) return true
    return false
  }
  querySelectorAll(selector) {
    const result = []
    for (const child of this.children) {
      if (child.matches(selector)) result.push(child)
      result.push(...child.querySelectorAll(selector))
    }
    return result
  }
  getBoundingClientRect() {
    for (let node = this; node; node = node.parentElement) {
      if (node.style.display === 'none') return { ...this.rect, width: 0, height: 0 }
    }
    return this.rect
  }
}

function context(root, url = 'https://v.xiumius.cn/board/v5/example/123?private=DROP_QUERY') {
  const html = new Element('html'), body = new Element('body')
  html.append(body)
  if (root) body.append(root)
  const document = {
    body, documentElement: html, title: 'PRIVATE_PAGE_TITLE',
    querySelectorAll: (selector) => html.querySelectorAll(selector),
  }
  const getComputedStyle = (el) => {
    if (el.style.throwMeasurement) throw new Error('fake-measurement-error')
    return {
      display: 'block', visibility: 'visible', opacity: '1', transform: 'none',
      fontSize: '15px', fontWeight: '400', lineHeight: '27px', letterSpacing: 'normal',
      color: 'rgb(34, 34, 34)', backgroundColor: 'rgba(0, 0, 0, 0)', backgroundImage: 'none',
      marginTop: '0px', marginBottom: '0px', paddingTop: '0px', paddingBottom: '0px',
      paddingLeft: '0px', paddingRight: '0px', borderRadius: '0px', textAlign: 'left',
      borderTopWidth: '0px', borderRightWidth: '0px', borderBottomWidth: '0px', borderLeftWidth: '0px',
      borderTopStyle: 'none', borderRightStyle: 'none', borderBottomStyle: 'none', borderLeftStyle: 'none',
      borderTopColor: 'rgb(0, 0, 0)', borderRightColor: 'rgb(0, 0, 0)',
      borderBottomColor: 'rgb(0, 0, 0)', borderLeftColor: 'rgb(0, 0, 0)', ...el.style,
    }
  }
  return { document, getComputedStyle, location: { href: url }, URL }
}
function run(root, url) { return vm.runInNewContext(`(${script})()`, context(root, url)) }

function complexRoot() {
  const root = new Element('div', { attrs: { class: 'tn-article-body' }, height: 2400, style: { backgroundColor: 'rgb(240, 246, 250)' } })
  const nav = new Element('nav', { text: 'PRIVATE_NAVIGATION_CONTENT' })
  const toolbar = new Element('div', { attrs: { role: 'toolbar' }, text: 'PRIVATE_TOOLBAR_CONTENT' })
  const heading = new Element('h2', { y: 30, text: 'PRIVATE_HEADING_TEXT', style: { fontSize: '24px', fontWeight: '700', lineHeight: '36px', color: 'rgb(25, 80, 160)' } })
  const card = new Element('section', { y: 100, height: 600, width: 680,
    style: { backgroundColor: 'rgb(255, 255, 255)', paddingTop: '20px', paddingBottom: '20px', borderTopWidth: '1px', borderTopStyle: 'solid' } })
  const paragraph = new Element('p', { y: 120, height: 60, text: 'PRIVATE_PARAGRAPH_BODY_'.repeat(3), style: { marginBottom: '18px' } })
  const imageA = new Element('img', { x: 20, y: 250, width: 300, height: 240 })
  const imageB = new Element('img', { x: 330, y: 310, width: 300, height: 180 })
  const paragraphB = new Element('p', { y: 520, text: 'PRIVATE_SECOND_PARAGRAPH', style: { marginBottom: '18px' } })
  card.append(paragraph, imageA, imageB, paragraphB)
  const divider = new Element('hr', { y: 740, height: 1, style: { borderTopWidth: '1px', borderTopStyle: 'solid' } })
  const svg = new Element('svg', { y: 800, height: 200 })
  const foreignObject = new Element('foreignObject', { y: 810, height: 150 })
  const svgText = new Element('div', { y: 820, text: 'PRIVATE_FOREIGN_OBJECT_TEXT', style: { fontSize: '90px', lineHeight: '100px' } })
  foreignObject.append(svgText)
  svg.append(foreignObject)
  root.append(nav, toolbar, heading, card, divider, svg)
  return root
}

test('exact JS compiles and extracts only structure, sizes, grouping and SVG text presence', () => {
  const result = run(complexRoot())
  const o = result.observation
  const json = JSON.stringify(result)
  for (const privateValue of ['PRIVATE_', 'private.example', '.jpg', 'DROP_QUERY']) assert.equal(json.includes(privateValue), false, privateValue)
  assert.equal(o.rootKind, 'xiumi-public')
  assert.equal(o.visuallyVerified, false)
  assert.equal(o.profile.style.body.fontSize, 15)
  assert.equal(o.profile.style.body.lineHeight, 1.8)
  assert.equal(o.profile.style.body.paragraphGap, 18)
  assert.equal(o.metrics.hasSvgForeignObjectText, true)
  assert.equal(o.metrics.editorEditabilityVerified, false)
  assert.equal(o.metrics.imageGroups[0].align, 'bottom')
  assert.equal(o.metrics.imageGroups[0].gapPx, 10)
  assert.ok(o.sequence.some((entry) => entry.kind === 'image-group'))
  assert.ok(o.sequence.some((entry) => entry.kind === 'card'))
  assert.ok(o.sequence.some((entry) => entry.kind === 'divider'))
  const foreign = o.sequence.find((entry) => entry.insideSvgForeignObject)
  assert.ok(foreign)
  assert.equal(foreign.fontMetricsExcluded, true)
  assert.equal(foreign.style.fontSize, undefined)
  assert.ok(o.metrics.imageTextAdjacency.some((entry) => entry.beforeTextIndex !== undefined))
})

test('observation profile and sequence produce executable LearningLibrary rules', (t) => {
  const directory = mkdtempSync(join(tmpdir(), 'xiumi-dom-learning-'))
  t.after(() => rmSync(directory, { recursive: true, force: true }))
  const result = run(complexRoot())
  const library = new LearningLibrary({ directory, seedDirectory: null })
  const learned = library.observe({ ...result, profile: result.observation.profile })
  assert.equal(learned.learned, true)
  const techniques = library.list().rules.map((rule) => rule.technique)
  for (const technique of ['title-hierarchy', 'short-paragraphs', 'image-groups', 'whitespace-rhythm', 'card-grouping', 'section-dividers', 'restrained-palette']) {
    assert.ok(techniques.some((item) => item.startsWith(technique)), technique)
  }
  assert.ok(library.guide({ categories: ['imagery'] }).steps.length > 0)
})

test('wechat and editor roots are supported, with navigation excluded', () => {
  const wechat = complexRoot()
  wechat.attrs = { id: 'js_content' }
  assert.equal(run(wechat, 'https://mp.weixin.qq.com/s/public-token?tracking=DROP_QUERY').observation.rootKind, 'wechat-article')
  const editor = complexRoot()
  editor.attrs = { class: 'tn-editor-paper' }
  const result = run(editor, 'https://xiumi.us/studio/v5?tracking=DROP_QUERY#/paper/for/123')
  assert.equal(result.observation.rootKind, 'xiumi-editor')
  assert.equal(result.url, 'https://xiumi.us/studio/v5#/paper/for/123')
  assert.equal(result.observation.charCount, run(complexRoot()).observation.charCount)
})

test('missing/empty content raises and real partial measurements are marked incomplete', () => {
  assert.throws(() => run(null), /no-content-root/)
  assert.throws(() => run(new Element('article')), /no-measurable-content/)
  const partial = complexRoot()
  partial.append(new Element('p', { text: 'not emitted because measurement fails', style: { throwMeasurement: true } }))
  const result = run(partial)
  assert.equal(result.observation.incomplete, true)
  assert.ok(result.observation.sampling.measurementErrors > 0)
  assert.equal(result.observation.visuallyVerified, false)
})

test('large documents have bounded sequences and explicit truncation', () => {
  const root = new Element('article', { height: 20000 })
  for (let index = 0; index < 650; index++) root.append(new Element('p', { y: index * 24, text: 'PRIVATE_BODY_TEXT', height: 20 }))
  const result = run(root)
  assert.equal(result.observation.sequence.length, 600)
  assert.equal(result.observation.truncated, true)
  assert.equal(result.observation.incomplete, true)
  assert.ok(result.observation.limitations.includes('sequence-truncated'))
  assert.equal(JSON.stringify(result).includes('PRIVATE_BODY_TEXT'), false)
})

test('paragraph gaps can come from measured geometry without inventing missing CSS values', () => {
  const root = new Element('article', { height: 400 })
  root.append(
    new Element('p', { y: 20, height: 30, text: 'PRIVATE_FIRST_BODY' }),
    new Element('p', { y: 70, height: 30, text: 'PRIVATE_SECOND_BODY' }),
  )
  const result = run(root)
  assert.equal(result.observation.profile.style.body.paragraphGap, 20)
  assert.equal(result.observation.profile.style.body.letterSpacing, undefined)
})

test('CSS-scaled covers cannot supply an unscaled heading-font hierarchy', () => {
  const root = complexRoot()
  root.children[2].style.transform = 'matrix(0.3,0,0,0.3,0,0)'
  const result = run(root)
  const scaled = result.observation.sequence.find((entry) => entry.fontMetricsExcluded && !entry.insideSvgForeignObject)
  assert.ok(scaled)
  assert.equal(scaled.style.fontSize, undefined)
  assert.ok(result.observation.limitations.includes('font-metrics-with-transform-excluded'))
})
