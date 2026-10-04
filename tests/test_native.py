import io
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from kana_rewriter.core import Config, apply_result
from kana_rewriter.native import Desktop, Reader, Writer, MAGIC, MAX_FRAME, read_frame

ROOT = Path(__file__).resolve().parents[1]
EXECUTABLE = ROOT / "build" / "native" / "kana-editor-worker.exe"


class NativeProtocolTests(unittest.TestCase):
    def test_unicode_offsets_and_frame_limits(self):
        writer = Writer()
        writer.number(2 ** 64 - 1)
        writer.text("前😀。散歩")
        frame = MAGIC + struct.pack("<I", len(writer.data)) + writer.data
        reader = Reader(read_frame(io.BytesIO(frame)))
        self.assertEqual(reader.number(), 2 ** 64 - 1)
        self.assertEqual(reader.text(), "前😀。散歩")
        reader.done()
        for bad in (frame[:-1], b"NOPE" + frame[4:], MAGIC + struct.pack("<I", MAX_FRAME + 1)):
            with self.subTest(frame=bad[:8]), self.assertRaises((ValueError, EOFError)):
                read_frame(io.BytesIO(bad))

    def test_configuration_and_relative_executable(self):
        for options in (dict(editor_worker="unknown"), dict(native_uia_wait="unknown"),
                        dict(native_worker_path=""), dict(editor_worker="native", edit_backend="clipboard")):
            with self.assertRaises(ValueError):
                Config(**options)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"
            path.write_text('editor_worker = "native"\nnative_worker_path = "worker.exe"\n', encoding="utf-8")
            self.assertEqual(Config.load(str(path)).native_worker_path, str(Path(directory) / "worker.exe"))

    def test_missing_executable_does_not_start_python_editor(self):
        desktop = Desktop(Config(editor_worker="native", native_worker_path="missing-worker.exe"))
        with patch("kana_rewriter.native.subprocess.Popen") as start:
            with self.assertRaisesRegex(RuntimeError, "ビルド"):
                desktop.warmup()
        start.assert_not_called()


class NativeAdapterTests(unittest.TestCase):
    def peer(self, mode):
        real_popen = subprocess.Popen
        processes = []

        def launch(*args, **kwargs):
            process = real_popen([sys.executable, "-B", "-m", "tests.native_fake_worker", mode],
                                 cwd=ROOT, **kwargs)
            processes.append(process)
            return process

        desktop = Desktop(Config(editor_worker="native", native_worker_path=str(sys.executable),
                                 editor_timeout_seconds=1))
        return desktop, patch("kana_rewriter.native.subprocess.Popen", side_effect=launch), processes

    def test_capture_apply_caret_mapping_and_token_not_replayed(self):
        desktop, peer, processes = self.peer("normal")
        with peer:
            try:
                desktop.warmup()
                capture = desktop.capture("line", 1000)
                self.assertEqual(capture.source, "さんぽ")
                self.assertEqual(capture.caret, 7)
                self.assertTrue(apply_result(desktop, capture, "散歩"))
                with self.assertRaisesRegex(RuntimeError, "無効"):
                    desktop.apply(capture, "散歩")
                self.assertEqual(desktop.inspect()["worker"], "native")
            finally:
                desktop.close()
        self.assertIsNotNone(processes[0].poll())
        self.assertFalse(desktop.reader_thread.is_alive())

    def test_corrupt_startup_or_capture_poison_worker(self):
        for mode in ("bad_header", "truncated_capture"):
            with self.subTest(mode=mode):
                desktop, peer, processes = self.peer(mode)
                with peer:
                    try:
                        with self.assertRaisesRegex(RuntimeError, "再送しません"):
                            desktop.capture("line", 1000)
                        self.assertTrue(desktop.failed)
                        with self.assertRaisesRegex(RuntimeError, "再起動"):
                            desktop.warmup()
                    finally:
                        desktop.close()
                self.assertIsNotNone(processes[0].poll())

    def test_startup_timeout_stops_child(self):
        desktop, peer, processes = self.peer("hang")
        with peer:
            try:
                with self.assertRaisesRegex(RuntimeError, "再送しません"):
                    desktop.warmup()
                self.assertTrue(desktop.failed)
                self.assertIsNotNone(processes[0].poll())
            finally:
                desktop.close()

    def test_apply_timeout_consumes_token_and_never_restarts(self):
        desktop, peer, processes = self.peer("apply_hang")
        with peer:
            try:
                capture = desktop.capture("line", 1000)
                before = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, "再送しません"):
                    desktop.apply(capture, "散歩")
                self.assertLess(time.monotonic() - before, 8)
                self.assertTrue(desktop.failed)
                self.assertIsNone(desktop.saved)
                with self.assertRaisesRegex(RuntimeError, "再起動"):
                    desktop.warmup()
                self.assertEqual(len(processes), 1)
            finally:
                desktop.close()

    def test_blocked_large_pipe_write_is_also_timed_out(self):
        desktop, peer, processes = self.peer("no_read_after_capture")
        with peer:
            try:
                capture = desktop.capture("line", 1000)
                before = time.monotonic()
                with self.assertRaisesRegex(RuntimeError, "再送しません"):
                    desktop.apply(capture, "あ" * 100000)
                self.assertLess(time.monotonic() - before, 8)
                self.assertTrue(desktop.failed)
                self.assertIsNotNone(processes[0].poll())
            finally:
                desktop.close()


@unittest.skipUnless(sys.platform == "win32" and EXECUTABLE.is_file(), "Build native worker first")
class NativeExecutableTests(unittest.TestCase):
    def test_real_worker_protocol_and_shutdown(self):
        for wait in ("poll", "event"):
            desktop = Desktop(Config(editor_worker="native", native_uia_wait=wait))
            try:
                self.assertTrue(desktop.warmup())
                process = desktop.process
                self.assertIsNone(process.poll())
                # No captured token: must reject without touching any editor.
                writer = Writer()
                for value in (1, 0, 1, 1):
                    writer.number(value)
                writer.text("散歩")
                with self.assertRaisesRegex(RuntimeError, "token"):
                    desktop._call(4, writer)
                self.assertTrue(desktop.warmup())
            finally:
                desktop.close()
            self.assertIsNotNone(process.poll())
            self.assertFalse(desktop.reader_thread.is_alive())

    def test_hidden_native_integration_fixtures(self):
        completed = subprocess.run([str(EXECUTABLE), "--self-test"], capture_output=True, text=True,
                                   timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("Native self-test passed", completed.stdout)
