// Offline DOM contract regression: both staged and deployed old HTML.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/app.js'), 'utf8');
async function run(voice, seed, expected) {
  const nodes = new Map();
  const node = () => ({value: '', checked: true, innerHTML: '', dataset: {}, classList: {remove(){}, add(){}}, handlers: {}, addEventListener(k,f){this.handlers[k]=f;}});
  const document = {querySelector(s) {
    if (s === '#ttsVoice' && voice === null) return null;
    if (!nodes.has(s)) nodes.set(s,node());
    return nodes.get(s);
  }, querySelectorAll(){return [];}};
  document.querySelector('#ttsText').value = 'Ahoj';
  document.querySelector('#ttsSeed').value = seed;
  if (voice !== null) document.querySelector('#ttsVoice').value = voice;
  const calls = [];
  const ctx = vm.createContext({document, console, alert(){throw Error('unexpected alert');}, setInterval(){}, setTimeout(){}, window: {}, fetch: async (url,opts) => {
    if (url === '/api/tts') {calls.push(JSON.parse(opts.body)); return {ok:true,json:async()=>({id:'job',sequence:1,voice:expected})};}
    return {ok:true,json:async()=>url === '/api/jobs?limit=20' ? {jobs:[{id:'j',kind:'tts',status:'completed',voice:'melisko'}]} : {manager:{},queue:{}}};
  }});
  vm.runInContext(source,ctx);
  await document.querySelector('#ttsGo').handlers.click();
  assert.equal(calls[0].voice, expected);
  assert.equal(calls[0].seed, seed === '' ? null : Number(seed));
  vm.runInContext('showTtsResult({id:"j",voice:"melisko",duration_s:1})',ctx);
  assert.match(document.querySelector('#ttsStatus').innerHTML,/Melisko/);
  await vm.runInContext('pollHistory()',ctx);
  assert.match(document.querySelector('#histBody').innerHTML,/Melisko/);
}
(async()=>{await run(null,'','rudo'); await run('melisko','0','melisko'); await run('rudo','42','rudo'); console.log('3 UI cases passed (old DOM, Melisko zero seed, Rudo override)');})().catch(e=>{console.error(e);process.exitCode=1;});
