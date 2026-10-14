import ctypes
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from kana_rewriter.core import Config, apply_result
from kana_rewriter.editor import TextState, make_capture, replacement_plan
from kana_rewriter.tsf_probe import NativeProbe, eligible, invalidates, probe_path


class TrialGateTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows worker")
    def test_worker_ctrl_c_closes_without_traceback(self):
        from kana_rewriter.direct import _worker
        connection = Mock()
        connection.recv.side_effect = KeyboardInterrupt
        with patch("kana_rewriter.direct.logging.basicConfig"), \
                patch("kana_rewriter.input_activity.close_monitor") as close_monitor, \
                patch("kana_rewriter.waiting.close_timer") as close_timer:
            _worker(connection, Config(), False)
        connection.close.assert_called_once()
        close_monitor.assert_called_once()
        close_timer.assert_called_once()
    def test_only_fresh_enter_commit_in_the_same_field_is_eligible(self):
        event = dict(kind=4, key=13, pid=3, hwnd=2, tick=100, text="さんぽ")
        self.assertEqual(eligible(event, 120, (1, 2, 3)), (True, "eligible"))
        for updates, now, target in ((dict(key=0), 120, (1, 2, 3)),
                                     (dict(key=27), 120, (1, 2, 3)),
                                     ({}, 1700, (1, 2, 3)),
                                     ({}, 90, (1, 2, 3)),
                                     ({}, 120, (1, 9, 3)),
                                     (dict(text="散歩"), 120, (1, 2, 3)),
                                     (dict(kind=5), 120, (1, 2, 3))):
            with self.subTest(updates=updates, now=now, target=target):
                self.assertFalse(eligible({**event, **updates}, now, target)[0])

    def test_new_composition_focus_or_keys_invalidate_inference(self):
        self.assertFalse(invalidates([]))
        for kind in (2, 3, 4, 5, 6, 7, 8, 9):
            self.assertTrue(invalidates([dict(kind=kind)]))

    def test_capture_is_exact_commit_and_preserves_surrounding_text(self):
        state = TextState("前のかな。さんぽ。後", 9, 9)
        capture = make_capture(state, (), "commit", Config(), 1, "さんぽ。")
        self.assertEqual(capture.source, "さんぽ。")
        self.assertEqual(replacement_plan(capture, "散歩。"), (5, 9, "前のかな。散歩。後", 8))
        with self.assertRaises(RuntimeError):
            make_capture(TextState(state.text, 8, 8), (), "commit", Config(), 1, "さんぽ。")


class Fixture:
    def __enter__(self):
        exe = Path(__file__).resolve().parents[1] / "build/tsf-probe/tsf-probe-fixture.exe"
        self.process = subprocess.Popen([str(exe)], stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.lines = queue.Queue()
        def read():
            for line in self.process.stdout:
                self.lines.put(line.strip())
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        try:
            ready = self.lines.get(timeout=5)
        except queue.Empty:
            self.__exit__(None, None, None)
            raise RuntimeError("TSF fixture startup timed out") from None
        if not ready.startswith("READY "):
            self.__exit__(None, None, None)
            raise RuntimeError(ready)
        _, pid, tid, hwnd = ready.split()
        self.pid, self.tid, self.hwnd = int(pid), int(tid), int(hwnd)
        return self

    def command(self, command):
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()
        result = self.lines.get(timeout=3)
        if result != "RESULT 0 0":
            raise RuntimeError(result)

    def __exit__(self, *args):
        if self.process.poll() is None:
            self.process.stdin.write("exit\n")
            self.process.stdin.flush()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.reader.join(timeout=1)
        self.process.stdin.close()
        self.process.stdout.close()
        self.process.stderr.close()


@unittest.skipUnless(sys.platform == "win32" and
                    probe_path().is_file(),
                    "Build 64-bit TSF probe first")
class NativeObserverTests(unittest.TestCase):
    def wait_events(self, probe, predicate):
        deadline, events = time.monotonic() + 3, []
        while time.monotonic() < deadline:
            events += probe.drain()
            if predicate(events):
                return events
            time.sleep(.01)
        self.fail(f"Missing expected notification: {events}")

    def test_repeated_attach_detach_and_duplicate_host_rejected(self):
        with Fixture() as fixture:
            for _ in range(2):
                probe = NativeProbe(fixture.tid).start()
                try:
                    self.wait_events(probe, lambda es: any(e["kind"] == 2 for e in es))
                    other = NativeProbe(fixture.tid)
                    with self.assertRaises(OSError):
                        other.start()
                    self.assertTrue(other.close())
                finally:
                    self.assertTrue(probe.close())
                self.assertTrue(probe.close())

    def test_capture_rejects_changed_uia_field_even_when_hwnd_is_same(self):
        from kana_rewriter.direct import EditorEngine
        from kana_rewriter import direct_win32 as win
        engine = EditorEngine(Config())
        with patch.object(win, "window_identity", return_value=(1, 2, 3)), \
                patch.object(engine, "focus_runtime", return_value=(9,)), \
                self.assertRaisesRegex(RuntimeError, "異なる入力欄"):
            engine.capture("commit", 1000, committed_text="さんぽ", expected_target=(1, 2, 3), expected_runtime=(8,))

    def test_cross_process_registration_actual_tsf_commit_and_cancel(self):
        for ending, expected_kind in (("commit", 4), ("cancel", 5)):
            with self.subTest(ending=ending), Fixture() as fixture:
                probe = NativeProbe(fixture.tid).start()
                try:
                    self.wait_events(probe, lambda es: any(e["kind"] == 2 and e["hr"] == 0 for e in es))
                    fixture.command("begin")
                    self.wait_events(probe, lambda es: any(e["kind"] == 3 and e["active"] == 1 for e in es))
                    time.sleep(.1)  # an idle composition must not become a commit
                    self.assertFalse(any(e["kind"] == 4 for e in probe.drain()))
                    fixture.command(ending)
                    events = self.wait_events(probe, lambda es: any(e["kind"] == expected_kind for e in es))
                    self.assertEqual(probe.dropped(), 0)
                    if ending == "commit":
                        event = next(e for e in events if e["kind"] == 4)
                        self.assertEqual(event["text"], "さんぽ")
                        self.assertEqual(event["pid"], fixture.pid)
                    else:
                        self.assertFalse(any(e["kind"] == 4 for e in events))
                finally:
                    self.assertTrue(probe.close())

    def test_synthetic_tsf_notification_to_real_cross_process_replacement(self):
        from kana_rewriter.direct import EditorEngine
        from kana_rewriter.__main__ import FixedConverter
        from kana_rewriter import direct_win32 as win
        with Fixture() as fixture:
            probe = NativeProbe(fixture.tid).start()
            try:
                self.wait_events(probe, lambda es: any(e["kind"] == 2 for e in es))
                fixture.command("begin")
                self.wait_events(probe, lambda es: any(e["kind"] == 3 and e["active"] == 1 for e in es))
                fixture.command("commit")
                events = self.wait_events(probe, lambda es: any(e["kind"] == 4 for e in es))
                event = next(e for e in events if e["kind"] == 4)
                target = (fixture.hwnd, fixture.hwnd, fixture.pid)
                with patch.object(win, "window_identity", return_value=target), \
                        patch.object(win, "input_tick", return_value=10), \
                        patch.object(win.user, "GetAsyncKeyState", return_value=0):
                    engine = EditorEngine(Config(edit_backend="win32"))
                    captured = engine.capture("commit", 1000, committed_text=event["text"], expected_target=target)
                    result = FixedConverter("さんぽ", "散歩").convert_selection(captured.source, captured.left_context, captured.right_context)
                    self.assertTrue(apply_result(engine, captured, result))
                    self.assertEqual(win.NativeEditor(fixture.hwnd, Config()).read(), TextState("散歩", 2, 2))
                # Focus/input guards are simulated because this fixture is invisible.
                # TSF callbacks, DLL injection and edit messages are real.
            finally:
                self.assertTrue(probe.close())
