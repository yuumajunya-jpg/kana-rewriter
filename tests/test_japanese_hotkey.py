import ctypes as C
from concurrent.futures import Future
import sys
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class JapaneseKeyReleaseTests(unittest.TestCase):
    def event(self, monitor, message, vk=0xF3, scan=0x29, flags=0):
        from kana_rewriter.japanese_hotkey import KeyboardEvent, user
        event = KeyboardEvent(vk, scan, flags, 0, 0)
        with patch.object(user, "CallNextHookEx", return_value=17):
            self.assertEqual(monitor._keyboard(0, message, C.addressof(event)), 17)

    def prepare(self):
        from kana_rewriter.japanese_hotkey import JapaneseKeyRelease
        monitor = JapaneseKeyRelease()
        monitor.thread = Mock()
        monitor.thread.is_alive.return_value = True
        return monitor

    def test_ime_vk_change_on_release_does_not_leave_physical_key_held(self):
        monitor = self.prepare()
        self.event(monitor, 0x0100, vk=0xF3)
        self.assertTrue(monitor.held)
        self.event(monitor, 0x0101, vk=0xF4)
        self.assertFalse(monitor.held)
        from kana_rewriter.japanese_hotkey import user
        with patch.object(user, "GetAsyncKeyState", return_value=0x8000) as async_state:
            monitor.wait_released()
        async_state.assert_not_called()

    def test_held_key_repeats_wait_until_physical_system_key_release(self):
        monitor = self.prepare()
        self.event(monitor, 0x0104)
        self.event(monitor, 0x0104)
        with patch("kana_rewriter.japanese_hotkey.poll_sleep",
                   side_effect=lambda seconds: self.event(monitor, 0x0105, vk=0x19)) as sleep:
            monitor.wait_released()
        sleep.assert_called_once_with(.005)
        self.assertFalse(monitor.held)

    def test_unrelated_extended_and_injected_keys_cannot_release_held_key(self):
        monitor = self.prepare()
        self.event(monitor, 0x0100)
        for scan, flags in ((0x30, 0), (0x29, 1), (0x29, 0x10)):
            self.event(monitor, 0x0101, scan=scan, flags=flags)
            self.assertTrue(monitor.held)
        with patch("kana_rewriter.japanese_hotkey.time.monotonic", side_effect=[0, 3]):
            with self.assertRaisesRegex(RuntimeError, "離してください"):
                monitor.wait_released()

    def test_missing_physical_event_and_dead_hook_fail_closed(self):
        monitor = self.prepare()
        with self.assertRaisesRegex(RuntimeError, "物理入力"):
            monitor.wait_released()
        self.event(monitor, 0x0101)
        monitor.thread.is_alive.return_value = False
        with self.assertRaisesRegex(RuntimeError, "入力監視"):
            monitor.wait_released()

    def test_real_monitor_installs_only_keyboard_hook_and_closes(self):
        from kana_rewriter.japanese_hotkey import JapaneseKeyRelease
        monitor = JapaneseKeyRelease()
        try:
            monitor.start()
            self.assertEqual(monitor.snapshot(), 0)
            self.assertEqual(monitor.hook_kinds, (13,))
        finally:
            monitor.close()
        self.assertFalse(monitor.thread.is_alive())

    def test_event_loop_waits_after_ai_before_edit_for_both_workers(self):
        from kana_rewriter.__main__ import run_windows
        from kana_rewriter.core import Config
        from kana_rewriter import winapi as win
        for worker in ("python", "native"):
            for release_fails in (False, True):
                with self.subTest(worker=worker, release_fails=release_fails):
                    future = Future()
                    messages = iter((11, 0, 0, 3))
                    order = []
                    def next_message(pointer, *args):
                        ident = next(messages)
                        if ident:
                            message = C.cast(pointer, C.POINTER(win.W.MSG)).contents
                            message.message, message.wParam = 0x0312, ident
                        return bool(ident)
                    def finish():
                        order.append("AI")
                        future.set_result(("散歩", .05))
                    def release():
                        order.append("release")
                        if release_fails:
                            raise RuntimeError("held")
                    with patch.object(win, "MessageWaiter") as waiter, \
                            patch.object(win.user, "RegisterHotKey", return_value=1), \
                            patch.object(win.user, "UnregisterHotKey"), \
                            patch.object(win.user, "PeekMessageW", side_effect=next_message), \
                            patch("kana_rewriter." + ("direct" if worker == "python" else "native") + ".Desktop") as desktop, \
                            patch("kana_rewriter.japanese_hotkey.JapaneseKeyRelease") as monitor, \
                            patch("kana_rewriter.__main__.ThreadPoolExecutor") as pool, \
                            patch("kana_rewriter.__main__.apply_result", side_effect=lambda *args: order.append("edit")) as apply, \
                            patch("builtins.print"):
                        waiter.return_value.wait.side_effect = lambda: finish() if not future.done() else None
                        pool.return_value.submit.return_value = future
                        monitor.return_value.start.return_value.wait_released.side_effect = release
                        self.assertEqual(run_windows(Mock(), Config(editor_worker=worker,
                            hotkey_line="半角全角", hotkey_quit="F8")), 0)
                    self.assertEqual(order, ["AI", "release"] + ([] if release_fails else ["edit"]))
                    self.assertEqual(apply.call_count, 0 if release_fails else 1)
                    desktop.return_value.capture.assert_called_once_with("line", 1000, trigger_keys=())
                    monitor.return_value.start.return_value.close.assert_called_once()
