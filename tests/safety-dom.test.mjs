import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import vm from 'node:vm'
const source = fs.readFileSync(new URL('../bridge.py', import.meta.url), 'utf8')
const script = source.match(/ARTICLE_EDIT_JS = r"""([\s\S]*?)"""/)[1]
function fixture(values) {
  const scopes = values.map((text, i) => ({ cellPath: 'cell-' + i, cell: { type: 'text', text: '<p>' + text + '</p>', style: {} }, $applyAsync() {} }))
  const elements = scopes.map((scope, i) => ({ querySelector: () => null, getBoundingClientRect: () => ({ width: 300, y: i * 40, x: 400 }),
    get innerText() { return scope.cell.text.replace(/<[^>]*>/g, '') }, scope }))
  const fn = vm.runInNewContext('(' + script + ')', { document: { querySelectorAll: () => elements }, angular: { element: el => ({ scope: () => el.scope }) } })
  return { scopes, fn }
}
test('ambiguous multi-cell replace refuses before changing any cell', () => {
  const f = fixture(['title', 'body', 'short tail'])
  const before = JSON.stringify(f.scopes.map(s => s.cell))
  assert.equal(f.fn({ action: 'replace_body', html: '<p>new</p>' }).ok, false)
  assert.equal(JSON.stringify(f.scopes.map(s => s.cell)), before)
})
test('explicit replacement clears even short selected paragraphs and preserves a long component title', () => {
  const f = fixture(['component title '.repeat(4), 'body '.repeat(10), 'short tail'])
  const title = f.scopes[0].cell.text
  assert.equal(f.fn({ action: 'replace_body', html: '<p>new</p>', cellIndexes: [1, 2] }).ok, true)
  assert.equal(f.scopes[0].cell.text, title)
  assert.equal(f.scopes[1].cell.text, '<p>new</p>')
  assert.equal(f.scopes[2].cell.text, '<p><br></p>')
})
test('stale cell evidence refuses before a write', () => {
  const f = fixture(['body'])
  const evidence = f.fn({ action: 'inspect' }).cells
  f.scopes[0].cell.text = '<p>manually changed</p>'
  assert.equal(f.fn({ action: 'clear_cell', cellIndex: 0, expectedCells: evidence }).ok, false)
  assert.equal(f.scopes[0].cell.text, '<p>manually changed</p>')
})
test('append never reuses an earlier empty component slot', () => {
  const f = fixture(['', 'body'])
  assert.equal(f.fn({ action: 'append_cell', text: 'new' }).ok, false)
  assert.equal(f.scopes[0].cell.text, '<p></p>')
})

const snapshotScript = fs.readFileSync(new URL('../safety.py', import.meta.url), 'utf8').match(/MODEL_SNAPSHOT_JS = r"""([\s\S]*?)"""/)[1]
function snapshot(models) {
  const els = models.map(cell => ({scope: cell ? {cell} : null}))
  const angular = {element: el => ({scope: () => el.scope})}
  const root = {querySelectorAll: () => els}
  return vm.runInNewContext('(' + snapshotScript + ')()', {document: {querySelector: sel => sel === '.tn-working-pallet' ? root : {value:'draft'}}, window: {angular}, angular})
}
test('shared model values and independent JSON readback produce identical snapshots', () => {
  const shared = {color:'red', nested:{border:'1px'}}
  const model = {type:'text', text:'body', first:shared, second:shared}
  assert.equal(JSON.stringify(snapshot([model])), JSON.stringify(snapshot([JSON.parse(JSON.stringify(model))])))
})
test('a missing model cannot silently count as a complete document', () => {
  assert.equal(snapshot([{type:'text', text:'body'}, null]).complete, false)
})
const composeScript = source.match(/COMPOSE_SET_CELLS_JS = r"""([\s\S]*?)"""/)[1]
function composeFixture(values) {
  const scopes = values.map((text,i)=>({cell:{type:'text',text,style:{}},cellPath:'p'+i,$apply(){}}))
  const els = scopes.map((scope,i)=>({scope,querySelector:()=>null,getBoundingClientRect:()=>({width:300,height:50,x:0,y:i*50}),innerText:scope.cell.text}))
  const fn = vm.runInNewContext('('+composeScript+')', {document:{querySelectorAll:()=>els},angular:{element:el=>({scope:()=>el.scope})}})
  return {scopes,fn}
}
test('compose refuses missing explicit paths and mismatched block counts before changing any style', () => {
  for (const payload of [
    {items:[{style:{color:'red'}}],paths:['missing']},
    {items:[{style:{color:'red'}}],requireExactCount:true},
    {items:[{expectedText:'a',style:{color:'red'}},{expectedText:'wrong',style:{color:'red'}}],requireExactCount:true},
  ]) {
    const f=composeFixture(['a','b']), before=JSON.stringify(f.scopes)
    assert.equal(f.fn(payload).ok,false)
    assert.equal(JSON.stringify(f.scopes),before)
  }
})

const watchScript = fs.readFileSync(new URL('../safety.py', import.meta.url), 'utf8').match(/SAVE_WATCH_START_JS = r"""([\s\S]*?)"""/)[1]
function watchFixture(elements) {
  let notify
  const window={}
  const doc={body:{},querySelectorAll:()=>elements}
  class Observer {constructor(cb){notify=cb} observe(){} disconnect(){}}
  vm.runInNewContext('('+watchScript+')()', {document:doc,window,MutationObserver:Observer,getComputedStyle:el=>({display:'block',visibility:'visible',opacity:el.visible?'1':'0'})})
  return {state:window.__xiumiSaveWatch.state,notify:(el,type='attributes')=>notify([{type,target:el,addedNodes:[]}])}
}
function toast(text,{visible=true,editor=false}={}) {
  return {nodeType:1,visible,innerText:text,getBoundingClientRect:()=>({width:300,height:40}),closest:()=>editor?{}:null,querySelectorAll:()=>[]}
}
test('preexisting save text is not an acknowledgement, but newly shown feedback is', () => {
  const stale=toast('已保存'), fresh=toast('保存成功',{visible:false})
  const f=watchFixture([stale,fresh])
  f.notify(stale)
  assert.equal(f.state.success,'')
  fresh.visible=true
  f.notify(fresh)
  assert.equal(f.state.success,'保存成功')
})
test('an article paragraph containing save text never counts as feedback', () => {
  const f=watchFixture([]), body=toast('保存成功',{editor:true})
  f.notify(body,'characterData')
  assert.equal(f.state.success,'')
})
