import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from kana_rewriter.__main__ import FixedConverter, main
from kana_rewriter.core import Config
from kana_rewriter.editor import TextState, simple_caret_text


class ComparisonCliTests(unittest.TestCase):
    def test_fixed_result_rejects_other_source(self):
        converter = FixedConverter("さんぽ", "散歩")
        self.assertEqual(converter.convert_selection("さんぽ", "前", "後"), "散歩")
        with self.assertRaises(RuntimeError):
            converter.convert_selection("べつ", "", "")

    def test_comparison_does_not_construct_or_load_model(self):
        args = ["kana_rewriter", "--config", "config.toml", "--editor-worker", "python", "--benchmark-source", "さんぽ",
                "--benchmark-result", "散歩"]
        with patch.object(sys, "argv", args), patch("kana_rewriter.__main__.Converter") as model, \
                patch("kana_rewriter.__main__.Config.load", return_value=Config(editor_worker="native")), \
                patch("kana_rewriter.__main__.run_windows", return_value=0) as run:
            self.assertEqual(main(), 0)
        model.assert_not_called()
        self.assertIsInstance(run.call_args.args[0], FixedConverter)
        self.assertEqual(run.call_args.args[1].editor_worker, "python")


@unittest.skipUnless(sys.platform == "win32", "Windows UIA")
class OptimizedUiaTests(unittest.TestCase):
    def prepare(self):
        from tests import test_direct
        return test_direct.UiaRangeTests().prepare()

    def test_expected_state_distinguishes_position_and_text_changes(self):
        editor, model = self.prepare()
        state = editor.read()
        self.assertEqual(editor.confirm_expected(state), "match")
        self.assertEqual(editor.confirm_expected(TextState(state.text, 0, 0)), "position_changed")
        model.text = "変更"
        self.assertEqual(editor.confirm_expected(state), "text_changed")

    def test_document_change_during_expected_check_is_not_accepted(self):
        from kana_rewriter.direct_uia import SnapshotPending
        editor, model = self.prepare()
        expected = editor.read()
        original = editor.get_text
        calls = 0
        def mutate(range_):
            nonlocal calls
            calls += 1
            result = original(range_)
            if calls == 2:
                model.text = model.text[:-1] + "外"
            return result
        with patch.object(editor, "get_text", side_effect=mutate):
            with self.assertRaises(SnapshotPending):
                editor.expected_once(expected)

    def test_matching_probe_still_checks_exact_focus(self):
        editor, _ = self.prepare()
        expected = editor.read()
        editor.check_probe_focus = Mock()
        editor.check_focus = Mock(side_effect=RuntimeError("入力先が変わった"))
        with self.assertRaisesRegex(RuntimeError, "入力先"):
            import time
            editor.wait_for_state(expected, time.monotonic() + 1)

    def test_probe_rejects_changed_window_without_uia_query(self):
        from kana_rewriter.direct_uia import AutomationEditor
        editor, _ = self.prepare()
        with patch("kana_rewriter.direct_uia.window_identity", return_value=(4, 5, 6)):
            with self.assertRaisesRegex(RuntimeError, "入力先"):
                AutomationEditor.check_probe_focus(editor)
        editor.check_focus.assert_not_called()

    def test_punctuation_batch_and_complex_text_fallback(self):
        editor, _ = self.prepare()
        editor.chromium = True
        editor.automation = SimpleNamespace(types=SimpleNamespace(
            UIA_TextFlowDirectionsAttributeId=1, UIA_EditControlTypeId=50004))
        editor.element = SimpleNamespace(CurrentControlType=50004)
        editor.pattern.DocumentRange.GetAttributeValue = Mock(return_value=0)
        editor.replace = Mock()
        state = TextState("さんぽ。あと", 4, 4)
        self.assertEqual(editor.replace_positioned(state, 0, 3, "散歩", 10, 3), 3)
        self.assertEqual(editor.replace.call_args.kwargs["caret_steps"], 1)
        state = TextState("さんぽ。😀", 4, 4)
        self.assertEqual(editor.replace_positioned(state, 0, 3, "散歩", 10, 3), 2)
        self.assertEqual(editor.replace.call_args.kwargs["caret_steps"], 0)
        self.assertFalse(simple_caret_text("e\u0301"))
        self.assertFalse(simple_caret_text("あ\u200f"))

    def test_unicode_and_arrows_are_sent_once_in_order(self):
        editor, model = self.prepare()
        state = editor.read()
        with patch("kana_rewriter.direct_uia.input_tick", return_value=10), \
                patch("kana_rewriter.direct_uia.check_input_ready"), \
                patch("kana_rewriter.direct_uia.send") as send:
            editor.replace(state, 5, 8, "散歩", caret_steps=1)
        send.assert_called_once()
        events = send.call_args.args[0]
        self.assertEqual([(event.scan, event.flags) for event in events[:4]],
                         [(ord("散"), 4), (ord("散"), 6), (ord("歩"), 4), (ord("歩"), 6)])
        self.assertEqual([(event.vk, event.flags) for event in events[4:]], [(0x27, 0), (0x27, 2)])

    def test_timer_unavailable_falls_back_and_cleanup_closes_handle(self):
        from kana_rewriter import waiting
        waiting.close_timer()
        with patch.object(waiting.kernel, "CreateWaitableTimerExW", return_value=None), \
                patch.object(waiting.time, "sleep") as fallback:
            waiting.sleep(.002)
            fallback.assert_called_once_with(.002)
        waiting.close_timer()
        waiting.local.timer = 123
        with patch.object(waiting.kernel, "CloseHandle") as close:
            waiting.close_timer()
            close.assert_called_once_with(123)
        self.assertFalse(hasattr(waiting.local, "timer"))
