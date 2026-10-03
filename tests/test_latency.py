import ctypes
from concurrent.futures import Future
import sys
import unittest
from unittest.mock import Mock, patch

from kana_rewriter.core import Config
from kana_rewriter.editor import TextState, make_capture
from kana_rewriter.timing import stage


class TimingTests(unittest.TestCase):
    def test_failed_stage_is_timed_and_exception_is_preserved(self):
        with self.assertLogs("kana_rewriter.timing", level="DEBUG") as logs, \
                patch("kana_rewriter.timing.time.perf_counter", side_effect=[1, 1.025]):
            with self.assertRaisesRegex(ValueError, "failure"):
                with stage("範囲構築"):
                    raise ValueError("failure")
        self.assertIn("範囲構築=25.0ms (中止)", logs.output[0])

    def test_no_clock_reads_when_debug_disabled(self):
        with patch("kana_rewriter.timing.logger.isEnabledFor", return_value=False), \
                patch("kana_rewriter.timing.time.perf_counter") as clock:
            with stage("test"):
                pass
        clock.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class LatencyTests(unittest.TestCase):
    def test_capture_starts_with_hotkey_still_held(self):
        from kana_rewriter.direct import EditorEngine
        engine = EditorEngine(Config())
        editor = Mock(kind="uia")
        editor.read.return_value = TextState("さんぽ", 3, 3)
        with patch.object(engine, "editor", return_value=editor), \
                patch("kana_rewriter.direct_win32.window_identity", return_value=(1, 2, 3)), \
                patch("kana_rewriter.direct_win32.input_tick", return_value=10), \
                patch("kana_rewriter.direct_win32.user.GetAsyncKeyState",
                      side_effect=lambda key: 0x8000 if key in (0x11, 0x12, 0x4B) else 0), \
                patch("kana_rewriter.direct_win32.user.GetWindowThreadProcessId", return_value=101), \
                patch("kana_rewriter.direct_win32.kernel.GetCurrentThreadId", return_value=202), \
                patch("kana_rewriter.direct.time.sleep") as sleep:
            self.assertEqual(engine.capture("line", 1000, (0x4B,)).source, "さんぽ")
        self.assertEqual(engine.trigger_keys, (0x4B,))
        sleep.assert_not_called()

    def test_apply_waits_for_release_then_rechecks_document(self):
        from kana_rewriter.direct import EditorEngine
        from kana_rewriter import direct_win32 as win
        engine = EditorEngine(Config())
        state = TextState("さんぽ", 3, 3)
        captured = make_capture(state, ((1, 2, 3), 10), "line", Config(), 1)
        editor = Mock(kind="win32")
        engine.saved = (captured, editor, state)
        engine.trigger_keys = (0x4B,)
        held = True

        def release(_):
            nonlocal held
            editor.replace.assert_not_called()
            editor.read.assert_not_called()
            held = False

        editor.read.return_value = TextState("さんぽ！", 4, 4)
        with patch.object(win, "window_identity", return_value=(1, 2, 3)), \
                patch.object(win, "input_tick", return_value=10), \
                patch.object(win.user, "GetAsyncKeyState",
                             side_effect=lambda key: 0x8000 if held and key == 0x4B else 0), \
                patch.object(win.time, "sleep", side_effect=release):
            with self.assertRaisesRegex(RuntimeError, "本文または選択位置"):
                engine.apply(captured, "散歩")
        self.assertFalse(held)
        editor.replace.assert_not_called()
        self.assertIsNone(engine.saved)

    def test_release_wait_cancels_on_focus_change_or_timeout(self):
        from kana_rewriter import direct_win32 as win
        with patch.object(win, "window_identity", return_value=(4, 5, 6)):
            with self.assertRaisesRegex(RuntimeError, "入力先"):
                win.wait_input_release((1, 2, 3), (0x4B,))
        with patch.object(win, "window_identity", return_value=(1, 2, 3)), \
                patch.object(win.user, "GetAsyncKeyState", return_value=0x8000), \
                patch.object(win.time, "monotonic", side_effect=[0, 3]):
            with self.assertRaisesRegex(RuntimeError, "離してください"):
                win.wait_input_release((1, 2, 3), (0x4B,))

    def test_worker_warmup_does_not_need_focused_editor(self):
        from kana_rewriter.direct import Desktop, EditorEngine
        engine = EditorEngine(Config())
        with patch("kana_rewriter.direct_win32.input_tick"), \
                patch("kana_rewriter.direct_uia.Automation") as automation:
            engine.warmup()
            engine.warmup()
        automation.assert_called_once()
        automation.return_value.focused_editor.assert_not_called()
        desktop = Desktop(Config(edit_backend="win32"))
        try:
            self.assertTrue(desktop.warmup())
            process = desktop.process
            self.assertTrue(process.is_alive())
        finally:
            desktop.close()
        self.assertFalse(process.is_alive())

    def test_already_queued_completion_wakes_native_message_wait(self):
        from kana_rewriter.winapi import MessageWaiter, user, W
        waiter = MessageWaiter()
        future = Future()
        future.add_done_callback(waiter.wake)
        future.set_result(None)
        message = W.MSG()
        self.assertTrue(user.PeekMessageW(ctypes.byref(message), None,
                                          waiter.WAKE_MESSAGE, waiter.WAKE_MESSAGE, 0))
        self.assertEqual(waiter.wait(), 0)
        self.assertTrue(user.PeekMessageW(ctypes.byref(message), None,
                                          waiter.WAKE_MESSAGE, waiter.WAKE_MESSAGE, 1))

    def test_event_loop_applies_completion_and_logs_total_without_polling_sleep(self):
        from kana_rewriter.__main__ import run_windows
        from kana_rewriter import winapi as win
        future = Future()
        messages = iter((1, 0, 0))

        def next_message(pointer, *args):
            ident = next(messages)
            if ident:
                message = ctypes.cast(pointer, ctypes.POINTER(win.W.MSG)).contents
                message.message, message.wParam = 0x0312, ident
            return bool(ident)

        def finish():
            future.set_result(("散歩", 0.05))

        def stop():
            raise KeyboardInterrupt

        waits = iter((finish, stop))
        with patch.object(win, "MessageWaiter") as waiter, \
                patch.object(win.user, "RegisterHotKey", return_value=1), \
                patch.object(win.user, "UnregisterHotKey"), \
                patch.object(win.user, "PeekMessageW", side_effect=next_message), \
                patch("kana_rewriter.direct.Desktop") as desktop, \
                patch("kana_rewriter.__main__.ThreadPoolExecutor") as pool, \
                patch("kana_rewriter.__main__.apply_result", return_value=True) as apply, \
                patch("kana_rewriter.__main__.time.sleep") as sleep, \
                patch("kana_rewriter.__main__.time.perf_counter", side_effect=[1, 1.02, 1.08, 1.10]), \
                patch("builtins.print"), self.assertLogs("kana_rewriter.__main__", level="DEBUG") as logs:
            pool.return_value.submit.return_value = future
            waiter.return_value.wait.side_effect = lambda: next(waits)()
            self.assertEqual(run_windows(Mock(), Config()), 0)
        desktop.return_value.warmup.assert_called_once()
        waiter.return_value.wake.assert_called_once_with(future)
        apply.assert_called_once_with(desktop.return_value, desktop.return_value.capture.return_value, "散歩")
        sleep.assert_not_called()
        self.assertIn("AI以外=50.0ms", logs.output[0])
