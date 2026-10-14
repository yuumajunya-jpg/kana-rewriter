import ctypes
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from kana_rewriter.auto_ime import CommitState, Focus, AutoImeMonitor, tick_count
from kana_rewriter.core import Config
from kana_rewriter.tsf_probe import probe_path


FIELD = Focus((1, 2, 3), (10,), 4)


def event(kind, **values):
    return {**dict(kind=kind, pid=3, tid=4, hwnd=2, tick=100, key=13,
                   active=0, hr=0, generation=1, text="さんぽ"), **values}


def commit_events():
    return [event(8, key=83), event(3, active=1), event(8), event(3), event(4)]


class SettingsTests(unittest.TestCase):
    def test_opt_in_and_backend_validation(self):
        self.assertFalse(Config().auto_replace_on_ime_commit)
        Config(auto_replace_on_ime_commit=True)
        for options in ({"auto_replace_on_ime_commit": 1},
                        {"auto_replace_on_ime_commit": "true"},
                        {"auto_replace_on_ime_commit": True, "editor_worker": "native"},
                        {"auto_replace_on_ime_commit": True, "edit_backend": "clipboard"},
                        {"tsf_monitor_path": None}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                Config(**options)

    def test_settings_load_and_dll_path_resolves_relative_to_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('auto_replace_on_ime_commit = true\ntsf_monitor_path = "monitor.dll"\n', encoding="utf-8")
            config = Config.load(str(path))
            self.assertTrue(config.auto_replace_on_ime_commit)
            self.assertEqual(config.tsf_monitor_path, str(Path(directory) / "monitor.dll"))

    @unittest.skipUnless(sys.platform == "win32", "Windows")
    def test_cli_enable_and_disable_override_saved_setting(self):
        from kana_rewriter.__main__ import main
        for saved, switch in ((False, "--auto-replace-on-ime-commit"),
                              (True, "--no-auto-replace-on-ime-commit")):
            with patch.object(sys, "argv", ["kana_rewriter", "--config", "config.toml", switch,
                                             "--benchmark-source", "さんぽ", "--benchmark-result", "散歩"]), \
                    patch("kana_rewriter.__main__.Config.load", return_value=Config(auto_replace_on_ime_commit=saved)), \
                    patch("kana_rewriter.__main__.run_windows", return_value=0) as run, patch("builtins.print"):
                self.assertEqual(main(), 0)
            self.assertEqual(run.call_args.args[1].auto_replace_on_ime_commit, not saved)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.state = CommitState()
        self.state.track(FIELD)

    def test_commit_delivered_once_and_uncommitted_idle_does_not_trigger(self):
        self.state.observe(commit_events()[:-1], 100)
        self.assertIsNone(self.state.take(500))
        self.state.observe([event(4)], 500)
        commit = self.state.take(500)
        self.assertEqual(commit.text, "さんぽ")
        self.assertEqual(commit.focus, FIELD)
        self.assertIsNone(self.state.take(500))

    def test_cancellation_and_notice_from_previous_field_do_not_trigger(self):
        self.state.observe(commit_events()[:-1] + [event(5), event(4)], 100)
        self.assertIsNone(self.state.take(100))
        self.state.track(Focus((1, 2, 3), (20,), 4))
        self.state.observe([event(4)], 100)
        self.assertIsNone(self.state.take(100))

    def test_field_switch_invalidates_old_inference_even_on_same_hwnd(self):
        self.state.observe(commit_events(), 100)
        commit = self.state.take(100)
        self.assertTrue(self.state.begin(commit))
        self.assertTrue(self.state.valid(commit))
        self.state.track(Focus(FIELD.target, (99,), FIELD.tid))
        self.assertFalse(self.state.valid(commit))

    def test_editing_input_after_capture_invalidates_inference(self):
        for kind in (2, 3, 4, 5, 6, 7, 8, 9):
            state = CommitState()
            state.track(FIELD)
            state.observe(commit_events(), 100)
            commit = state.take(100)
            self.assertTrue(state.begin(commit))
            state.observe([event(kind)], 110)
            self.assertFalse(state.valid(commit))
            self.assertIsNone(state.take(110))

    def test_manual_action_discards_pending_auto_and_notifications_while_busy(self):
        self.state.observe(commit_events(), 100)
        self.state.begin()
        self.assertIsNone(self.state.take(100))
        self.state.observe(commit_events(), 110)
        self.state.finish()
        self.assertIsNone(self.state.take(110))
        self.state.observe([event(4)], 120)
        self.assertIsNone(self.state.take(120))

    def test_stale_non_enter_and_late_key_events_are_rejected(self):
        for events, now in ((commit_events(), 2000),
                            (commit_events()[:-1] + [{**event(4), "key": 0}], 100),
                            (commit_events() + [event(8)], 100)):
            self.state.observe(events, now)
            self.assertIsNone(self.state.take(now))

    def test_close_permanently_invalidates_pending_result(self):
        self.state.observe(commit_events(), 100)
        commit = self.state.take(100)
        self.state.close()
        self.assertFalse(self.state.valid(commit))
        self.assertFalse(self.state.begin())
        self.state.finish()
        self.state.observe(commit_events(), 100)
        self.assertIsNone(self.state.take(100))

    def test_old_field_packets_and_enter_alone_cannot_arm_a_new_field(self):
        self.state.track(Focus(FIELD.target, (99,), FIELD.tid), 500)
        self.state.observe(commit_events(), 600)
        self.assertIsNone(self.state.take(600))
        self.state.observe([event(8, tick=600), event(3, active=1, tick=600), event(4, tick=600)], 600)
        self.assertIsNone(self.state.take(600))


@unittest.skipUnless(sys.platform == "win32", "Windows")
class WatcherTests(unittest.TestCase):
    @unittest.skipUnless(probe_path().is_file(), "Build TSF DLL first")
    def test_real_observer_can_follow_two_processes_and_detach(self):
        from tests.test_tsf_probe import Fixture
        class Reader:
            current = None
            def read(self): return self.current
            def now(self): return tick_count()
        reader = Reader()
        with Fixture() as first, Fixture() as second, self.assertLogs("kana_rewriter.auto_ime", level="DEBUG") as logs:
            reader.current = Focus((first.hwnd, first.hwnd, first.pid), (1,), first.tid)
            monitor = AutoImeMonitor(Config(auto_replace_on_ime_commit=True), Mock(), reader_factory=lambda: reader).start()
            def wait_context(pid):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    if any(f"context pid={pid}" in record.getMessage() for record in logs.records[:]):
                        return
                    time.sleep(.01)
                self.fail(str([record.getMessage() for record in logs.records]))
            try:
                wait_context(first.pid)
                reader.current = Focus((second.hwnd, second.hwnd, second.pid), (2,), second.tid)
                wait_context(second.pid)
                self.assertIsNone(monitor.error)
            finally:
                monitor.close()
            self.assertFalse(monitor.thread.is_alive())

    def test_following_threads_reuses_dll_and_closes_old_subscription(self):
        class Reader:
            current = FIELD
            def read(self): return self.current
            def now(self): return tick_count()
        class Probe:
            tid = 0
            def __init__(self): self.starts, self.closes, self.events = [], [], []
            def start(self):
                self.starts.append(self.tid)
                self.events = [{**event(1), "tid": self.tid, "tick": tick_count()}]
                return self
            def drain(self):
                events, self.events = self.events, []
                return events
            def dropped(self): return 0
            def close(self): self.closes.append(self.tid); return True
        reader, probe, wake = Reader(), Probe(), Mock()
        factory = Mock(return_value=probe)
        monitor = AutoImeMonitor(Config(auto_replace_on_ime_commit=True), wake,
                                 reader_factory=lambda: reader, probe_factory=factory).start()
        def wait_until(predicate):
            deadline = time.monotonic() + 2
            while not predicate() and time.monotonic() < deadline: time.sleep(.01)
            self.assertTrue(predicate())
        try:
            wait_until(lambda: probe.starts == [4])
            reader.current = Focus(FIELD.target, (99,), 4)
            wait_until(lambda: monitor.state.focus == reader.current)
            self.assertEqual(probe.starts, [4])
            reader.current = Focus((8, 9, 10), (77,), 11)
            wait_until(lambda: probe.starts == [4, 11])
            self.assertIn(4, probe.closes)
        finally:
            monitor.close()
        self.assertFalse(monitor.thread.is_alive())
        self.assertEqual(factory.call_count, 1)
        self.assertIn(11, probe.closes)

    def test_missing_dll_fails_startup_without_monitor_thread_leak(self):
        monitor = AutoImeMonitor(Config(auto_replace_on_ime_commit=True), Mock(),
                                 probe_factory=Mock(side_effect=RuntimeError("missing DLL")))
        with self.assertRaisesRegex(RuntimeError, "missing DLL"):
            monitor.start()
        self.assertFalse(monitor.thread.is_alive())


@unittest.skipUnless(sys.platform == "win32", "Windows")
class NormalLoopTests(unittest.TestCase):
    def run_loop(self, commit=True, valid=True, manual=False):
        from kana_rewriter.__main__ import run_windows
        from kana_rewriter import winapi as win
        calls = 0
        def messages(pointer, *args):
            nonlocal calls
            calls += 1
            message = ctypes.cast(pointer, ctypes.POINTER(win.W.MSG)).contents
            if calls == 1 and manual:
                message.message, message.wParam = 0x0312, 1
                return 1
            if calls == 1 or calls == 2 and manual:
                return 0
            message.message, message.wParam = 0x0312, 3
            return 1
        state = CommitState()
        state.track(FIELD)
        state.observe(commit_events(), 100)
        notice = state.take(100)
        future = Mock()
        future.done.return_value = True
        future.result.return_value = ("散歩", .01)
        with patch.object(win.user, "RegisterHotKey", return_value=1), \
                patch.object(win.user, "UnregisterHotKey"), \
                patch.object(win.user, "PeekMessageW", side_effect=messages), \
                patch.object(win, "MessageWaiter"), \
                patch("kana_rewriter.auto_ime.AutoImeMonitor") as monitor, \
                patch("kana_rewriter.auto_ime.capture_commit", return_value="capture") as capture, \
                patch("kana_rewriter.direct.Desktop") as desktop, \
                patch("kana_rewriter.__main__.ThreadPoolExecutor") as pool, \
                patch("kana_rewriter.__main__.apply_result", return_value=True) as apply, patch("builtins.print"):
            monitor.return_value.take.return_value = notice if commit and not manual else None
            monitor.return_value.begin.return_value = True
            monitor.return_value.valid.side_effect = [True, valid]
            pool.return_value.submit.return_value = future
            self.assertEqual(run_windows(Mock(), Config(auto_replace_on_ime_commit=True, hotkey_line="F8",
                                                       hotkey_quit="Ctrl+Alt+Q")), 0)
            if manual:
                desktop.return_value.capture.assert_called_once()
                capture.assert_not_called()
                apply.assert_called_once()
            elif commit:
                capture.assert_called_once()
                self.assertEqual(apply.call_count, int(valid))
            monitor.return_value.close.assert_called_once()
            desktop.return_value.close.assert_called_once()

    def test_normal_startup_processes_commit_and_applies(self): self.run_loop()
    def test_input_during_inference_prevents_application(self): self.run_loop(valid=False)
    def test_manual_shortcut_can_be_used_with_auto_enabled(self): self.run_loop(manual=True)
