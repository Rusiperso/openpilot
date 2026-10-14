const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(require('node:path').join(__dirname, '../static/g4_upload.js'), 'utf8');

function harness(selected, replies) {
  const progress = [];
  const status = {replaceChildren() {}, append(node) {progress.push(node)}};
  const buttons = [{disabled:false}, {disabled:false}];
  const calls = [];
  const context = vm.createContext({
    document: {
      querySelectorAll(selector) {return selector.includes('segment-checkbox') ? selected.map(value => ({value})) : buttons},
      getElementById(id) {return id === 'video' ? {pause() {}} : status},
      createElement() {return {className:'', textContent:''}},
    },
    showUploadStatus() {},
    async fetch(url, options) {
      calls.push({url,options});
      const reply = replies.shift();
      return {ok:reply.ok, status:reply.ok ? 200 : 502, async json() {return reply}};
    },
  });
  vm.runInContext(source, context);
  return {context,calls,buttons,progress};
}

test('one server request per segment; no browser download or file repost', async () => {
  const h = harness(['00000cfb--69588de3d7--0','00000cfb--69588de3d7--0','00000cfb--69588de3d7--1'],
                    [{ok:true,remoteBasePath:'routes/TEST/'},{ok:true,remoteBasePath:'routes/TEST/'}]);
  await vm.runInContext('uploadSelectedG4Segments()',h.context);
  assert.deepEqual(h.calls.map(call => call.url),['/footage/full/upload_g4/00000cfb--69588de3d7/0','/footage/full/upload_g4/00000cfb--69588de3d7/1']);
  assert.ok(h.calls.every(call => call.options.method === 'POST' && !call.options.body));
  assert.match(h.progress[0].textContent,/2\/2/);
  assert.ok(h.buttons.every(button => !button.disabled));
});

test('a failed segment stops the sequence and permits retry', async () => {
  const h = harness(['route--0','route--1'],[{ok:false,error:'rlog가 없습니다'}]);
  await vm.runInContext('uploadSelectedG4Segments()',h.context);
  assert.equal(h.calls.length,1);
  assert.match(h.progress[0].textContent,/rlog가 없습니다/);
  assert.equal(h.progress[0].className,'alert alert-danger');
  assert.ok(h.buttons.every(button => !button.disabled));
});

test('no selected segment causes no network request', async () => {
  const h = harness([],[]);
  await vm.runInContext('uploadSelectedG4Segments()',h.context);
  assert.equal(h.calls.length,0);
});
