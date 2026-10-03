import ctypes
import sys
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(sys.platform == "win32", "Windows ABI only")
class WindowsTests(unittest.TestCase):
    def test_browser_caret_returns_from_line_end(self):
        from kana_rewriter import windows as win
        desktop = win.Desktop()
        desktop.clipboard = Mock()
        desktop.clipboard.write_text.return_value = 11
        target = (1, 2, 0, ())
        with patch.object(win, "focus", return_value=target), \
                patch.object(win.user, "GetAsyncKeyState", return_value=0), \
                patch.object(win.user, "GetClipboardSequenceNumber", return_value=11), \
                patch.object(win.user, "GetClassNameW"), \
                patch.object(win.time, "monotonic", side_effect=[0, 1]), \
                patch.object(win, "chord") as chord:
            desktop.replace("前。散歩?!後ろ", target, caret=len("前。散歩?!"))
        self.assertEqual([call.args for call in chord.call_args_list],
                         [(0x11, 0x56), (0x23,), (0x25,), (0x25,)])

    def test_standard_edit_caret_uses_absolute_utf16_offset(self):
        from kana_rewriter import windows as win
        desktop = win.Desktop()
        desktop.clipboard = Mock()
        desktop.clipboard.write_text.return_value = 11
        target = (1, 2, 0, ())

        def edit_name(hwnd, buffer, size):
            buffer.value = "Edit"
            return 4

        def message(hwnd, message, wparam, lparam, *args):
            if message == 0x00B0:
                ctypes.c_uint32.from_address(wparam).value = 100
            return 1

        with patch.object(win, "focus", return_value=target), \
                patch.object(win.user, "GetAsyncKeyState", return_value=0), \
                patch.object(win.user, "GetClipboardSequenceNumber", return_value=11), \
                patch.object(win.user, "GetClassNameW", side_effect=edit_name), \
                patch.object(win.user, "SendMessageTimeoutW", side_effect=message) as paste, \
                patch.object(win, "chord") as chord:
            desktop.replace("😀散歩?後", target, caret=4)
        self.assertEqual([call.args[1] for call in paste.call_args_list], [0x00B0, 0x0302, 0x00B1])
        self.assertEqual(paste.call_args.args[2:4], (105, 105))
        chord.assert_not_called()

    def test_line_capture_gets_both_contexts(self):
        from kana_rewriter.windows import Desktop
        desktop = Desktop()
        with patch("kana_rewriter.windows.focus", return_value="target"), \
                patch("kana_rewriter.windows.user.GetAsyncKeyState", return_value=0), \
                patch("kana_rewriter.windows.time.sleep"), \
                patch("kana_rewriter.windows.chord") as chord, \
                patch.object(desktop, "stamp", return_value=("target", 1, 2)), \
                patch.object(desktop, "copy_selection", side_effect=["歯が痛いので、はいしゃ", "歯が痛いので、はいしゃに行く"]):
            capture = desktop.capture("line", 1000)
        self.assertEqual(capture.source, "いので、はいしゃ")
        self.assertEqual(capture.left_context, "歯が痛")
        self.assertEqual(capture.right_context, "に行く")
        self.assertEqual([call.args for call in chord.call_args_list],
                         [(0x10, 0x24), (0x25,), (0x10, 0x23)])

    def test_line_capture_uses_configured_delimiters(self):
        from kana_rewriter.windows import Desktop
        desktop = Desktop(conversion_delimiters="。、")
        text = "まえ、きょう?"
        with patch("kana_rewriter.windows.focus", return_value="target"), \
                patch("kana_rewriter.windows.user.GetAsyncKeyState", return_value=0), \
                patch("kana_rewriter.windows.time.sleep"), \
                patch("kana_rewriter.windows.chord"), \
                patch.object(desktop, "stamp", return_value=("target", 1, 2)), \
                patch.object(desktop, "copy_selection", side_effect=[text, text]):
            capture = desktop.capture("line", 1000)
        self.assertEqual(capture.source, "きょう")
        self.assertEqual(capture.left_context, "まえ、")
        self.assertEqual(capture.right_context, "?")

    def test_line_mismatch_aborts(self):
        from kana_rewriter.windows import Desktop
        desktop = Desktop()
        with patch("kana_rewriter.windows.focus", return_value="target"), \
                patch("kana_rewriter.windows.user.GetAsyncKeyState", return_value=0), \
                patch("kana_rewriter.windows.time.sleep"), \
                patch("kana_rewriter.windows.chord"), \
                patch.object(desktop, "copy_selection", side_effect=["きょう", "あした"]):
            with self.assertRaises(RuntimeError):
                desktop.capture("line", 1000)

    def test_input_struct_matches_windows_abi(self):
        from kana_rewriter.windows import Input, GuiInfo
        self.assertEqual(ctypes.sizeof(Input), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)
        self.assertEqual(ctypes.sizeof(GuiInfo), 72 if ctypes.sizeof(ctypes.c_void_p) == 8 else 48)

    def test_whole_sentence_is_pasted_once(self):
        from kana_rewriter import windows as win
        desktop = win.Desktop()
        desktop.clipboard = Mock()
        saved = desktop.clipboard.snapshot.return_value
        saved.sequence = 10
        desktop.clipboard.write_text.return_value = 11
        target = (1, 2, 0, ())
        with patch.object(win, "focus", return_value=target), \
                patch.object(win.user, "GetAsyncKeyState", return_value=0), \
                patch.object(win.user, "GetClipboardSequenceNumber", return_value=11), \
                patch.object(win.user, "GetClassNameW"), \
                patch.object(win, "chord") as chord, \
                patch.object(win.time, "monotonic", side_effect=[0, 1]):
            desktop.replace("今日はいい天気だ。散歩でもしようか😀\n", target)
        desktop.clipboard.write_text.assert_called_once_with(
            "今日はいい天気だ。散歩でもしようか😀\r\n", 10)
        chord.assert_called_once_with(0x11, 0x56)
        desktop.clipboard.restore.assert_called_once_with(saved, 11)
        saved.release.assert_called_once()

    def test_standard_edit_paste_and_timeout(self):
        from kana_rewriter import windows as win
        target = (1, 2, 0, ())

        def edit_name(hwnd, buffer, size):
            buffer.value = "Edit"
            return 4

        for success in (1, 0):
            with self.subTest(success=success):
                desktop = win.Desktop()
                desktop.clipboard = Mock()
                desktop.clipboard.write_text.return_value = 11
                with patch.object(win, "focus", return_value=target), \
                        patch.object(win.user, "GetAsyncKeyState", return_value=0), \
                        patch.object(win.user, "GetClipboardSequenceNumber", return_value=11), \
                        patch.object(win.user, "GetClassNameW", side_effect=edit_name), \
                        patch.object(win.user, "SendMessageTimeoutW", return_value=success) as paste, \
                        patch.object(win, "chord") as chord:
                    if success:
                        desktop.replace("漢字", target)
                        desktop.clipboard.restore.assert_called_once()
                    else:
                        with self.assertRaisesRegex(RuntimeError, "保持"):
                            desktop.replace("漢字", target)
                        desktop.clipboard.restore.assert_not_called()
                    self.assertEqual(paste.call_args.args[:2], (2, 0x0302))
                    chord.assert_not_called()
                desktop.clipboard.snapshot.return_value.release.assert_called_once()

    def test_sequence_only_change_rechecks_payload_before_pasting(self):
        from kana_rewriter import windows as win
        target = (1, 2, 0, ())
        for changed_payload in (False, True):
            with self.subTest(changed_payload=changed_payload):
                desktop = win.Desktop()
                desktop.clipboard = Mock()
                desktop.clipboard.write_text.return_value = 11
                desktop.clipboard.confirm_text.return_value = 14
                if changed_payload:
                    desktop.clipboard.confirm_text.side_effect = RuntimeError("文字列が変わった")
                with patch.object(win, "focus", return_value=target), \
                        patch.object(win.user, "GetAsyncKeyState", return_value=0), \
                        patch.object(win.user, "GetClipboardSequenceNumber", return_value=14), \
                        patch.object(win.user, "GetClassNameW"), \
                        patch.object(win.time, "monotonic", side_effect=[0, 1]), \
                        patch.object(win, "chord") as chord:
                    if changed_payload:
                        with self.assertRaises(RuntimeError):
                            desktop.replace("完成文", target)
                        chord.assert_not_called()
                    else:
                        desktop.replace("完成文", target)
                        chord.assert_called_once_with(0x11, 0x56)
                        desktop.clipboard.restore.assert_called_once_with(
                            desktop.clipboard.snapshot.return_value, 14)
                    desktop.clipboard.confirm_text.assert_called_once_with("完成文")

    def test_focus_change_during_paste_preparation_aborts(self):
        from kana_rewriter import windows as win
        desktop = win.Desktop()
        desktop.clipboard = Mock()
        desktop.clipboard.write_text.return_value = 11
        with patch.object(win, "focus", side_effect=["target", "other"]), \
                patch.object(win.user, "GetAsyncKeyState", return_value=0), \
                patch.object(win, "chord") as chord:
            with self.assertRaisesRegex(RuntimeError, "準備中"):
                desktop.replace("漢字", "target")
            chord.assert_not_called()
        desktop.clipboard.restore.assert_called_once()

    def test_focus_change_prevents_key_send(self):
        from kana_rewriter.windows import Desktop
        with patch("kana_rewriter.windows.focus", return_value="other"), \
                patch("kana_rewriter.windows.send") as send:
            with self.assertRaises(RuntimeError):
                Desktop().replace("漢", "target")
            send.assert_not_called()

    def test_held_modifier_prevents_key_send(self):
        from kana_rewriter.windows import Desktop
        with patch("kana_rewriter.windows.focus", return_value="target"), \
                patch("kana_rewriter.windows.user.GetAsyncKeyState", return_value=0x8000), \
                patch("kana_rewriter.windows.send") as send:
            with self.assertRaises(RuntimeError):
                Desktop().replace("漢", "target")
            send.assert_not_called()
