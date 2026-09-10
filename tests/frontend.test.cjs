// Run actual inline handlers against a minimal DOM; no network or browser needed.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const source = fs.readFileSync(require('node:path').join(__dirname, '../web/index.html'), 'utf8');
const scripts = [...source.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(match=>match[1]);
scripts.forEach(script=>new vm.Script(script));
const script = scripts.find(script=>script.includes('function readRows'));
const elements = {add:{}, tasks:{}};
const tasks = [{bot:'@test',command:'/old',enabled:true}, {bot:'@second',command:'/old',enabled:true}];
const rows = tasks.map(() => ({querySelectorAll: () => [
  {dataset:{k:'command'},type:'text',value:'/edited'},
  {dataset:{k:'enabled'},type:'checkbox',checked:false}
]}));
const context = vm.createContext({tasks, $:id=>elements[id], document:{querySelectorAll:()=>rows},
  renderTasks(){}, renderDashboard(){}, latestStatus:{}, scheduleBotProfiles(){}});
vm.runInContext(script.match(/^    function readRows\(\).*$/m)[0], context);
vm.runInContext(script.match(/^    \$\('add'\)\.onclick=.*$/m)[0], context);
vm.runInContext(script.match(/^    \$\('tasks'\)\.onclick=.*$/m)[0], context);
vm.runInContext(script.match(/^    function formatReward\(item\).*$/m)[0], context);
vm.runInContext(script.match(/^    function formatBalance\(item\).*$/m)[0], context);
assert.equal(context.formatReward({reward:{amount:6,unit:'bet'}}), '6 bet');
assert.equal(context.formatReward({reward:{amount:7,unit:'咪咪'}}), '7 咪咪');
assert.equal(context.formatReward({points:5}), '5 积分');
assert.equal(context.formatReward({}), '—');
assert.equal(context.formatBalance({balance:{amount:137,unit:'bet'}}), '137 bet');
assert.equal(context.formatBalance({balance:{amount:148,unit:'咪咪'}}), '148 咪咪');
assert.equal(context.formatBalance({}), '—');
elements.add.onclick();
assert.equal(tasks[0].command, '/edited');
assert.equal(tasks[0].enabled, false);
elements.tasks.onclick({target:{classList:{contains:()=>true},closest:()=>({dataset:{i:'1'}})}});
assert.equal(tasks[0].command, '/edited');
assert.equal(tasks[0].enabled, false);
assert.equal(tasks.length, 2);

(async () => {
  let resolveFetch, calls=0, delay;
  const pollContext = vm.createContext({
    fetch:()=>{calls++; return new Promise(resolve=>{resolveFetch=resolve;});},
    AbortSignal, document:{hidden:false}, renderStatus(){},
    loadWebsiteStatus:async()=>{}, toast(){}, clearTimeout(){},
    setTimeout:(_,ms)=>{delay=ms; return 1;}
  });
  vm.runInContext(script.slice(script.indexOf('    let pollInFlight'), script.indexOf("    document.addEventListener('visibilitychange'")), pollContext);
  const first = pollContext.poll();
  assert.equal(first, pollContext.poll());
  assert.equal(calls,1);
  resolveFetch({ok:true,json:async()=>({})});
  await first;
  assert.equal(delay,2000);
  pollContext.document.hidden=true;
  const second = pollContext.poll();
  resolveFetch({ok:true,json:async()=>({})});
  await second;
  assert.equal(delay,30000);
  pollContext.document.hidden=false;
  const failed = pollContext.poll();
  resolveFetch({ok:false});
  await failed;
  assert.equal(delay,4000);
  const recovered = pollContext.poll();
  resolveFetch({ok:true,json:async()=>({})});
  await recovered;
  assert.equal(delay,2000);

  const delayCell = {textContent:'88 ms'};
  const nodeState = {textContent:'在线',className:'node-state alive',title:''};
  const row = {querySelector:selector=>selector === '.proxy-delay-value' ? delayCell : nodeState};
  const button = {dataset:{node:'测试节点'},closest:()=>row,disabled:false,textContent:'测速'};
  let request, toastMessage;
  const delayContext = vm.createContext({
    fetch:async (url,options)=>{request={url,options}; return {ok:true,json:async()=>({delay:42})};},
    toast:message=>{toastMessage=message;}
  });
  vm.runInContext(script.match(/^    async function testProxyDelay\(button\).*$/m)[0], delayContext);
  await delayContext.testProxyDelay(button);
  assert.equal(request.url, '/api/proxy/delay');
  assert.deepEqual(JSON.parse(request.options.body), {name:'测试节点'});
  assert.equal(delayCell.textContent, '42 ms');
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, '测速');
  assert.equal(toastMessage, '测试节点：42 ms');

  delayContext.fetch=async()=>({ok:false,json:async()=>({detail:'节点不可达'})});
  delayCell.textContent='42 ms';
  await assert.rejects(delayContext.testProxyDelay(button), /节点不可达/);
  assert.equal(delayCell.textContent, '42 ms');
  assert.equal(button.disabled, false);
  assert.equal(button.textContent, '测速');
  console.log('Frontend syntax, editing, polling and manual proxy delay: passed');
})().catch(error=>{console.error(error); process.exitCode=1;});
