import ctypes as C
from concurrent.futures import Future
import sys
import unittest
from unittest.mock import Mock, patch


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class JapaneseKeyReleaseTests(unittest.TestCase):
    def event(self, monitor, message, vk=0xF3, scan=0x29, flags=0, device=1, raw_flags=None):
        from kana_rewriter.japanese_hotkey import RawPacket
        packet = RawPacket()
        packet.header.kind, packet.header.size, packet.header.device = 1, C.sizeof(packet), device
        packet.keyboard.scan, packet.keyboard.vk, packet.keyboard.message = scan, vk, message
        packet.keyboard.flags = flags | (1 if message in (0x0101, 0x0105) else 0)
        if raw_flags is not None:
            packet.keyboard.flags = raw_flags
        monitor.observe(packet)
        return packet

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

    def test_unrelated_extended_and_device_less_keys_cannot_release_held_key(self):
        monitor = self.prepare()
        self.event(monitor, 0x0100)
        for scan, flags in ((0x30, 0), (0x29, 2), (0x29, 4)):
            self.event(monitor, 0x0101, scan=scan, flags=flags)
            self.assertTrue(monitor.held)
        self.event(monitor, 0x0101, device=0)
        self.assertTrue(monitor.held)
        with patch("kana_rewriter.japanese_hotkey.time.monotonic", side_effect=[0, 3]):
            with self.assertRaisesRegex(RuntimeError, "離してください"):
                monitor.wait_released()

    def test_missing_physical_event_and_dead_monitor_fail_closed(self):
        monitor = self.prepare()
        with patch("kana_rewriter.japanese_hotkey.time.monotonic", side_effect=[0, 3]):
            with self.assertRaisesRegex(RuntimeError, "Raw Inputを確認"):
                monitor.wait_released()
        self.event(monitor, 0x0101)
        monitor.thread.is_alive.return_value = False
        with self.assertRaisesRegex(RuntimeError, "入力監視"):
            monitor.wait_released()

    def test_real_monitor_registers_raw_keyboard_without_hooks_and_closes(self):
        from kana_rewriter.japanese_hotkey import JapaneseKeyRelease, user
        monitor = JapaneseKeyRelease()
        try:
            with patch.object(user, "SetWindowsHookExW") as hook:
                monitor.start()
            hook.assert_not_called()
            self.assertEqual(monitor.snapshot(), 0)
            self.assertTrue(monitor.window)
        finally:
            monitor.close()
        self.assertFalse(monitor.thread.is_alive())
        self.assertIsNone(monitor.window)

    def test_release_from_another_keyboard_cannot_clear_held_key(self):
        monitor = self.prepare()
        self.event(monitor, 0x0100, device=1)
        self.event(monitor, 0x0101, device=2)
        self.assertTrue(monitor.held)
        self.event(monitor, 0x0101, device=1)
        monitor.wait_released()

    def test_raw_break_works_when_legacy_message_still_says_keydown(self):
        monitor = self.prepare()
        self.event(monitor, 0x0100)
        self.event(monitor, 0x0100, vk=0x19, flags=1)
        monitor.wait_released()
        self.assertEqual((monitor.makes, monitor.breaks), (1, 1))

    def test_reported_nls_keyup_with_zero_flags_is_release(self):
        monitor = self.prepare()
        # Exact user packet: SC029 / Flags=0 / VK_F4 / WM_KEYUP.
        # Two successive conversions must not leave a latched held state.
        for _ in range(2):
            self.event(monitor, 0x0101, vk=0xF4, device=131152, raw_flags=0)
            with patch("kana_rewriter.japanese_hotkey.poll_sleep") as sleep:
                monitor.wait_released()
            sleep.assert_not_called()
        self.assertEqual((monitor.makes, monitor.breaks, monitor.held), (0, 2, False))

    def test_system_keyup_with_zero_flags_releases_same_keyboard(self):
        monitor = self.prepare()
        self.event(monitor, 0x0104)
        self.assertTrue(monitor.held)
        self.event(monitor, 0x0105, vk=0xF4, raw_flags=0)
        monitor.wait_released()
        self.assertEqual((monitor.makes, monitor.breaks), (1, 1))

    def test_message_keyup_does_not_bypass_scan_device_or_extension_checks(self):
        monitor = self.prepare()
        self.event(monitor, 0x0100)
        for scan, flags, device in ((0x30, 0, 1), (0x29, 2, 1),
                                    (0x29, 4, 1), (0x29, 0, 0), (0x29, 0, 2)):
            self.event(monitor, 0x0101, scan=scan, device=device, raw_flags=flags)
            self.assertTrue(monitor.held)

    def test_break_scancode_high_bit_is_normalized(self):
        monitor = self.prepare()
        self.event(monitor, 0x0100)
        self.event(monitor, 0x0101, scan=0xA9, vk=0xF4)
        monitor.wait_released()
        self.assertEqual((monitor.makes, monitor.breaks), (1, 1))

    def test_empty_scan_recovers_only_mapped_half_width_key(self):
        from kana_rewriter.japanese_hotkey import user
        monitor = self.prepare()
        self.event(monitor, 0x0100)
        with patch.object(user, "MapVirtualKeyW", return_value=0x29) as mapping:
            self.event(monitor, 0x0101, scan=0, vk=0xF4)
        mapping.assert_called_once_with(0xF4, 4)
        monitor.wait_released()

    def test_unmapped_or_other_empty_scan_cannot_release_key(self):
        from kana_rewriter.japanese_hotkey import user
        monitor = self.prepare()
        self.event(monitor, 0x0100)
        with patch.object(user, "MapVirtualKeyW", return_value=0) as mapping:
            self.event(monitor, 0x0101, scan=0, vk=0xF4)
            self.event(monitor, 0x0101, scan=0, vk=0x41)
        mapping.assert_called_once_with(0xF4, 4)
        self.assertTrue(monitor.held)

    def test_extended_high_bit_or_other_break_cannot_release_key(self):
        monitor = self.prepare()
        self.event(monitor, 0x0100)
        for scan, flags, device in ((0xA9, 2, 1), (0xA9, 4, 1),
                                    (0xA9, 0, 2), (0xA9, 0, 0), (0xAA, 0, 1)):
            self.event(monitor, 0x0101, scan=scan, flags=flags, device=device)
            self.assertTrue(monitor.held)

    def test_raw_packet_is_read_in_two_calls_and_validated(self):
        from kana_rewriter.japanese_hotkey import user, W
        monitor = self.prepare()
        packet = self.event(monitor, 0x0100)
        packet.keyboard.flags = 1
        data = bytes(packet)
        def read(handle, command, buffer, size, header_size):
            C.cast(size, C.POINTER(W.UINT)).contents.value = len(data)
            if buffer is None:
                return 0
            C.memmove(buffer, data, len(data))
            return len(data)
        with patch.object(user, "GetRawInputData", side_effect=read) as get:
            monitor.read_raw(123)
        self.assertEqual(get.call_count, 2)
        monitor.wait_released()
        packet.header.size += 1
        data = bytes(packet)
        with patch.object(user, "GetRawInputData", side_effect=read):
            with self.assertRaisesRegex(RuntimeError, "不完全"):
                monitor.read_raw(123)

    def test_raw_read_failure_stops_monitor_and_still_cleans_up_message(self):
        from kana_rewriter.japanese_hotkey import user
        monitor = self.prepare()
        with patch.object(monitor, "read_raw", side_effect=RuntimeError("read failed")), \
                patch.object(user, "DefWindowProcW", return_value=17) as cleanup:
            self.assertEqual(monitor.window_proc(123, 0x00FF, 0, 456), 17)
        cleanup.assert_called_once_with(123, 0x00FF, 0, 456)
        with self.assertRaisesRegex(RuntimeError, "入力監視"):
            monitor.wait_released()

    def test_failed_raw_registration_destroys_window(self):
        from kana_rewriter.japanese_hotkey import JapaneseKeyRelease, user
        monitor = JapaneseKeyRelease()
        with patch.object(user, "RegisterRawInputDevices", return_value=0):
            with self.assertRaisesRegex(RuntimeError, "入力監視"):
                monitor.start()
        monitor.close()
        self.assertIsNone(monitor.window)
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
