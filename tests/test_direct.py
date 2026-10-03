import ctypes as C
import multiprocessing
import sys
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kana_rewriter.core import Config, apply_result
from kana_rewriter.editor import TextState, make_capture, replacement_plan, python_offset, utf16_length


class SnapshotTests(unittest.TestCase):
    def test_utf16_mapping_and_invalid_surrogate_boundary(self):
        text = "前😀。さんぽ"
        self.assertEqual(utf16_length(text), len(text) + 1)
        self.assertEqual(python_offset(text, 3), 2)
        with self.assertRaises(RuntimeError):
            python_offset(text, 2)

    def test_caret_after_punctuation_keeps_suffix_and_adjusts_position(self):
        text = "今日はいい天気だ。さんぽ。次の行"
        caret = text.index("次")
        capture = make_capture(TextState(text, caret, caret), (), "line", Config(), 1)
        self.assertEqual(capture.source, "さんぽ")
        self.assertEqual(replacement_plan(capture, "散歩"),
                         (9, 12, "今日はいい天気だ。散歩。次の行", caret - 1))

    def test_selection_gets_external_context_and_routes_without_clipboard(self):
        capture = make_capture(TextState("歯が痛いので、はいしゃです", 7, 11), (),
                               "selection", Config(), 3)
        self.assertEqual((capture.left_context, capture.source, capture.right_context),
                         ("歯が痛いので、", "はいしゃ", "です"))
        backend = Mock()
        backend.apply.return_value = True
        self.assertTrue(apply_result(backend, capture, "歯医者"))
        backend.apply.assert_called_once_with(capture, "歯医者")
        backend.stamp.assert_not_called()
        backend.copy_selection.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class EngineTests(unittest.TestCase):
    def test_foreign_thread_ime_context_is_not_used_to_block_conversion(self):
        from kana_rewriter import direct_win32 as win
        with patch.object(win.user, "GetAsyncKeyState", return_value=0), \
                patch.object(win.user, "GetWindowThreadProcessId", return_value=101), \
                patch.object(win.kernel, "GetCurrentThreadId", return_value=202), \
                patch.object(win.imm, "ImmGetContext", return_value=123) as get_context, \
                patch.object(win.imm, "ImmGetCompositionStringW", return_value=8) as composition:
            win.check_input_ready(999)
        get_context.assert_not_called()
        composition.assert_not_called()

    def test_local_ime_composition_is_rejected_and_context_released(self):
        from kana_rewriter import direct_win32 as win
        with patch.object(win.user, "GetAsyncKeyState", return_value=0), \
                patch.object(win.user, "GetWindowThreadProcessId", return_value=101), \
                patch.object(win.kernel, "GetCurrentThreadId", return_value=101), \
                patch.object(win.imm, "ImmGetContext", return_value=123), \
                patch.object(win.imm, "ImmGetCompositionStringW", return_value=8), \
                patch.object(win.imm, "ImmReleaseContext") as release:
            with self.assertRaisesRegex(RuntimeError, "IMM"):
                win.check_input_ready(999)
        release.assert_called_once_with(999, 123)

    def test_worker_timeout_stops_and_never_replays(self):
        from kana_rewriter.direct import Desktop
        desktop = Desktop(Config())
        connection, process = Mock(), Mock()
        connection.poll.return_value = False
        process.is_alive.return_value = True
        desktop.connection, desktop.process = connection, process
        with self.assertRaisesRegex(RuntimeError, "再送しません"):
            desktop.apply("capture", "散歩")
        process.terminate.assert_called_once()
        with self.assertRaisesRegex(RuntimeError, "再起動"):
            desktop.apply("capture", "散歩")
        connection.send.assert_called_once_with(("apply", ("capture", "散歩")))

    def prepare(self, text="さんぽ。あと", caret=4):
        from kana_rewriter.direct import EditorEngine
        state = TextState(text, caret, caret)
        capture = make_capture(state, ((1, 2, 3), 10), "line", Config(), 1)
        editor = Mock(kind="win32")
        editor.wait_for_state = None
        engine = EditorEngine(Config())
        engine.saved = (capture, editor, state)
        return engine, editor, state, capture

    def test_replace_only_target_and_restore_caret(self):
        from kana_rewriter.input_activity import InputActivity
        activity = InputActivity()
        activity.counter = 10

        def moving_pointer():
            activity._mouse(0, 0x0200, 0)
            return activity.counter

        engine, editor, state, capture = self.prepare()
        editor.read.side_effect = [state, TextState("散歩。あと", 2, 2), TextState("散歩。あと", 3, 3)]
        with patch("kana_rewriter.direct_win32.window_identity", return_value=(1, 2, 3)), \
                patch("kana_rewriter.input_activity.user.CallNextHookEx", return_value=0), \
                patch("kana_rewriter.direct_win32.input_tick", side_effect=moving_pointer):
            self.assertTrue(engine.apply(capture, "散歩"))
        editor.replace.assert_called_once_with(state, 0, 3, "散歩", expected_tick=10)
        editor.restore_caret.assert_called_once_with(TextState("散歩。あと", 2, 2), 3, expected_tick=10)

    def test_held_mouse_button_blocks_dragging_even_without_a_new_click(self):
        from kana_rewriter import direct_win32 as win
        for key in (0x01, 0x02, 0x04, 0x05, 0x06):
            with self.subTest(key=key), patch.object(
                    win.user, "GetAsyncKeyState", side_effect=lambda k: 0x8000 if k == key else 0):
                with self.assertRaisesRegex(RuntimeError, "マウスボタン"):
                    win.check_input_ready(999)

    def test_uia_waits_for_caret_even_when_text_is_already_updated(self):
        engine, editor, state, capture = self.prepare()
        editor.kind = "uia"
        editor.read.side_effect = [state, TextState("散歩。あと", 0, 2),
                                   TextState("散歩。あと", 2, 2), TextState("散歩。あと", 3, 3)]
        with patch("kana_rewriter.direct_win32.window_identity", return_value=(1, 2, 3)), \
                patch("kana_rewriter.direct_win32.input_tick", return_value=10):
            self.assertTrue(engine.apply(capture, "散歩"))
        editor.replace.assert_called_once()
        self.assertEqual(editor.read.call_count, 4)

    def test_document_selection_or_focus_changes_cancel_and_consume_token(self):
        for target, changed in [((1, 2, 3), TextState("変更", 0, 0)),
                                ((1, 2, 3), TextState("さんぽ。あと", 2, 2)),
                                ((4, 5, 6), None)]:
            engine, editor, state, capture = self.prepare()
            editor.read.return_value = changed or state
            with patch("kana_rewriter.direct_win32.window_identity", return_value=target), \
                    patch("kana_rewriter.direct_win32.input_tick", return_value=11):
                with self.assertRaises(RuntimeError):
                    engine.apply(capture, "散歩")
                with self.assertRaises(RuntimeError):
                    engine.apply(capture, "散歩")
            editor.replace.assert_not_called()

    def test_activity_during_inference_with_unchanged_state_is_allowed(self):
        engine, editor, state, capture = self.prepare()
        editor.read.side_effect = [state, TextState("散歩。あと", 2, 2), TextState("散歩。あと", 3, 3)]
        with patch("kana_rewriter.direct_win32.window_identity", return_value=(1, 2, 3)), \
                patch("kana_rewriter.direct_win32.input_tick", return_value=99):
            self.assertTrue(engine.apply(capture, "散歩"))
        editor.replace.assert_called_once_with(state, 0, 3, "散歩", expected_tick=99)

    def test_capture_activity_checks_actual_state_and_focus(self):
        from kana_rewriter.direct import EditorEngine
        state = TextState("さんぽ", 3, 3)
        for changed, target, allowed in [(state, (1, 2, 3), True),
                                        (TextState("さんぽ！", 4, 4), (1, 2, 3), False),
                                        (TextState("さんぽ", 0, 0), (1, 2, 3), False),
                                        (state, (4, 5, 6), False)]:
            with self.subTest(changed=changed, target=target):
                engine = EditorEngine(Config())
                editor = Mock(kind="uia")
                editor.read.side_effect = [state, changed]
                with patch.object(engine, "editor", return_value=editor), \
                        patch("kana_rewriter.winapi.user.GetAsyncKeyState", return_value=0), \
                        patch("kana_rewriter.direct_win32.check_input_ready"), \
                        patch("kana_rewriter.direct_win32.input_tick", side_effect=[10, 11]), \
                        patch("kana_rewriter.direct_win32.window_identity",
                              side_effect=[(1, 2, 3), target]):
                    if allowed:
                        self.assertEqual(engine.capture("line", 1000).source, "さんぽ")
                    else:
                        with self.assertRaises(RuntimeError):
                            engine.capture("line", 1000)
                        self.assertIsNone(engine.saved)

    def test_failed_readback_never_replays_or_moves_caret(self):
        engine, editor, state, capture = self.prepare()
        editor.read.side_effect = [state, state]
        with patch("kana_rewriter.direct_win32.window_identity", return_value=(1, 2, 3)), \
                patch("kana_rewriter.direct_win32.input_tick", return_value=10):
            with self.assertRaisesRegex(RuntimeError, "再送しません"):
                engine.apply(capture, "散歩")
        editor.replace.assert_called_once()
        editor.restore_caret.assert_not_called()


def native_fixture(connection, rich):
    from ctypes import wintypes as W
    user = C.WinDLL("user32", use_last_error=True)
    user.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                                   C.c_int, C.c_int, C.c_int, C.c_int,
                                   W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p]
    user.CreateWindowExW.restype = W.HWND
    user.SendMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
    user.DestroyWindow.argtypes = [W.HWND]
    if rich:
        C.WinDLL("Msftedit.dll")
    hwnd = user.CreateWindowExW(0, "RICHEDIT50W" if rich else "EDIT", "", 0xC4,
                               0, 0, 640, 480, None, None, None, None)
    if not hwnd:
        raise C.WinError(C.get_last_error())
    initial = C.create_unicode_buffer("前😀。\r\nさんぽ。後ろ")
    user.SendMessageW(hwnd, 0x000C, 0, C.addressof(initial))
    connection.send(hwnd)
    message = W.MSG()
    try:
        while not connection.poll(0.005):
            while user.PeekMessageW(C.byref(message), None, 0, 0, 1):
                user.TranslateMessage(C.byref(message))
                user.DispatchMessageW(C.byref(message))
    finally:
        user.DestroyWindow(hwnd)
        connection.close()


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class NativeIntegrationTests(unittest.TestCase):
    @patch("kana_rewriter.direct_win32.input_tick", return_value=10)
    @patch("kana_rewriter.direct_win32.window_identity", return_value=(1, 2, 3))
    def test_cross_process_edit_and_rich_edit_multiline_unicode_undo(self, identity, tick):
        from kana_rewriter.direct_win32 import NativeEditor
        from kana_rewriter.input_activity import InputActivity, user
        activity = InputActivity()

        def moving_pointer():
            with patch.object(user, "CallNextHookEx", return_value=0):
                activity._mouse(0, 0x0200, 0)
            return activity.counter

        tick.side_effect = moving_pointer
        for rich in (False, True):
            with self.subTest(rich=rich):
                context = multiprocessing.get_context("spawn")
                parent, child = context.Pipe()
                process = context.Process(target=native_fixture, args=(child, rich))
                process.start()
                child.close()
                try:
                    self.assertTrue(parent.poll(10), "fixture did not start")
                    editor = NativeEditor(parent.recv(), Config())
                    state = editor.read()
                    start = state.text.index("さんぽ")
                    with patch("kana_rewriter.direct_win32.check_input_ready"):
                        editor.replace(state, start, start + 3, "散歩")
                    actual = editor.read()
                    self.assertEqual(actual, TextState(state.text.replace("さんぽ", "散歩"), start + 2, start + 2))
                    editor.restore_caret(actual, len(actual.text))
                    self.assertEqual(editor.read().start, len(actual.text))
                    self.assertTrue(editor.message(0x00C7))  # WM_UNDO
                    self.assertEqual(editor.read().text, state.text)
                    # Real COM ranges on our fixture; no focus change or keys.
                    from kana_rewriter.direct_uia import Automation, AutomationEditor
                    automation = Automation()
                    element = automation.client.ElementFromHandle(editor.hwnd)
                    pattern = automation.pattern(element, "TextPattern")
                    if not rich:
                        value = automation.pattern(element, "ValuePattern")
                        self.assertIsNotNone(value)
                        self.assertEqual(value.CurrentValue, state.text)
                        continue
                    self.assertIsNotNone(pattern)
                    uia = AutomationEditor.__new__(AutomationEditor)
                    uia.automation, uia.element, uia.pattern = automation, element, pattern
                    uia.limit = 1000
                    uia.check_focus = Mock()
                    uiastate = uia.read()
                    uia_start = uiastate.text.index("さんぽ")
                    selected = uia.range_for(uiastate, uia_start, uia_start + 3)
                    self.assertEqual(selected.GetText(100), "さんぽ")
                    selected.Select()
                    self.assertEqual((uia.read().start, uia.read().end), (uia_start, uia_start + 3))
                finally:
                    parent.send("stop")
                    process.join(5)
                    if process.is_alive():
                        process.terminate()
                        process.join()
                    parent.close()


class Range:
    """UIA fake where one Character covers a combining sequence."""
    def __init__(self, model, start=0, end=None):
        self.model, self.start = model, start
        self.end = len(model.text) if end is None else end

    def Clone(self):
        return Range(self.model, self.start, self.end)

    def GetText(self, limit):
        return self.model.text[self.start:self.end][:limit]

    def MoveEndpointByRange(self, endpoint, other, other_endpoint):
        value = other.start if other_endpoint == 0 else other.end
        if endpoint == 0:
            self.start = value
            self.end = max(self.end, value)
        else:
            self.end = value
            self.start = min(self.start, value)

    def MoveEndpointByUnit(self, endpoint, unit, count):
        boundaries = self.model.boundaries
        value = self.start if endpoint == 0 else self.end
        old = boundaries.index(value)
        new = max(0, min(len(boundaries) - 1, old + count))
        point = Range(self.model, boundaries[new], boundaries[new])
        self.MoveEndpointByRange(endpoint, point, endpoint)
        return new - old

    def Select(self):
        self.model.selected = self.Clone()

    def CompareEndpoints(self, endpoint, other, other_endpoint):
        a = self.start if endpoint == 0 else self.end
        b = other.start if other_endpoint == 0 else other.end
        return a - b


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class UiaRangeTests(unittest.TestCase):
    def test_blank_line_boundary_with_different_collapsed_range_anchor(self):
        # A provider can expose the same prefix at two paragraph anchors.
        # Collapsing a prefix at the next line start retains the blank-line
        # anchor, which contributes an extra newline when extended to the right.
        class ParagraphRange(Range):
            def __init__(self, model, start=0, end=None, blank_anchor=False):
                super().__init__(model, start, end)
                self.blank_anchor = blank_anchor

            def Clone(self):
                return ParagraphRange(self.model, self.start, self.end, self.blank_anchor)

            def MoveEndpointByRange(self, endpoint, other, other_endpoint):
                collapsing_prefix = endpoint == 0 and other is self and other_endpoint == 1
                super().MoveEndpointByRange(endpoint, other, other_endpoint)
                if collapsing_prefix and self.start == self.model.line_start:
                    self.blank_anchor = True

            def GetText(self, limit):
                text = super().GetText(limit)
                return ("\n" + text)[:limit] if self.blank_anchor and text else text

        for prefix, source in ((prefix, source)
                               for prefix in ("\n", "\n\n", "前😀\n\n", "前\n\n\n")
                               for source in ("さんぽ", "e\u0301さんぽ", "😀さんぽ")):
            with self.subTest(prefix=prefix, source=source):
                editor, model = self.prepare()
                model.text = prefix + source + "。後ろ"
                model.line_start = len(prefix)
                model.boundaries = [i for i in range(len(model.text) + 1)
                                    if i == len(model.text) or model.text[i] != "\u0301"]
                end = len(prefix) + len(source)
                model.selected = ParagraphRange(model, end, end)
                editor.pattern.DocumentRange = ParagraphRange(model)
                state = editor.read()
                with patch("kana_rewriter.direct_uia.input_tick", return_value=10), \
                        patch("kana_rewriter.direct_uia.check_input_ready"), \
                        patch("kana_rewriter.direct_uia.send") as send:
                    editor.replace(state, len(prefix), end, "散歩")
                self.assertEqual(editor.get_text(model.selected), source)
                self.assertEqual((model.selected.start, model.selected.end),
                                 (len(prefix), end))
                send.assert_called_once()

    def test_matching_text_at_wrong_position_is_rejected(self):
        editor, model = self.prepare()
        model.text = "さんぽ\n\nさんぽ"
        document = Range(model)
        self.assertFalse(editor.range_matches(document, Range(model, 0, 3), model.text, 5, 8))

    def test_unverifiable_blank_line_range_never_selects_or_sends(self):
        editor, model = self.prepare()
        model.text = "前\n\nさんぽ"
        model.boundaries = list(range(len(model.text) + 1))
        model.selected = Range(model, 6, 6)
        editor.pattern.DocumentRange = Range(model)
        state = editor.read()
        get_text = editor.get_text

        def inconsistent_text(range_):
            text = get_text(range_)
            return "\n" + text if (range_.start, range_.end) == (3, 6) else text

        with patch.object(editor, "get_text", side_effect=inconsistent_text), \
                patch("kana_rewriter.direct_uia.input_tick", return_value=10), \
                patch("kana_rewriter.direct_uia.check_input_ready"), \
                patch("kana_rewriter.direct_uia.send") as send:
            with self.assertRaisesRegex(RuntimeError, "置換対象範囲"):
                editor.replace(state, 3, 6, "散歩")
        self.assertEqual((model.selected.start, model.selected.end), (6, 6))
        send.assert_not_called()

    def test_line_start_conversion_with_null_bstr_for_empty_ranges(self):
        from kana_rewriter.direct import EditorEngine

        class NullEmptyRange(Range):
            def Clone(self):
                return NullEmptyRange(self.model, self.start, self.end)

            def GetText(self, limit):
                return super().GetText(limit) or None

        for prefix in ("", "前の行\n", "前😀\n\n"):
            for mode in ("line", "selection"):
                with self.subTest(prefix=prefix, mode=mode):
                    editor, model = self.prepare()
                    model.text = prefix + "さんぽ"
                    model.boundaries = list(range(len(model.text) + 1))
                    model.selected = NullEmptyRange(model, len(prefix) if mode == "selection"
                                                    else len(model.text), len(model.text))

                    class Pattern:
                        @property
                        def DocumentRange(self):
                            return NullEmptyRange(model)

                    editor.pattern = Pattern()
                    engine = EditorEngine(Config())

                    def deliver(events):
                        self.assertEqual((model.selected.start, model.selected.end),
                                         (len(prefix), len(prefix) + 3))
                        model.text = prefix + "散歩"
                        model.selected = NullEmptyRange(model, len(model.text), len(model.text))

                    with patch.object(engine, "editor", return_value=editor), \
                            patch("kana_rewriter.winapi.user.GetAsyncKeyState", return_value=0), \
                            patch("kana_rewriter.direct_win32.window_identity", return_value=(1, 2, 3)), \
                            patch("kana_rewriter.direct_win32.input_tick", return_value=10), \
                            patch("kana_rewriter.direct_win32.check_input_ready"), \
                            patch("kana_rewriter.direct_uia.input_tick", return_value=10), \
                            patch("kana_rewriter.direct_uia.check_input_ready"), \
                            patch("kana_rewriter.direct_uia.send", side_effect=deliver) as send:
                        capture = engine.capture(mode, 1000)
                        self.assertEqual(capture.source, "さんぽ")
                        self.assertTrue(engine.apply(capture, "散歩"))
                    self.assertEqual(model.text, prefix + "散歩")
                    send.assert_called_once()

    def test_null_text_for_nonempty_range_is_not_accepted(self):
        from kana_rewriter.direct_uia import SnapshotPending
        editor, model = self.prepare()
        range_ = Range(model)
        range_.GetText = Mock(return_value=None)
        with self.assertRaises(SnapshotPending):
            editor.get_text(range_)

    def test_state_change_before_selection_does_not_send_or_select(self):
        editor, model = self.prepare()
        state = editor.read()
        model.selected = Range(model, 0, 0)
        with patch("kana_rewriter.direct_uia.input_tick", return_value=10), \
                patch("kana_rewriter.direct_uia.check_input_ready"), \
                patch("kana_rewriter.direct_uia.send") as send:
            with self.assertRaisesRegex(RuntimeError, "置換直前"):
                editor.replace(state, 5, 8, "散歩")
        self.assertEqual((model.selected.start, model.selected.end), (0, 0))
        send.assert_not_called()

    def test_full_normal_conversion_with_live_uia_ranges_and_async_input(self):
        from kana_rewriter.direct import EditorEngine
        from kana_rewriter.input_activity import InputActivity
        activity = InputActivity()

        def moving_pointer():
            # Exercise every engine/backend guard with mouse motion, including
            # selection reflection and the IME switch immediately before send.
            activity._mouse(0, 0x0200, 0)
            return activity.counter

        editor, model = self.prepare()
        prefix = "文。\n" * 27 + "\n\n" + "今日はいい天気だ。"
        source, result = "さいきんあめつづきなので", "最近雨続きなので"
        model.text = prefix + source
        model.boundaries = list(range(len(model.text) + 1))
        model.selected = Range(model, len(model.text), len(model.text))
        model.pending = ""
        trace = []
        session = editor.ime_session_factory.return_value
        session.disable.side_effect = lambda: trace.append("ime_off")

        def restore_ime():
            self.assertEqual(model.text, prefix + result)
            self.assertEqual((model.selected.start, model.selected.end), (100, 100))
            trace.append("ime_restored")

        session.close.side_effect = restore_ime
        test_case = self

        class LiveRange(Range):
            def GetText(self, limit):
                if model.pending:
                    test_case.assertEqual(trace[-1], "input_sent")
                    # The browser applies the remaining queued characters
                    # after DocumentRange.GetText but before GetSelection.
                    before = super().GetText(limit)
                    model.text += model.pending
                    model.pending = ""
                    model.selected = Range(model, len(model.text), len(model.text))
                    trace.append("input_applied")
                    return before
                return super().GetText(limit)

        class LivePattern:
            @property
            def DocumentRange(self):
                return LiveRange(model)

        editor.pattern = LivePattern()
        engine = EditorEngine(Config())

        def deliver_unicode_input(events):
            self.assertEqual(trace[-1], "ime_off")
            trace.append("input_sent")
            payload = b"".join(event.scan.to_bytes(2, "little") for event in events if event.flags == 4)
            delivered = payload.decode("utf-16-le")
            self.assertEqual(delivered, result)
            self.assertEqual((model.selected.start, model.selected.end), (92, 104))
            self.assertEqual(model.text[model.selected.start:model.selected.end], source)
            model.text = model.text[:model.selected.start] + delivered[:1]
            model.pending = delivered[1:]
            model.selected = Range(model, len(model.text), len(model.text))

        with self.assertLogs("kana_rewriter.timing", level="DEBUG") as timings, \
                patch.object(engine, "editor", return_value=editor), \
                patch("kana_rewriter.direct_win32.window_identity", return_value=(1, 2, 3)), \
                patch("kana_rewriter.direct_win32.input_tick", side_effect=moving_pointer), \
                patch("kana_rewriter.direct_win32.check_input_ready"), \
                patch("kana_rewriter.winapi.user.GetAsyncKeyState", return_value=0), \
                patch("kana_rewriter.input_activity.user.CallNextHookEx", return_value=0), \
                patch("kana_rewriter.direct_uia.input_tick", side_effect=moving_pointer), \
                patch("kana_rewriter.direct_uia.check_input_ready"), \
                patch("kana_rewriter.direct_uia.send", side_effect=deliver_unicode_input) as send:
            capture = engine.capture("line", 1000)
            self.assertEqual(capture.source, source)
            self.assertEqual(len(capture.text), 104)
            self.assertTrue(apply_result(engine, capture, result))
        self.assertEqual(model.text, prefix + result)
        self.assertEqual((model.selected.start, model.selected.end), (100, 100))
        send.assert_called_once()
        self.assertIsNone(engine.saved)
        self.assertEqual(trace, ["ime_off", "input_sent", "input_applied", "ime_restored"])
        session.close.assert_called_once()

        timing_text = "\n".join(timings.output)
        for label in ("取得/全体", "適用/キー解放待ち", "UIA/範囲構築", "UIA/選択反映待ち",
                      "UIA/IME状態取得と一時オフ", "UIA/SendInput送信", "適用/入力反映確認",
                      "適用/カーソル復元と確認", "適用/IME復元"):
            self.assertIn(label + "=", timing_text)

    def test_ime_switch_or_input_failure_restores_without_resending(self):
        from kana_rewriter.direct import EditorEngine
        for stage in ("switch", "send"):
            with self.subTest(stage=stage):
                editor, _ = self.prepare()
                state = editor.read()
                capture = make_capture(state, ((1, 2, 3), 10), "line", Config(), 1)
                engine = EditorEngine(Config())
                engine.saved = (capture, editor, state)
                session = editor.ime_session_factory.return_value
                if stage == "switch":
                    session.disable.side_effect = RuntimeError("IME切替失敗")
                with patch("kana_rewriter.direct_win32.window_identity", return_value=(1, 2, 3)), \
                        patch("kana_rewriter.direct_win32.input_tick", return_value=10), \
                        patch("kana_rewriter.direct_uia.input_tick", return_value=10), \
                        patch("kana_rewriter.direct_uia.check_input_ready"), \
                        patch("kana_rewriter.direct_uia.send", side_effect=RuntimeError("送信失敗")) as send:
                    with self.assertRaises(RuntimeError):
                        engine.apply(capture, "散歩")
                    with self.assertRaises(RuntimeError):
                        engine.apply(capture, "散歩")
                session.close.assert_called_once()
                self.assertEqual(send.call_count, 0 if stage == "switch" else 1)

    def test_persistent_snapshot_inconsistency_does_not_pass_validation(self):
        from kana_rewriter.direct_uia import SnapshotPending
        editor, _ = self.prepare()
        editor.timeout = 0
        with patch.object(editor, "read_once", side_effect=SnapshotPending("不一致")):
            with self.assertRaisesRegex(RuntimeError, "整合する状態を確認できません"):
                editor.read()

    def test_normal_write_can_update_document_between_text_and_selection_reads(self):
        editor, model = self.prepare()
        model.text = "前。さんぽ"
        model.selected = Range(model, 5, 5)
        old_text = model.text

        class UpdatingRange(Range):
            def GetText(self, limit):
                if not model.updated:
                    model.updated = True
                    model.text = "前。散歩"
                    model.selected = Range(model, 4, 4)
                    return old_text
                return super().GetText(limit)

        class UpdatingPattern:
            @property
            def DocumentRange(self):
                return UpdatingRange(model)

        model.updated = False
        editor.pattern = UpdatingPattern()
        self.assertEqual(editor.read(), TextState("前。散歩", 4, 4))

    def test_native_edit_context_instruction_is_not_treated_as_document(self):
        editor, model = self.prepare()
        model.text = "この時点では、エディターにアクセスできません。"
        editor.pattern.DocumentRange = Range(model)
        editor.focused = SimpleNamespace(CurrentClassName="native-edit-context", CurrentName=model.text)
        editor.selection = Mock()
        with self.assertRaisesRegex(RuntimeError, "Shift\\+Alt\\+F1"):
            editor.read()
        editor.selection.assert_not_called()

    def test_accessible_native_edit_context_real_text_is_read(self):
        editor, model = self.prepare()
        editor.focused = SimpleNamespace(CurrentClassName="native-edit-context", CurrentName="エディターコンテンツ")
        self.assertEqual(editor.read(), TextState(model.text, 9, 9))

    def test_dedicated_caret_api_overrides_placeholder_zero_selection(self):
        editor, model = self.prepare()
        del editor.selection
        editor.element = object()
        placeholder = Range(model, 0, 0)
        editor.pattern.GetSelection = lambda: SimpleNamespace(Length=1, GetElement=lambda _: placeholder)
        dedicated = Mock()
        dedicated.GetCaretRange.return_value = (True, Range(model, 9, 9))
        editor.automation = SimpleNamespace(pattern=lambda element, name: dedicated)
        self.assertEqual(editor.read(), TextState(model.text, 9, 9))
        self.assertIn("GetCaretRange", editor.selection_source)

    def test_real_selection_is_preserved_even_with_dedicated_caret(self):
        editor, model = self.prepare()
        del editor.selection
        editor.element = object()
        real_selection = Range(model, 5, 8)
        editor.pattern.GetSelection = lambda: SimpleNamespace(Length=1, GetElement=lambda _: real_selection)
        editor.automation = Mock()
        self.assertEqual(editor.read(), TextState(model.text, 5, 8))
        editor.automation.pattern.assert_not_called()

    def test_inactive_caret_api_does_not_replace_collapsed_selection(self):
        editor, model = self.prepare()
        del editor.selection
        editor.element = object()
        selection = Range(model, 5, 5)
        editor.pattern.GetSelection = lambda: SimpleNamespace(Length=1, GetElement=lambda _: selection)
        dedicated = Mock()
        dedicated.GetCaretRange.return_value = (False, Range(model, 9, 9))
        editor.automation = SimpleNamespace(pattern=lambda element, name: dedicated)
        self.assertEqual(editor.read(), TextState(model.text, 5, 5))

    def test_selection_verification_uses_textual_positions(self):
        editor, model = self.prepare()
        state = editor.read()
        target = editor.range_for(state, 5, 8)
        target.CompareEndpoints = Mock(return_value=-1)
        with patch.object(editor, "range_for", return_value=target), \
                patch("kana_rewriter.direct_uia.input_tick", return_value=10), \
                patch("kana_rewriter.direct_uia.check_input_ready"), \
                patch("kana_rewriter.direct_uia.send") as send:
            editor.replace(state, 5, 8, "散歩")
        send.assert_called_once()
        target.CompareEndpoints.assert_not_called()

    def test_incorrect_selection_never_sends_text(self):
        editor, model = self.prepare()
        editor.timeout = 0
        state = editor.read()
        target = editor.range_for(state, 5, 8)
        target.Select = Mock()  # provider acknowledges but does not apply
        with patch.object(editor, "range_for", return_value=target), \
                patch("kana_rewriter.direct_uia.input_tick", return_value=10), \
                patch("kana_rewriter.direct_uia.check_input_ready"), \
                patch("kana_rewriter.direct_uia.send") as send:
            with self.assertRaisesRegex(RuntimeError, "要求5:8、実際9:9"):
                editor.replace(state, 5, 8, "散歩")
        send.assert_not_called()

    def test_chromium_detection_uses_framework_or_window_class(self):
        from kana_rewriter.direct_uia import AutomationEditor
        for framework, window_class, expected in [("Chrome", "other", True),
                                                   ("Win32", "chrome_renderwidgethosthwnd", True),
                                                   ("Firefox", "mozillawindowclass", False)]:
            with self.subTest(framework=framework, window_class=window_class):
                focused = SimpleNamespace(CurrentFrameworkId=framework, GetRuntimeId=lambda: [1, 2],
                                          CurrentClassName="input", CurrentControlType=50004,
                                          CurrentName="input")
                with patch("kana_rewriter.direct_uia.window_identity", return_value=(1, 2, 3)), \
                        patch("kana_rewriter.direct_uia.class_name", return_value=window_class), \
                        patch.object(AutomationEditor, "check_writable"):
                    editor = AutomationEditor(Mock(), focused, focused, Mock(), Config())
                self.assertEqual(editor.chromium, expected)
                self.assertEqual(editor.ime_check, "auto")

    def test_only_nonempty_active_uia_composition_blocks_editing(self):
        for distance, text, blocked in [(0, "残存文字", False), (-1, "あ", True), (-1, "", False)]:
            with self.subTest(distance=distance, text=text):
                editor, _ = self.prepare()
                editor.ime_check, editor.chromium = "strict", False
                active = Mock()
                active.CompareEndpoints.return_value = distance
                active.GetText.return_value = text
                composing = Mock()
                composing.GetActiveComposition.return_value = active
                editor.focused = SimpleNamespace(CurrentIsPassword=False, CurrentIsEnabled=True,
                                                 CurrentHasKeyboardFocus=True)
                editor.element = object()
                editor.pattern.DocumentRange.GetAttributeValue = lambda attribute: False
                editor.automation = SimpleNamespace(
                    types=SimpleNamespace(UIA_IsReadOnlyAttributeId=1),
                    pattern=lambda element, name: composing if name == "TextEditPattern" else None)
                # prepare() mocks this method for range-only tests.
                from kana_rewriter.direct_uia import AutomationEditor
                if blocked:
                    with self.assertRaisesRegex(RuntimeError, "UIA TextEditPattern"):
                        AutomationEditor.check_writable(editor)
                else:
                    AutomationEditor.check_writable(editor)
                if distance == 0:
                    active.GetText.assert_not_called()

    def test_chromium_cached_composition_is_advisory_in_auto_mode(self):
        from kana_rewriter.direct_uia import AutomationEditor
        for mode, chromium, blocked in [("auto", True, False), ("strict", True, True),
                                        ("auto", False, True), ("off", True, False)]:
            with self.subTest(mode=mode, chromium=chromium):
                editor, _ = self.prepare()
                editor.ime_check, editor.chromium = mode, chromium
                editor.focused = SimpleNamespace(CurrentIsPassword=False, CurrentIsEnabled=True,
                                                 CurrentHasKeyboardFocus=True)
                editor.element = object()
                editor.pattern.DocumentRange.GetAttributeValue = lambda _: False
                active = Mock()
                active.CompareEndpoints.return_value = -1
                active.GetText.return_value = "確定した文章"
                composing = Mock()
                composing.GetActiveComposition.return_value = active
                editor.automation = SimpleNamespace(
                    types=SimpleNamespace(UIA_IsReadOnlyAttributeId=1),
                    pattern=Mock(side_effect=lambda element, name: composing
                                 if name == "TextEditPattern" else None))
                if blocked:
                    with self.assertRaisesRegex(RuntimeError, "UIA TextEditPattern"):
                        AutomationEditor.check_writable(editor)
                else:
                    AutomationEditor.check_writable(editor)
                if mode == "off":
                    composing.GetActiveComposition.assert_not_called()

    def prepare(self):
        from kana_rewriter.direct_uia import AutomationEditor
        model = SimpleNamespace(text="前😀e\u0301。さんぽ。後", boundaries=[0, 1, 2, 4, 5, 6, 7, 8, 9, 10])
        model.selected = Range(model, 9, 9)
        editor = AutomationEditor.__new__(AutomationEditor)
        editor.limit, editor.timeout, editor.window = 1000, 0.05, (1, 2, 3)
        editor.pattern = SimpleNamespace(DocumentRange=Range(model))
        editor.check_focus = Mock()
        editor.check_writable = Mock()
        editor.ime_session_factory = Mock(return_value=Mock())
        editor.selection = lambda: model.selected
        return editor, model

    def test_verified_range_mapping_with_emoji_and_combining_sequence(self):
        editor, model = self.prepare()
        state = editor.read()
        selected = editor.range_for(state, 5, 8)
        self.assertEqual(selected.GetText(100), "さんぽ")
        with self.assertRaises(RuntimeError):
            editor.point(editor.pattern.DocumentRange, model.text, 3)

    def test_select_then_one_unicode_batch_without_deleting_first(self):
        editor, model = self.prepare()
        state = editor.read()
        def verify_send(events):
            self.assertEqual((model.selected.start, model.selected.end), (5, 8))
            self.assertEqual([(e.scan, e.flags) for e in events],
                             [(ord("散"), 4), (ord("散"), 6), (ord("歩"), 4), (ord("歩"), 6)])
        with patch("kana_rewriter.direct_uia.input_tick", return_value=10), \
                patch("kana_rewriter.direct_uia.check_input_ready"), \
                patch("kana_rewriter.direct_uia.send", side_effect=verify_send) as send:
            editor.replace(state, 5, 8, "散歩")
        send.assert_called_once()

    def test_input_changes_during_select_abort_before_send(self):
        editor, _ = self.prepare()
        state = editor.read()
        with patch("kana_rewriter.direct_uia.input_tick", side_effect=[10, 10, 11]), \
                patch("kana_rewriter.direct_uia.check_input_ready"), \
                patch("kana_rewriter.direct_uia.send") as send:
            with self.assertRaises(RuntimeError):
                editor.replace(state, 5, 8, "散歩")
        send.assert_not_called()

    def test_click_wheel_or_key_during_select_still_blocks_input(self):
        from kana_rewriter.input_activity import InputActivity, user
        for kind, message in (("mouse", 0x0201), ("mouse", 0x020A), ("keyboard", 0x0100)):
            with self.subTest(kind=kind, message=message):
                editor, model = self.prepare()
                state = editor.read()
                activity = InputActivity()
                selected = editor.range_for(state, 5, 8)

                def select_and_interact():
                    model.selected = selected.Clone()
                    getattr(activity, "_" + kind)(0, message, 0)
                    activity._mouse(0, 0x0200, 0)

                selected.Select = select_and_interact
                with patch.object(editor, "range_for", return_value=selected), \
                        patch.object(user, "CallNextHookEx", return_value=0), \
                        patch("kana_rewriter.direct_uia.input_tick", side_effect=lambda: activity.counter), \
                        patch("kana_rewriter.direct_uia.check_input_ready"), \
                        patch("kana_rewriter.direct_uia.send") as send:
                    with self.assertRaisesRegex(RuntimeError, "選択の反映待ち"):
                        editor.replace(state, 5, 8, "散歩")
                send.assert_not_called()

    def test_tab_rejected_before_selection(self):
        editor, model = self.prepare()
        with self.assertRaises(ValueError):
            editor.replace(editor.read(), 5, 8, "散歩\t")
        self.assertEqual((model.selected.start, model.selected.end), (9, 9))
