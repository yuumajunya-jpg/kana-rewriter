/* Temporary diagnostic observer, loaded through Obsidian's official eval CLI.
 * Never selects, types, focuses, dispatches a transaction, or changes the note.
 * stop() removes listeners and restores every wrapped function.
 */
(() => {
  if (window.__kanaSelectionTrace && !window.__kanaSelectionTrace.stopped)
    return window.__kanaSelectionTrace.status();
  const fs = require("fs");
  const path = window.__kanaSelectionTracePath || "C:/workfile/kana-rewriter/logs/obsidian-selection.jsonl";
  fs.mkdirSync(require("path").dirname(path), {recursive: true});
  fs.appendFileSync(path, "", "utf8"); // verify the destination before installing observers
  const pending = [], removers = [], ids = new WeakMap();
  let serial = 0, writing = false, stopped = false, dropped = 0, error = null;
  const installed = new WeakMap();
  const identify = node => {
    if (!node) return null;
    if (!ids.has(node)) ids.set(node, ++serial);
    return {id: ids.get(node), type: node.nodeType, connected: node.isConnected,
      class: node.nodeType === 1 ? node.className : node.parentElement?.className,
      text: (node.nodeType === 3 ? node.nodeValue : node.textContent)?.slice(-120)};
  };
  const cm = () => app.workspace.activeEditor?.editor?.cm;
  function snapshot() {
    const view = cm(), selection = getSelection(), main = view?.state.selection.main;
    return {dom: {anchor: identify(selection?.anchorNode), anchorOffset: selection?.anchorOffset,
      focus: identify(selection?.focusNode), focusOffset: selection?.focusOffset,
      text: selection?.toString().slice(0, 120),
      inEditor: !!view?.contentDOM.contains(selection?.anchorNode)},
      cm: main ? {anchor: main.anchor, head: main.head, length: view.state.doc.length,
        composing: view.composing, compositionStarted: view.inputState?.compositionStarted,
        selectionChanged: view.observer?.selectionChanged,
        pendingMutations: view.observer?.queue?.length,
        tail: view.state.doc.sliceString(Math.max(0, view.state.doc.length - 140))} : null};
  }
  function capture(kind, detail) {
    if (stopped) return;
    try {
      if (pending.length >= 20000) { dropped++; return; }
      pending.push({epochMs: performance.timeOrigin + performance.now(), kind, detail, ...snapshot()});
    } catch (exc) { error = String(exc); }
  }
  function wrap(object, name, label) {
    if (!object || typeof object[name] !== "function") return;
    let names = installed.get(object);
    if (!names) installed.set(object, names = new Set());
    if (names.has(name)) return;
    names.add(name);
    const own = Object.getOwnPropertyDescriptor(object, name), original = object[name];
    function observed(...args) {
      const detail = {stack: new Error().stack?.split("\n").slice(2, 10),
        args: args.map(arg => arg?.nodeType ? identify(arg) :
          typeof arg === "number" || typeof arg === "boolean" ? arg : typeof arg)};
      capture(label + ":before", detail);
      // Preserve the original receiver, arguments, return value and exceptions.
      try { return Reflect.apply(original, this, args); }
      finally { capture(label + ":after"); }
    }
    object[name] = observed;
    removers.push(() => {
      if (object[name] !== observed) return; // do not overwrite subsequent plugin changes
      if (own) Object.defineProperty(object, name, own);
      else delete object[name];
    });
  }
  function attachView() {
    const view = cm();
    wrap(view?.docView, "updateSelection", "cm.updateSelection");
    wrap(view, "dispatchTransactions", "cm.dispatchTransactions");
  }
  for (const name of ["setBaseAndExtent", "collapse", "extend", "addRange", "removeAllRanges",
                      "selectAllChildren", "setPosition", "empty"])
    wrap(Selection.prototype, name, "js.selection." + name);
  function listen(type, phase) {
    const handler = event => {
      attachView();
      if (type !== "selectionchange" && !event.target?.closest?.(".cm-content")) return;
      capture(type + ":" + phase, {key: event.key, code: event.code, data: event.data,
        inputType: event.inputType, isComposing: event.isComposing, trusted: event.isTrusted});
    };
    document.addEventListener(type, handler, phase === "capture");
    removers.push(() => document.removeEventListener(type, handler, phase === "capture"));
  }
  for (const type of ["selectionchange", "compositionstart", "compositionupdate", "compositionend",
                       "beforeinput", "input", "keydown", "keyup"])
    for (const phase of ["capture", "bubble"]) listen(type, phase);
  const observer = new MutationObserver(records => {
    const relevant = records.filter(r => (r.target.nodeType === 1 ? r.target : r.target.parentElement)
      ?.closest?.(".cm-content"));
    if (relevant.length) capture("mutation", relevant.slice(0, 20).map(r => ({type: r.type,
      target: identify(r.target), oldValue: r.oldValue?.slice(-120),
      added: [...r.addedNodes].map(identify), removed: [...r.removedNodes].map(identify)})));
  });
  observer.observe(document.body, {subtree: true, childList: true, characterData: true,
    characterDataOldValue: true});
  const drained = [];
  function flush() {
    if (writing) return;
    if (!pending.length) { for (const done of drained.splice(0)) done(); return; }
    const batch = pending.splice(0);
    writing = true;
    fs.appendFile(path, batch.map(row => JSON.stringify(row)).join("\n") + "\n", "utf8", err => {
      writing = false;
      if (err) error = String(err);
      if (stopped) flush();
    });
  }
  // This timer only drains the log buffer; conversion never waits for it.
  const timer = setInterval(flush, 250);
  const trace = {
    get stopped() { return stopped; },
    status() { return {path, rendererPid: require("process").pid,
      stopped, pending: pending.length, writing, dropped, error}; },
    stop() {
      if (!stopped) {
        capture("stop", {dropped});
        stopped = true;
        clearInterval(timer);
        observer.disconnect();
        for (const remove of removers.reverse()) remove();
      }
      return new Promise(resolve => { drained.push(() => resolve(trace.status())); flush(); });
    }
  };
  window.__kanaSelectionTrace = trace;
  attachView();
  capture("start", {userAgent: navigator.userAgent, path});
  return trace.status();
})()
