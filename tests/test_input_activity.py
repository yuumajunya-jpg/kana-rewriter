import sys
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class InputActivityTests(unittest.TestCase):
    def test_motion_and_releases_are_forwarded_without_cancelling(self):
        from kana_rewriter.input_activity import InputActivity, user
        monitor = InputActivity()
        with patch.object(user, "CallNextHookEx", return_value=17) as forward:
            for _ in range(100):
                self.assertEqual(monitor._mouse(0, 0x0200, 0), 17)
            for message in (0x0202, 0x0205, 0x0208, 0x020C):
                monitor._mouse(0, message, 0)
            for message in (0x0101, 0x0105):
                monitor._keyboard(0, message, 0)
            self.assertEqual(monitor.counter, 0)
            self.assertEqual(forward.call_count, 106)

    def test_keys_clicks_and_wheels_change_sequence_even_between_reads(self):
        from kana_rewriter.input_activity import InputActivity, user
        monitor = InputActivity()
        with patch.object(user, "CallNextHookEx", return_value=0):
            for message in (0x0100, 0x0104):
                before = monitor.counter
                monitor._keyboard(0, message, 0)
                monitor._keyboard(0, message + 1, 0)
                self.assertEqual(monitor.counter, before + 1)
            for message in (0x0201, 0x0204, 0x0207, 0x020B, 0x020A, 0x020E):
                before = monitor.counter
                monitor._mouse(0, message, 0)
                monitor._mouse(0, 0x0200, 0)
                self.assertEqual(monitor.counter, before + 1)

    def test_negative_hook_code_is_forwarded_unchanged(self):
        from kana_rewriter.input_activity import InputActivity, user
        monitor = InputActivity()
        with patch.object(user, "CallNextHookEx", return_value=42) as forward:
            self.assertEqual(monitor._keyboard(-1, 0x0100, 123), 42)
            self.assertEqual(monitor._mouse(-1, 0x0201, 456), 42)
        self.assertEqual(monitor.counter, 0)
        self.assertEqual(forward.call_count, 2)

    def test_partial_hook_install_failure_cleans_up_and_fails_closed(self):
        from kana_rewriter.input_activity import InputActivity, user
        monitor = InputActivity()
        with patch.object(user, "SetWindowsHookExW", side_effect=[123, 0]), \
                patch.object(user, "UnhookWindowsHookEx") as remove:
            with self.assertRaisesRegex(RuntimeError, "入力監視"):
                monitor.start()
            monitor.close()
        remove.assert_called_once_with(123)

    def test_dead_monitor_cannot_return_stale_counter(self):
        from kana_rewriter.input_activity import InputActivity
        monitor = InputActivity()
        monitor.thread = Mock()
        monitor.thread.is_alive.return_value = False
        with self.assertRaisesRegex(RuntimeError, "入力監視"):
            monitor.snapshot()

    def test_real_hook_message_loop_starts_and_stops_without_sending_input(self):
        from kana_rewriter.input_activity import InputActivity
        monitor = InputActivity()
        try:
            monitor.start()
            self.assertIsInstance(monitor.snapshot(), int)
            self.assertTrue(monitor.thread.is_alive())
        finally:
            monitor.close()
        self.assertFalse(monitor.thread.is_alive())
