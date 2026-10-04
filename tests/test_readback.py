import sys
import unittest
from unittest.mock import Mock, patch

from kana_rewriter.editor import TextState


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class ReadbackTests(unittest.TestCase):
    def prepare(self):
        from kana_rewriter.direct_uia import AutomationEditor
        editor = AutomationEditor.__new__(AutomationEditor)
        editor.check_focus = Mock()
        editor.check_probe_focus = Mock()
        editor.pattern = Mock()
        editor.get_text = Mock()
        editor.expected_once = Mock(return_value="match")
        return editor

    def test_partial_and_same_length_wrong_text_skip_expensive_snapshot(self):
        editor = self.prepare()
        expected = TextState("前\n\n散歩。後", 6, 6)
        editor.get_text.side_effect = ["前\n\n散", "前\n\n散歩。誤", expected.text]
        with patch("kana_rewriter.direct_uia.time.monotonic", return_value=0), \
                patch("kana_rewriter.direct_uia.poll_sleep") as sleep, \
                self.assertLogs("kana_rewriter", level="DEBUG") as logs:
            self.assertEqual(editor.wait_for_state(expected, 2), expected)
        editor.expected_once.assert_called_once_with(expected, document=editor.pattern.DocumentRange)
        self.assertEqual(editor.get_text.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertTrue(any("本文照会=3回" in line and "最終確認=1回" in line
                            and "その他=" in line for line in logs.output))

    def test_initial_delay_is_bounded_and_does_not_replace_validation(self):
        editor = self.prepare()
        editor.readback_initial_delay = 0.02
        expected = TextState("散歩", 2, 2)
        editor.get_text.return_value = expected.text
        with patch("kana_rewriter.direct_uia.time.monotonic", return_value=1.99), \
                patch("kana_rewriter.direct_uia.poll_sleep") as sleep:
            self.assertEqual(editor.wait_for_state(expected, 2), expected)
        self.assertAlmostEqual(sleep.call_args.args[0], 0.01)
        editor.expected_once.assert_called_once()
        editor.check_focus.assert_called_once()
        self.assertGreaterEqual(editor.check_probe_focus.call_count, 2)

    def test_matching_text_still_requires_matching_selection_and_fresh_snapshot(self):
        from kana_rewriter.direct_uia import SnapshotPending
        editor = self.prepare()
        expected = TextState("散歩", 2, 2)
        editor.get_text.return_value = expected.text
        editor.expected_once.side_effect = ["position_changed", SnapshotPending("更新中"), "match"]
        with patch("kana_rewriter.direct_uia.time.monotonic", return_value=0), \
                patch("kana_rewriter.direct_uia.poll_sleep"):
            self.assertEqual(editor.wait_for_state(expected, 2), expected)
        self.assertEqual(editor.expected_once.call_count, 3)

    def test_pending_probe_is_retried_without_snapshot(self):
        from kana_rewriter.direct_uia import SnapshotPending
        editor = self.prepare()
        expected = TextState("散歩", 2, 2)
        editor.get_text.side_effect = [SnapshotPending("更新中"), "散歩"]
        with patch("kana_rewriter.direct_uia.time.monotonic", return_value=0), \
                patch("kana_rewriter.direct_uia.poll_sleep"):
            self.assertEqual(editor.wait_for_state(expected, 2), expected)
        editor.expected_once.assert_called_once()

    def test_timeout_and_focus_change_do_not_accept_partial_result(self):
        editor = self.prepare()
        expected = TextState("散歩", 2, 2)
        editor.get_text.return_value = "散"
        with patch("kana_rewriter.direct_uia.time.monotonic", return_value=3):
            with self.assertRaisesRegex(RuntimeError, "再送しません"):
                editor.wait_for_state(expected, 2)
        editor.expected_once.assert_not_called()
        editor.check_probe_focus.side_effect = RuntimeError("入力先変更")
        with self.assertRaisesRegex(RuntimeError, "入力先変更"):
            editor.wait_for_state(expected, 2)

    def test_unsupported_caret_pattern_is_queried_once_per_editor(self):
        from kana_rewriter.direct_uia import AutomationEditor
        editor = AutomationEditor.__new__(AutomationEditor)
        selected = Mock()
        selected.CompareEndpoints.return_value = 0
        editor.pattern = Mock()
        editor.pattern.GetSelection.return_value.Length = 1
        editor.pattern.GetSelection.return_value.GetElement.return_value = selected
        editor.element = object()
        editor.automation = Mock()
        editor.automation.pattern.return_value = None
        self.assertIs(editor.selection(), selected)
        self.assertIs(editor.selection(), selected)
        editor.automation.pattern.assert_called_once_with(editor.element, "TextPattern2")
