import ctypes
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import unittest
from unittest.mock import Mock, patch

from kana_rewriter.core import Config
from kana_rewriter.tsf import NativeProbe, eligible, PROTOCOL_VERSION


@unittest.skipUnless(sys.platform == "win32", "Windows DLL")
class ProtocolTests(unittest.TestCase):
    def test_old_or_incompatible_dll_is_rejected_before_polling(self):
        for version in (None, PROTOCOL_VERSION + 1):
            dll = Mock()
            dll.ProbeProtocolVersion = None if version is None else Mock(return_value=version)
            with self.subTest(version=version), patch("kana_rewriter.tsf.C.WinDLL", return_value=dll), \
                    patch.object(Path, "is_file", return_value=True), \
                    self.assertRaisesRegex(RuntimeError, "再ビルド"):
                NativeProbe(4, "old.dll")
            dll.PollProbe.assert_not_called()


class CommitPolicyTests(unittest.TestCase):
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
        event = dict(kind=4, key=13, pid=3, hwnd=2, tick=100, text="さんぽ", modifiers=0)
        self.assertEqual(eligible(event, 120, (1, 2, 3)), (True, "eligible"))
        self.assertEqual(eligible({**event, "modifiers": 1}, 120, (1, 2, 3)),
                         (False, "ctrl_confirm"))
        for updates, now, target in ((dict(key=0), 120, (1, 2, 3)),
                                     (dict(key=27), 120, (1, 2, 3)),
                                     ({}, 1700, (1, 2, 3)),
                                     ({}, 90, (1, 2, 3)),
                                     ({}, 120, (1, 9, 3)),
                                     (dict(text="散歩"), 120, (1, 2, 3)),
                                     (dict(kind=5), 120, (1, 2, 3))):
            with self.subTest(updates=updates, now=now, target=target):
                self.assertFalse(eligible({**event, **updates}, now, target)[0])

    def test_auto_conversion_skips_entire_commit_containing_kanji_or_katakana(self):
        event = dict(kind=4, key=13, pid=3, hwnd=2, tick=100, modifiers=0)
        for text in ('カタカナ', 'コーヒーをのむ', 'きょうはカフェにいく',
                     '今日はいいてんき', '散歩', 'ｶﾌｪにいく', '𠮷にいく', '々かな'):
            with self.subTest(text=text):
                self.assertEqual(eligible({**event, 'text': text}, 120, (1, 2, 3)),
                                 (False, 'already_converted'))
        for text in ('さんぽ', 'こーひー', 'か\u3099っこう', 'さんぽ。'):
            with self.subTest(text=text):
                self.assertEqual(eligible({**event, 'text': text}, 120, (1, 2, 3)),
                                 (True, 'eligible'))




class Fixture:
    def __enter__(self):
        exe = getattr(self, "executable", None) or (Path(__file__).resolve().parents[1] / "build/tsf-edit-probe/tsf-probe-fixture.exe")
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
