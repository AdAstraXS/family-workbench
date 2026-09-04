const {test} = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../../static/js/option_wheel_jobs.js'), 'utf8');
const url = '/option-wheel/jobs/1234-5678/status/';
function job(status='running') { return {kind:'option-wheel-job-v1', id:'1234-5678', status, message:'当前任务 TSLA', created_at:'2026-09-03T13:00:00Z', status_url:url, detail_url:'/option-wheel/jobs/1234-5678/', results: status==='saved' ? [{id:1,url:'/option-wheel/decisions/1/'}]:[]}; }
const response = data => ({ok:true, redirected:false, headers:{get:()=> 'application/json'}, json:async()=>data, text:async()=>''});
function setup(fetcher, restore=false) {
  const nodes = {};
  function node(id) { return nodes[id] ||= {hidden:true,textContent:'',children:[],attrs:{},
    appendChild(child){this.children.push(child); if(child.id) nodes[child.id]=child;},
    replaceChildren(){this.children=[];}, getAttribute(k){return this.attrs[k];}, setAttribute(k,v){this.attrs[k]=v;}}; }
  for(const id of ['feedback','status','detail','elapsed','check']) node('wheel-analysis-'+id);
  const form = restore ? null : node('wheel-analysis-form');
  let listener;
  if(form) Object.assign(form,{action:'/option-wheel/refresh-analysis/',elements:[{},{}],reportValidity:()=>true, addEventListener:(_,fn)=>{listener=fn;},attrs:{'data-status-url':url}});
  if(restore) node('wheel-job-page').attrs['data-status-url']=url;
  const calls=[], timers=new Map(); let n=0;
  vm.runInNewContext(source,{
    document:{getElementById:id=>nodes[id]||null,createElement:()=>node('dynamic-'+(++n))},
    FormData:class{getAll(){return ['1'];}}, Date, AbortController,
    fetch:async(...args)=>{calls.push(args);return fetcher(...args);},
    setTimeout:(fn,ms)=>{const id=++n;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id),
  });
  const flush=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
  return {nodes,calls,timers,flush,submit:()=>listener({preventDefault(){}}),
    next:async()=>{const entry=[...timers.entries()].find(([,v])=>v.ms<15000);assert.ok(entry);timers.delete(entry[0]);await entry[1].fn();await flush();}};
}
test('short submission, repeated click dedup, automatic saved links',async()=>{
  const app=setup(async(_,options)=>response(job(options.method==='POST'?'queued':'saved')));
  await app.submit(); await app.submit();
  assert.equal(app.calls.length,1);
  assert.equal(app.nodes['wheel-analysis-status'].textContent,'等待启动');
  await app.next();
  assert.equal(app.calls.length,2);assert.equal(app.calls[1][1].method,undefined);
  assert.equal(app.nodes['wheel-analysis-status'].textContent,'分析已保存');
  const links=app.nodes['wheel-job-results'].children.map(p=>p.children[0].href);
  assert.ok(links.includes('/option-wheel/decisions/1/'));
  assert.equal(app.timers.size,0);
});
test('uncertain POST recovers via known id GET; never re-POST',async()=>{
  const app=setup(async(_,options)=>{if(options.method==='POST')throw Error('lost response');return response(job('saved'));});
  await app.submit(); assert.match(app.nodes['wheel-analysis-status'].textContent,/尚未确认/);
  await app.next();
  assert.equal(app.nodes['wheel-analysis-status'].textContent,'分析已保存');
  assert.equal(app.calls.filter(c=>c[1].method==='POST').length,1);
});
test('reload on job page only GETs, transient failure retries read',async()=>{
  let n=0;
  const app=setup(async()=>{if(++n===1)throw Error('offline');return response(job('failed'));},true);
  await app.flush(); assert.match(app.nodes['wheel-analysis-status'].textContent,/暂时无法读取/);
  await app.next(); assert.equal(app.nodes['wheel-analysis-status'].textContent,'本次未保存分析');
  assert.equal(app.calls.filter(c=>c[1].method==='POST').length,0);
});
test('interrupted job ends polling, external links never rendered',async()=>{
  const result=job('interrupted'); result.results=[{id:9,url:'https://bad.example/'}];
  const app=setup(async()=>response(result),true); await app.flush();
  assert.match(app.nodes['wheel-analysis-status'].textContent,/中断/);
  assert.equal(app.timers.size,0);
  assert.equal(app.nodes['wheel-job-results'].children.length,1);
});
test('login redirects never treated as saved',async()=>{
  const app=setup(async()=>({redirected:true}),true);await app.flush();
  assert.match(app.nodes['wheel-analysis-status'].textContent,/无法读取/);
});
test('definite validation rejection displays the reason and does not poll',async()=>{
  const app=setup(async()=>({ok:false,redirected:false,status:400,headers:{get:()=> 'text/html'},text:async()=> '每次最多选择 9 个标的。'}));
  await app.submit(); await app.flush();
  assert.equal(app.nodes['wheel-analysis-status'].textContent,'无法提交分析');
  assert.equal(app.nodes['wheel-analysis-detail'].textContent,'每次最多选择 9 个标的。');
  assert.equal(app.calls.length,1);
});
