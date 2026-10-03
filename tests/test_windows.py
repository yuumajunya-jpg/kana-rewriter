import ctypes
import sys
import unittest
from unittest.mock import patch


@unittest.skipUnless(sys.platform == "win32", "Windows ABI only")
class WindowsTests(unittest.TestCase):
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
        self.assertEqual(capture.source, "はいしゃ")
        self.assertEqual(capture.left_context, "歯が痛いので、")
        self.assertEqual(capture.right_context, "に行く")
        self.assertEqual([call.args for call in chord.call_args_list],
                         [(0x10, 0x24), (0x25,), (0x10, 0x23)])

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

    def test_unicode_including_surrogate_pair(self):
        from kana_rewriter.windows import Desktop
        with patch("kana_rewriter.windows.focus", return_value="target"), \
                patch("kana_rewriter.windows.user.GetAsyncKeyState", return_value=0), \
                patch("kana_rewriter.windows.send") as send:
            Desktop().replace("漢😀\n", "target")
        events = send.call_args.args[0]
        self.assertEqual([event.scan for event in events[::2]], [0x6F22, 0xD83D, 0xDE00, 13])
        self.assertEqual([event.flags for event in events], [4, 6] * 4)

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
