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
        editor.pattern = Mock()
        editor.get_text = Mock()
        editor.read_once = Mock()
        return editor

    def test_partial_and_same_length_wrong_text_skip_expensive_snapshot(self):
        editor = self.prepare()
        expected = TextState("前\n\n散歩。後", 6, 6)
        editor.get_text.side_effect = ["前\n\n散", "前\n\n散歩。誤", expected.text]
        editor.read_once.return_value = expected
        with patch("kana_rewriter.direct_uia.time.monotonic", return_value=0), \
                patch("kana_rewriter.direct_uia.time.sleep") as sleep, \
                self.assertLogs("kana_rewriter.direct_uia", level="DEBUG") as logs:
            self.assertEqual(editor.wait_for_state(expected, 2), expected)
        editor.read_once.assert_called_once()
        self.assertEqual(editor.get_text.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertIn("本文照会=3回", logs.output[-1])
        self.assertIn("最終確認=1回", logs.output[-1])

    def test_matching_text_still_requires_matching_selection_and_fresh_snapshot(self):
        from kana_rewriter.direct_uia import SnapshotPending
        editor = self.prepare()
        expected = TextState("散歩", 2, 2)
        editor.get_text.return_value = expected.text
        editor.read_once.side_effect = [TextState("散歩", 0, 2), SnapshotPending("更新中"), expected]
        with patch("kana_rewriter.direct_uia.time.monotonic", return_value=0), \
                patch("kana_rewriter.direct_uia.time.sleep"):
            self.assertEqual(editor.wait_for_state(expected, 2), expected)
        self.assertEqual(editor.read_once.call_count, 3)

    def test_pending_probe_is_retried_without_snapshot(self):
        from kana_rewriter.direct_uia import SnapshotPending
        editor = self.prepare()
        expected = TextState("散歩", 2, 2)
        editor.get_text.side_effect = [SnapshotPending("更新中"), "散歩"]
        editor.read_once.return_value = expected
        with patch("kana_rewriter.direct_uia.time.monotonic", return_value=0), \
                patch("kana_rewriter.direct_uia.time.sleep"):
            self.assertEqual(editor.wait_for_state(expected, 2), expected)
        editor.read_once.assert_called_once()

    def test_timeout_and_focus_change_do_not_accept_partial_result(self):
        editor = self.prepare()
        expected = TextState("散歩", 2, 2)
        editor.get_text.return_value = "散"
        with patch("kana_rewriter.direct_uia.time.monotonic", return_value=3):
            with self.assertRaisesRegex(RuntimeError, "再送しません"):
                editor.wait_for_state(expected, 2)
        editor.read_once.assert_not_called()
        editor.check_focus.side_effect = RuntimeError("入力先変更")
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
