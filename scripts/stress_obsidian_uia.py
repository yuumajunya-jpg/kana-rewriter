"""Stress real Obsidian UIA selection in an explicitly designated TEST vault.

Start a separate Obsidian with --user-data-dir=<test profile>,
--remote-debugging-port=19273 and --force-renderer-accessibility; open a note
named repro.md in a disposable vault. This script overwrites that test note.
No OS keyboard input, IME control or foreground-window changes are performed.
Requires desktop dependencies and websocket-client. This isolates the failing
UIA selection step; it is not an end-to-end physical IME/Enter test.
"""
import argparse
import ctypes as C
from ctypes import wintypes as W
import importlib.util
import json
from pathlib import Path
import statistics
import sys
import time
import urllib.request
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class Debugger:
    def __init__(self, port):
        import websocket
        pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=5))
        pages = [p for p in pages if p["type"] == "page" and p["url"] == "app://obsidian.md/index.html"]
        if len(pages) != 1:
            raise RuntimeError("Expected exactly one test Obsidian page")
        self.ws = websocket.create_connection(pages[0]["webSocketDebuggerUrl"],
                                               suppress_origin=True, timeout=10)
        self.sequence = 0

    def call(self, method, **params):
        self.sequence += 1
        self.ws.send(json.dumps(dict(id=self.sequence, method=method, params=params)))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == self.sequence:
                if "error" in message:
                    raise RuntimeError(message["error"])
                return message.get("result", {})

    def js(self, expression):
        response = self.call("Runtime.evaluate", expression=expression,
                             awaitPromise=True, returnByValue=True)
        if "exceptionDetails" in response:
            raise RuntimeError(response["exceptionDetails"])
        return response["result"].get("value")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=19273)
    parser.add_argument("--test-vault", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=1000)
    parser.add_argument("--baseline-python", type=Path)
    parser.add_argument("--composition", action="store_true",
                        help="Commit renderer composition with CDP before reading UIA (not native TSF)")
    parser.add_argument("--view", choices=("both", "source", "live"), default="both")
    parser.add_argument("--backend-replace", action="store_true",
                        help="Renderer/UIA-only replace() test: SendInput, activity and IME are stubbed")
    parser.add_argument("--preamble", choices=("markdown", "unicode", "mixed"), default="markdown")
    parser.add_argument("--output", type=Path, default=ROOT / "build/obsidian-uia-stress.json")
    args = parser.parse_args()
    if args.samples < 1:
        parser.error("--samples must be positive")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Keep generated COM wrappers local; do not modify Python's installation.
    import comtypes.client
    import comtypes.gen
    cache = ROOT / "build/comtypes-gen"
    cache.mkdir(parents=True, exist_ok=True)
    comtypes.client.gen_dir = str(cache)
    comtypes.gen.__path__.insert(0, str(cache))
    from kana_rewriter.direct_uia import Automation, AutomationEditor
    from kana_rewriter.editor import TextState
    implementations = {"current": AutomationEditor}
    if args.baseline_python:
        spec = importlib.util.spec_from_file_location("kana_rewriter._stress_baseline", args.baseline_python)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        implementations["baseline"] = module.AutomationEditor
    debug = Debugger(args.port)
    try:
        actual_vault = debug.js("app.vault.adapter.basePath")
        if Path(actual_vault).resolve() != args.test_vault.resolve():
            raise RuntimeError("Connected vault differs from --test-vault; no edits made")
        if not (args.test_vault / "repro.md").is_file():
            raise RuntimeError("Test vault must contain repro.md")
        debug.call("Emulation.setFocusEmulationEnabled", enabled=True)
        debug.js("app.workspace.getLeaf().openFile(app.vault.getAbstractFileByPath('repro.md'))")
        title = debug.js("document.title")
        user = C.WinDLL("user32")
        user.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
        windows = []
        @C.WINFUNCTYPE(W.BOOL, W.HWND, W.LPARAM)
        def collect(hwnd, _):
            name = C.create_unicode_buffer(1024)
            user.GetWindowTextW(hwnd, name, len(name))
            if name.value == title:
                windows.append(hwnd)
            return True
        user.EnumWindows(collect, 0)
        if len(windows) != 1:
            raise RuntimeError("Expected exactly one test window with the connected page title")
        automation = Automation()
        root = automation.client.ElementFromHandle(windows[0])
        condition = automation.client.CreatePropertyCondition(
            automation.types.UIA_ClassNamePropertyId, "cm-content cm-lineWrapping")
        source = "へんかんてすと"
        samples = []
        report = {"scope": ("production replace with live UIA verification; CDP input transport; IME control stubbed"
                            if args.backend_replace else
                            "real UIA selection and renderer insertText, no physical IME or Enter"),
                  "renderer_composition": args.composition,
                  "backend_replace": args.backend_replace,
                  "os_sendinput_tested": False,
                  "input_activity_tested": False,
                  "ime_control_tested": False,
                  "preamble": args.preamble,
                  "timing_metric": "replace_ms" if args.backend_replace else "selection_ms",
                  "versions": debug.js("process.versions"), "samples": samples}
        preamble = "---\ntitle: Fixture\n---\n# Empty elements\n>\n- \n1.\n```\n```\n---\n"
        last_live = None
        for index in range(args.samples):
            live = bool(index % 2) if args.view == "both" else args.view == "live"
            if live != last_live:
                debug.js("app.workspace.activeLeaf.setViewState({type:'markdown',"
                         "state:{file:'repro.md',mode:'source',source:" + json.dumps(not live) + "}})")
                last_live = live
            use_unicode = args.preamble == "unicode" or (args.preamble == "mixed" and index % 3 == 0)
            text = "前😀e\u0301\n\n" if use_unicode else preamble
            text += ("- " + source + "\n") * (index % 35 + 1)
            text += "- 変換テスト" + source + ("。後" if index % 4 == 0 else "")
            order = list(implementations.items())
            if (index // 2) % 2:
                order.reverse()
            for label, implementation in order:
                target_offset = text.rfind(source)
                setup_text = (text[:target_offset] + text[target_offset + len(source):]
                              if args.composition else text)
                setup_caret = target_offset + (0 if args.composition else len(source))
                setup_caret = len(setup_text[:setup_caret].encode("utf-16-le")) // 2
                # Frame boundaries settle test setup, never a product delay.
                debug.js("(async()=>{const e=app.workspace.activeEditor.editor;e.setValue("
                         + json.dumps(setup_text) + ");e.setCursor(e.offsetToPos("
                         + str(setup_caret) + "));e.focus();"
                         "await new Promise(requestAnimationFrame);await new Promise(requestAnimationFrame)})()")
                if args.composition:
                    debug.call("Input.imeSetComposition", text=source,
                               selectionStart=len(source), selectionEnd=len(source))
                    debug.call("Input.insertText", text=source)
                # Switching preview modes destroys/recreates accessibility
                # nodes. Settle setup before measuring or selecting anything;
                # do not count an old node as a selection test.
                setup_deadline = time.monotonic() + 2
                setup_reads = 0
                while True:
                    setup_reads += 1
                    try:
                        element = root.FindFirst(automation.types.TreeScope_Descendants, condition)
                        if not element:
                            raise RuntimeError("Test editor missing")
                        editor = implementation.__new__(implementation)
                        editor.automation, editor.element = automation, element
                        editor.pattern = automation.pattern(element, "TextPattern")
                        editor.chromium, editor.limit, editor.timeout = True, 200000, .5
                        # Background test window: production focus/activity
                        # guards are covered by the unit/integration suite.
                        editor.check_focus = lambda: None
                        state = editor.read()
                        end = state.end
                        start = end - len(source)
                        if state.text[start:end] == source:
                            break
                    except comtypes.COMError:
                        if time.monotonic() >= setup_deadline:
                            raise
                    if time.monotonic() >= setup_deadline:
                        raise RuntimeError("UIA setup did not publish the target text/caret")
                    debug.js("new Promise(requestAnimationFrame)")
                expected = TextState(state.text, start, end)
                began = time.perf_counter()
                selection_ms = None
                try:
                    if args.backend_replace:
                        shift = False
                        def send_to_test_window(events):
                            nonlocal shift
                            if events[0].vk == 0:
                                payload = "".join(chr(e.scan) for e in events if e.flags == 4)
                                debug.call("Input.insertText", text=payload)
                                return
                            for event in events:
                                if event.vk == 0x10:
                                    shift = event.flags == 0
                                elif event.vk == 0x25:
                                    debug.call("Input.dispatchKeyEvent",
                                               type="keyUp" if event.flags & 2 else "keyDown",
                                               key="ArrowLeft", code="ArrowLeft",
                                               windowsVirtualKeyCode=37, modifiers=8 if shift else 0)
                                else:
                                    raise RuntimeError("Unexpected test selection key")
                        editor.window = (windows[0], windows[0], 0)
                        editor.check_writable = lambda: None
                        editor.ime_session_factory = lambda _: SimpleNamespace(disable=lambda: None)
                        # This tests renderer/UIA behavior only. SendInput,
                        # activity cancellation and IME control are stubbed;
                        # success here is not proof of desktop integration.
                        with patch.dict(implementation.replace.__globals__, {
                                "send": send_to_test_window, "input_tick": lambda: 0,
                                "check_input_ready": lambda _: None}):
                            editor.replace(state, start, end, "変換テスト", expected_tick=0)
                        status = "match"
                        selection_ms = (time.perf_counter() - began) * 1000
                        expected_text = text[:text.rfind(source)] + "変換テスト" + text[text.rfind(source)+len(source):]
                        ok = debug.js("app.workspace.activeEditor.editor.getValue()") == expected_text
                    else:
                        target = editor.range_for(state, start, end)
                        target.Select()  # exactly once, no reselection or fallback
                    if not args.backend_replace:
                        deadline = time.monotonic() + .5
                        status = editor.confirm_expected(expected)
                        while status == "position_changed" and time.monotonic() < deadline:
                            status = editor.confirm_expected(expected)
                        selection_ms = (time.perf_counter() - began) * 1000
                        ok = status == "match"
                        if ok:
                            debug.call("Input.insertText", text="変換テスト")
                            expected_text = text[:text.rfind(source)] + "変換テスト" + text[text.rfind(source)+len(source):]
                            ok = debug.js("app.workspace.activeEditor.editor.getValue()") == expected_text
                except (comtypes.COMError, RuntimeError) as error:
                    # Keep failed trials in the results; never reselect them.
                    status, ok = f"{type(error).__name__}: {error}", False
                if selection_ms is None:
                    selection_ms = (time.perf_counter() - began) * 1000
                detail = None
                if not ok:
                    detail = {"expected": [start, end], "snapshot_text": state.text,
                              "renderer": debug.js("(()=>{const e=app.workspace.activeEditor.editor;"
                              "const v=e.cm;return {value:e.getValue(),selection:e.getSelection(),"
                              "dom:window.getSelection().toString(),composing:v?.composing,"
                              "compositionState:v?.inputState?.composing}})()")}
                    try:
                        observed = editor.read()
                        detail["observed"] = [observed.start, observed.end]
                        detail["observed_text"] = observed.text
                    except (comtypes.COMError, RuntimeError) as error:
                        detail["read_error"] = str(error)
                samples.append(dict(index=index, implementation=label, live_preview=live,
                                    ok=ok, status=status, selection_ms=selection_ms,
                                    setup_reads=setup_reads, failure_detail=detail))
            if (index + 1) % 100 == 0:
                print(f"{index + 1}/{args.samples}", flush=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        for label in implementations:
            rows = [s for s in samples if s["implementation"] == label]
            print(label, "success", sum(s["ok"] for s in rows), "/", len(rows),
                  "replace median ms" if args.backend_replace else "selection median ms",
                  round(statistics.median(s["selection_ms"] for s in rows), 2))
        return int(any(not row["ok"] for row in samples))
    finally:
        debug.call("Emulation.setFocusEmulationEnabled", enabled=False)
        debug.ws.close()


if __name__ == "__main__":
    raise SystemExit(main())
