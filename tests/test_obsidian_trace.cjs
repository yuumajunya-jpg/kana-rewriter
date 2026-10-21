const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('scripts/trace_obsidian_selection.js', 'utf8');

(async () => {
  const listeners = new Map(), writes = [], timer = {callback: null};
  let disconnected = false, calls = 0;
  const node = {nodeType: 3, nodeValue: 'かな', textContent: 'かな', isConnected: true};
  const selection = {anchorNode: node, focusNode: node, anchorOffset: 2, focusOffset: 2,
    toString: () => ''};
  class Selection {
    collapse(...args) { calls++; assert.equal(this, selection); return args; }
    extend() { throw new Error('original error'); }
  }
  const original = Selection.prototype.collapse;
  const view = {contentDOM: {contains: n => n === node}, composing: false,
    state: {selection: {main: {anchor: 2, head: 2}}, doc: {length: 2, sliceString: () => 'かな'}},
    observer: {queue: [], selectionChanged: false}, inputState: {},
    docView: {updateSelection() { throw new Error('must not be called during install'); }},
    dispatchTransactions() { throw new Error('must not be called during install'); }};
  const originalUpdate = view.docView.updateSelection;
  const window = {};
  const context = vm.createContext({window, Selection, app: {workspace: {activeEditor: {editor: {cm: view}}}},
    getSelection: () => selection, navigator: {userAgent: 'fake'}, performance: {timeOrigin: 10, now: () => 5},
    document: {body: {}, addEventListener(type, fn, phase) { listeners.set(type + phase, fn); },
      removeEventListener(type, fn, phase) { assert.equal(listeners.get(type + phase), fn); listeners.delete(type + phase); }},
    MutationObserver: class { observe() {} disconnect() { disconnected = true; } },
    setInterval(fn) { timer.callback = fn; return timer; }, clearInterval(t) { assert.equal(t, timer); },
    require(name) { return name === 'process' ? {pid: 123} : name === 'path' ? require('node:path') : {
      mkdirSync() {}, appendFileSync() {}, appendFile(path, data, enc, done) { writes.push(data); setImmediate(done); }
    }; }
  });
  vm.runInContext(source, context);
  assert.equal(calls, 0);
  assert.equal(listeners.size, 16);
  assert.notEqual(view.docView.updateSelection, originalUpdate);
  const wrapped = Selection.prototype.collapse;
  assert.deepEqual(wrapped.call(selection, node, 1), [node, 1]);
  assert.equal(calls, 1);
  assert.throws(() => Selection.prototype.extend.call(selection), /original error/);
  vm.runInContext(source, context); // idempotent install
  assert.equal(Selection.prototype.collapse, wrapped);
  listeners.get('selectionchangetrue')({isTrusted: true});
  timer.callback(); // stop must also wait for this in-flight file write
  const result = await window.__kanaSelectionTrace.stop();
  assert.equal(result.stopped, true);
  assert.equal(result.pending, 0);
  assert.equal(result.error, null);
  assert.equal(Selection.prototype.collapse, original);
  assert.equal(view.docView.updateSelection, originalUpdate);
  assert.equal(listeners.size, 0);
  assert.equal(disconnected, true);
  const records = writes.join('').trim().split('\n').map(JSON.parse);
  assert(records.some(r => r.kind === 'js.selection.collapse:before' && r.detail.stack.length));
  assert(records.some(r => r.kind === 'js.selection.extend:after'));
  assert(records.some(r => r.kind === 'selectionchange:capture' && r.cm.anchor === 2));
  assert.equal(records.at(-1).kind, 'stop');
  console.log('Obsidian trace: forwarding, exceptions, idempotence, asynchronous drain and cleanup passed');
})().catch(err => { console.error(err); process.exitCode = 1; });
