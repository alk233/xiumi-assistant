import test from 'node:test'
import assert from 'node:assert/strict'
import { Bridge, guardedTool, referenceTool, guideTool, GUIDE_TTL_MS } from '../index.js'

test('complete operations cannot interleave write and save with another open', async () => {
  const b = new Bridge({})
  const events = []
  const a = b.runExclusive(async () => { events.push('write-A'); await new Promise(r => setTimeout(r, 8)); events.push('save-A') }, { owner: 'a' })
  const c = b.runExclusive(async () => events.push('open-B'), { owner: 'b' })
  await Promise.all([a, c])
  assert.deepEqual(events, ['write-A', 'save-A', 'open-B'])
})
test('timed-out request sends cancellation and blocks mutation until inspection', async () => {
  const b = new Bridge({})
  const messages = []
  b.ensure = () => { b.child = { stdin: { write(line) { messages.push(JSON.parse(line)) } } } }
  b.targets.set('shared', { articleId: '100', revision: 'r1' })
  await assert.rejects(b.request('article_edit', { action: 'set_cell' }, 8), /结果未知/)
  assert.equal(messages[1].method, 'cancel')
  assert.throws(() => b.request('article_save', { confirm: true }), /对账/)
  const inspect = b.request('article_edit', { action: 'inspect' }, 1000)
  const request = messages.at(-1)
  b.onLine(JSON.stringify({ id: request.id, ok: true, result: { cells: [] }, context: { articleId: '100', revision: 'r2' } }))
  await inspect
  assert.equal(b.needsReconcile, null)
  b.failAll(new Error('test cleanup'))
})
test('in-flight abort reaches the bridge cancellation channel', async () => {
  const b = new Bridge({})
  const messages = []
  b.ensure = () => { b.child = { stdin: { write(line) { messages.push(JSON.parse(line)) } } } }
  const controller = new AbortController()
  const p = b.runExclusive(() => b.request('status', {}, 1000), { signal: controller.signal })
  await new Promise(r => setImmediate(r))
  controller.abort()
  await assert.rejects(p, /取消/)
  assert.equal(messages.at(-1).method, 'cancel')
  b.failAll(new Error('test cleanup'))
})
test('queued cancellation never runs the queued action', async () => {
  const b = new Bridge({})
  const controller = new AbortController()
  let executed = false
  const first = b.runExclusive(() => new Promise(r => setTimeout(r, 8)))
  const second = b.runExclusive(() => { executed = true }, { signal: controller.signal })
  controller.abort()
  await first
  await assert.rejects(second, /未执行/)
  assert.equal(executed, false)
})
test('tool output schemas include backend save failure evidence', () => {
  const tool = guardedTool({ name: 'fixture', parameters: { properties: {} }, output: { schema: { properties: {}, additionalProperties: false } }, execute() {} }, {}, {})
  assert.equal(tool.output.schema.properties.saveFailureReasons.type, 'array')
  assert.equal(tool.output.schema.properties.saveVerified.type, 'boolean')
})
test('layout-only refuses an unbound caller before sending a request', () => {
  const b=new Bridge({})
  b.ensure=()=>assert.fail('must not start bridge')
  assert.throws(()=>b.request('compose',{mode:'layout-only'}), /绑定文章/)
})
test('profile removal requires confirmation and a bounded ID before touching either storage', async () => {
  const t=referenceTool({request(){assert.fail('must not call bridge')}})
  await assert.rejects(t.execute({action:'remove',id:'valid-id',confirm:false}), /confirm=true/)
  await assert.rejects(t.execute({action:'remove',id:'../../other',confirm:true}), /无效/)
})
test('production guidance is opt-in and carries the actual editing and verification workflow', async () => {
  const t=guideTool({}, {guide:()=>({steps:[],rules:[]})})
  const lean=await t.execute({action:'guide'})
  assert.equal(lean.productionGuide, undefined)
  const value=await t.execute({action:'guide',detail:'full'})
  assert.match(value.productionGuide,/cellIndexes/)
  assert.match(value.productionGuide,/saveVerified/)
  assert.match(value.productionGuide,/图片/)
})
test('a missing DSH session identity cannot use a shared mutation target', () => {
  const t=guardedTool({name:'xiumi_edit',parameters:{properties:{}},description:'',execute(){assert.fail('must not run')}},{},{})
  assert.throws(()=>t.execute({action:'clear_cell'},{}), /对话身份/)
})

// 硬规定：未读制作指南时，任何产出型工具都必须被拒绝；读过之后放行；超过有效期要求重读。
test('content-producing tools refuse until xiumi_guide action=guide has been called', async () => {
  const gate={at:0,intent:'',ruleCount:0}
  const guide=guideTool({}, {guide:()=>({steps:[],rules:[{id:'r1'}],sourceCount:6})}, gate)
  const write=guardedTool({name:'xiumi_write',parameters:{properties:{}},description:'',
    execute(){return {ok:true}}},{runExclusive:fn=>fn()},{},gate)
  await assert.rejects(async () => write.execute({title:'x'},{agent:{id:'a'}}), /还没读制作指南/)
  await guide.execute({action:'guide',intent:'学术会议通知'})
  assert.ok(gate.at>0)
  assert.equal(gate.intent,'学术会议通知')
  const out=await write.execute({title:'x'},{agent:{id:'a'}})
  assert.deepEqual(out,{ok:true})
})
test('guide gate also covers xiumi_articles new/save/update but not read-only actions', async () => {
  const gate={at:0}
  const tool=guardedTool({name:'xiumi_articles',parameters:{properties:{}},description:'',execute(){return {ok:true}}},{runExclusive:fn=>fn()},{},gate)
  for (const action of ['new','save','update']) {
    await assert.rejects(async () => tool.execute({action},{agent:{id:'a'}}), /还没读制作指南/)
  }
  assert.deepEqual(await tool.execute({action:'inspect'},{agent:{id:'a'}}),{ok:true})
  assert.deepEqual(await tool.execute({action:'list'},{agent:{id:'a'}}),{ok:true})
})
test('stale guide knowledge asks for a re-read after the TTL', async () => {
  const gate={at:Date.now()-GUIDE_TTL_MS-1}
  const write=guardedTool({name:'xiumi_write',parameters:{properties:{}},description:'',execute(){assert.fail('must not run')}},{},{},gate)
  await assert.rejects(async () => write.execute({title:'x'},{agent:{id:'a'}}), /超过 60 分钟/)
})

