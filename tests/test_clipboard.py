import ctypes as C
import sys
import unittest
from unittest.mock import Mock, patch, call


@unittest.skipUnless(sys.platform == "win32", "Windows clipboard only")
class ClipboardTests(unittest.TestCase):
    def test_native_duplicate_is_independent_of_original_memory(self):
        from ctypes import wintypes as W
        from kana_rewriter import clipboard as cb
        cb.kernel.GlobalAlloc.argtypes = [W.UINT, C.c_size_t]
        cb.kernel.GlobalAlloc.restype = W.HGLOBAL
        source = cb.kernel.GlobalAlloc(2, 64)
        self.assertTrue(source)
        duplicate = None
        try:
            original_text = C.create_unicode_buffer("元の文字")
            pointer = cb.kernel.GlobalLock(source)
            self.assertTrue(pointer)
            try:
                C.memmove(pointer, original_text, C.sizeof(original_text))
            finally:
                cb.kernel.GlobalUnlock(source)
            duplicate = cb.ole.OleDuplicateData(source, 13, 2)
            self.assertTrue(duplicate)
            pointer = cb.kernel.GlobalLock(source)
            try:
                C.memset(pointer, 0, 64)
            finally:
                cb.kernel.GlobalUnlock(source)
            pointer = cb.kernel.GlobalLock(duplicate)
            self.assertTrue(pointer)
            try:
                self.assertEqual(C.wstring_at(pointer), "元の文字")
            finally:
                cb.kernel.GlobalUnlock(duplicate)
        finally:
            cb.free_data(13, duplicate)
            cb.kernel.GlobalFree(source)

    def clipboard(self):
        from kana_rewriter.clipboard import Clipboard
        # Never touch the user's actual clipboard or create GUI windows in tests.
        instance = Clipboard.__new__(Clipboard)
        instance.hwnd = 123
        return instance

    def test_eager_snapshot_and_handle_ownership(self):
        from kana_rewriter import clipboard as cb
        instance = self.clipboard()
        with patch.object(cb.user, "OpenClipboard", return_value=1), \
                patch.object(cb.user, "CloseClipboard", return_value=1), \
                patch.object(cb.user, "EnumClipboardFormats", side_effect=[13, 8, 0]), \
                patch.object(cb.user, "GetClipboardData", side_effect=[101, 102]), \
                patch.object(cb.ole, "OleDuplicateData", side_effect=[201, 202]) as duplicate, \
                patch.object(cb.user, "GetClipboardSequenceNumber", return_value=10), \
                patch.object(cb.user, "EmptyClipboard", return_value=1), \
                patch.object(cb.user, "SetClipboardData", side_effect=[201, 202]) as put, \
                patch.object(cb, "free_data") as free:
            saved = instance.snapshot()
            self.assertEqual(saved.items, [(13, 201), (8, 202)])
            self.assertEqual(saved.sequence, 10)
            self.assertEqual(duplicate.call_args_list, [call(101, 13, 2), call(102, 8, 2)])
            self.assertTrue(instance.restore(saved, 10))
            self.assertEqual(put.call_args_list, [call(13, 201), call(8, 202)])
            saved.release()
            self.assertNotIn(call(13, 201), free.call_args_list)
            self.assertNotIn(call(8, 202), free.call_args_list)

    def test_open_clipboard_retries_temporary_contention(self):
        from kana_rewriter import clipboard as cb
        instance = self.clipboard()
        with patch.object(cb.user, "OpenClipboard", side_effect=[0, 0, 1]) as opened, \
                patch.object(cb.user, "CloseClipboard", return_value=1) as closed, \
                patch.object(cb, "pump_sent_messages") as pump, \
                patch.object(cb.time, "sleep"):
            with instance.opened():
                pass
            self.assertEqual(opened.call_count, 3)
            self.assertEqual(pump.call_count, 2)
            closed.assert_called_once()

    def test_read_tracks_sequence_after_delayed_rendering(self):
        from kana_rewriter import clipboard as cb
        instance = self.clipboard()
        buffer = C.create_unicode_buffer("きょう")
        with patch.object(cb.user, "OpenClipboard", return_value=1), \
                patch.object(cb.user, "CloseClipboard", return_value=1) as close, \
                patch.object(cb.user, "GetClipboardSequenceNumber", side_effect=[11, 12]), \
                patch.object(cb.user, "GetClipboardData", return_value=101), \
                patch.object(cb.kernel, "GlobalLock", return_value=C.addressof(buffer)), \
                patch.object(cb.kernel, "GlobalUnlock") as unlock:
            self.assertEqual(instance.read_changed_text(10), ("きょう", 12))
            unlock.assert_called_once_with(101)
            close.assert_called_once()

    def test_read_does_not_reuse_stale_text(self):
        from kana_rewriter import clipboard as cb
        instance = self.clipboard()
        with patch.object(cb.user, "OpenClipboard", return_value=1), \
                patch.object(cb.user, "CloseClipboard", return_value=1), \
                patch.object(cb.user, "GetClipboardSequenceNumber", return_value=10), \
                patch.object(cb.user, "GetClipboardData") as get:
            self.assertIsNone(instance.read_changed_text(10))
            get.assert_not_called()

    def test_new_clipboard_content_is_never_overwritten(self):
        from kana_rewriter import clipboard as cb
        instance = self.clipboard()
        saved = cb.Snapshot([(13, 201)], 10)
        with patch.object(cb.user, "OpenClipboard", return_value=1), \
                patch.object(cb.user, "CloseClipboard", return_value=1), \
                patch.object(cb.user, "GetClipboardSequenceNumber", return_value=99), \
                patch.object(cb.user, "EmptyClipboard") as empty, \
                patch.object(cb, "free_data") as free:
            self.assertFalse(instance.restore(saved, 12))
            empty.assert_not_called()
            saved.release()
            free.assert_called_once_with(13, 201)

    def test_failed_snapshot_releases_earlier_copies(self):
        from kana_rewriter import clipboard as cb
        with patch.object(cb.user, "OpenClipboard", return_value=1), \
                patch.object(cb.user, "CloseClipboard", return_value=1), \
                patch.object(cb.user, "EnumClipboardFormats", side_effect=[13, 8]), \
                patch.object(cb.user, "GetClipboardData", side_effect=[101, 102]), \
                patch.object(cb.ole, "OleDuplicateData", side_effect=[201, 0]), \
                patch.object(cb, "free_data") as free:
            with self.assertRaisesRegex(RuntimeError, "コピー前"):
                self.clipboard().snapshot()
            free.assert_called_once_with(13, 201)

    def test_empty_clipboard_is_restored_to_empty(self):
        from kana_rewriter import clipboard as cb
        with patch.object(cb.user, "OpenClipboard", return_value=1), \
                patch.object(cb.user, "CloseClipboard", return_value=1), \
                patch.object(cb.user, "EnumClipboardFormats", return_value=0), \
                patch.object(cb.user, "GetClipboardSequenceNumber", return_value=12), \
                patch.object(cb.user, "EmptyClipboard", return_value=1) as empty, \
                patch.object(cb.user, "SetClipboardData") as put:
            instance = self.clipboard()
            saved = instance.snapshot()
            self.assertEqual(saved.items, [])
            self.assertTrue(instance.restore(saved, 12))
            empty.assert_called_once()
            put.assert_not_called()

    def test_partial_restore_frees_only_untransferred_handles(self):
        from kana_rewriter import clipboard as cb
        saved = cb.Snapshot([(13, 201), (8, 202)], 10)
        with patch.object(cb.user, "OpenClipboard", return_value=1), \
                patch.object(cb.user, "CloseClipboard", return_value=1), \
                patch.object(cb.user, "GetClipboardSequenceNumber", return_value=12), \
                patch.object(cb.user, "EmptyClipboard", return_value=1), \
                patch.object(cb.user, "SetClipboardData", side_effect=[201, 0]), \
                patch.object(cb, "free_data") as free:
            with self.assertRaisesRegex(RuntimeError, "復元"):
                self.clipboard().restore(saved, 12)
            saved.release()
            self.assertNotIn(call(13, 201), free.call_args_list)
            self.assertIn(call(8, 202), free.call_args_list)

    def test_unsupported_private_format_aborts_before_modifying_clipboard(self):
        from kana_rewriter import clipboard as cb
        with patch.object(cb.user, "OpenClipboard", return_value=1), \
                patch.object(cb.user, "CloseClipboard", return_value=1), \
                patch.object(cb.user, "EnumClipboardFormats", return_value=0x200), \
                patch.object(cb.user, "EmptyClipboard") as empty:
            with self.assertRaisesRegex(RuntimeError, "独自形式"):
                self.clipboard().snapshot()
            empty.assert_not_called()

    def test_copy_selection_restores_snapshot_and_can_repeat(self):
        from kana_rewriter import windows as win
        clipboard = Mock()
        saved = Mock(sequence=10)
        clipboard.snapshot.return_value = saved
        clipboard.read_changed_text.side_effect = [None, ("きょう", 12), ("きょう", 15)]
        desktop = win.Desktop()
        desktop.clipboard = clipboard
        with patch.object(win, "focus", return_value="target"), \
                patch.object(win, "chord") as chord, \
                patch.object(win, "pump_sent_messages"), \
                patch.object(win.time, "sleep"):
            self.assertEqual(desktop.copy_selection(), "きょう")
            self.assertEqual(desktop.copy_selection(), "きょう")
            self.assertEqual(chord.call_count, 2)
        self.assertEqual(clipboard.restore.call_args_list, [call(saved, 12), call(saved, 15)])
        self.assertEqual(saved.release.call_count, 2)

    def test_failed_snapshot_never_sends_ctrl_c(self):
        from kana_rewriter import windows as win
        desktop = win.Desktop()
        desktop.clipboard = Mock()
        desktop.clipboard.snapshot.side_effect = RuntimeError("使用中")
        with patch.object(win, "focus", return_value="target"), patch.object(win, "chord") as chord:
            with self.assertRaises(RuntimeError):
                desktop.copy_selection()
            chord.assert_not_called()
