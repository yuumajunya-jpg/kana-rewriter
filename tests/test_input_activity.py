import sys
import unittest
import ctypes as C
import queue
from unittest.mock import Mock, patch


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class InputActivityTests(unittest.TestCase):
    def test_only_own_injected_keys_are_excluded(self):
        from kana_rewriter.input_activity import InputActivity, KeyboardHookData, user
        from kana_rewriter.winapi import SELF_INPUT_TAG
        monitor = InputActivity()
        cases = [(0x10, SELF_INPUT_TAG, False), (0x11, SELF_INPUT_TAG, False),
                 (0x10, SELF_INPUT_TAG ^ 1, True), (0x10, 0, True),
                 (0, SELF_INPUT_TAG, True), (0, 0, True)]
        with patch.object(user, "CallNextHookEx", return_value=17) as forward:
            for flags, extra, counted in cases:
                for message in (0x0100, 0x0104):
                    before = monitor.counter
                    event = KeyboardHookData(0x25, 0, flags, 0, extra)
                    self.assertEqual(monitor._keyboard(0, message, C.addressof(event)), 17)
                    self.assertEqual(monitor.counter, before + int(counted))
            self.assertEqual(forward.call_count, len(cases) * 2)

    def test_real_sendinput_and_real_hook_preserve_external_activity(self):
        from kana_rewriter.input_activity import InputActivity, KeyboardHookData
        from kana_rewriter.winapi import user, send, KeyboardInput, Input, InputUnion, SELF_INPUT_TAG
        foreign_tag = SELF_INPUT_TAG ^ 1
        acknowledgements = queue.Queue()

        class Probe(InputActivity):
            def _keyboard(self, code, message, data):
                result = super()._keyboard(code, message, data)
                if code == 0 and data:
                    event = C.cast(data, C.POINTER(KeyboardHookData)).contents
                    if (event.flags & 0x10 and event.extra in (SELF_INPUT_TAG, foreign_tag)):
                        if event.vk == 0x87 and message == 0x0101:
                            acknowledgements.put((event.extra, self.counter))
                        # Tagged test events never reach any application.
                        # Physical/user input and all other injected input pass.
                        return 1
                return result

        monitor = Probe().start()
        try:
            from kana_rewriter.direct_win32 import unicode_events
            own_batch = [KeyboardInput(0x10, 0, 0, 0, 0)]
            own_batch.extend(KeyboardInput(0x25, 0, flags, 0, 0)
                             for _ in range(7) for flags in (1, 3))
            own_batch.append(KeyboardInput(0x10, 0, 2, 0, 0))
            own_batch.extend(unicode_events("変換テスト"))
            # An acknowledged final key-up proves the preceding real hook
            # callbacks ran, without a fixed sleep or a fake input_tick.
            own_batch.extend([KeyboardInput(0x87, 0, 0, 0, 0), KeyboardInput(0x87, 0, 2, 0, 0)])
            for _ in range(1000):
                before = monitor.snapshot()
                send(own_batch)
                tag, after = acknowledgements.get(timeout=2)
                self.assertEqual(tag, SELF_INPUT_TAG)
                self.assertEqual(after, before, "own SendInput was counted as user input")
                inputs = (Input * 2)(*(Input(1, InputUnion(keyboard=KeyboardInput(
                    0x87, 0, flags, 0, foreign_tag))) for flags in (0, 2)))
                self.assertEqual(user.SendInput(2, inputs, C.sizeof(Input)), 2)
                tag, after = acknowledgements.get(timeout=2)
                self.assertEqual(tag, foreign_tag)
                self.assertEqual(after, before + 1, "foreign input must still cancel an edit")
        finally:
            monitor.close()

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
