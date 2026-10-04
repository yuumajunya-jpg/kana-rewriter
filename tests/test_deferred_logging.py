import logging
import pickle
import sys
import unittest
from unittest.mock import Mock, patch

from kana_rewriter.core import Config
from kana_rewriter.timing import DeferredHandler, buffered_logs, flush_logs


class DeferredLoggingTests(unittest.TestCase):
    def test_output_is_deferred_and_restored_even_on_failure(self):
        package = logging.getLogger("kana_rewriter")
        sink = Mock(spec=logging.Handler)
        sink.level = logging.DEBUG
        with patch.object(package, "handlers", [sink]), \
                patch.object(package, "propagate", False), \
                patch.object(package, "level", logging.DEBUG):
            with self.assertRaisesRegex(ValueError, "stop"):
                with buffered_logs():
                    package.warning("first %s", 1)
                    sink.handle.assert_not_called()
                    flush_logs()
                    self.assertEqual(sink.handle.call_args.args[0].getMessage(), "first 1")
                    package.warning("second")
                    self.assertEqual(sink.handle.call_count, 1)
                    raise ValueError("stop")
            self.assertEqual(package.handlers, [sink])
            self.assertFalse(package.propagate)
            self.assertEqual(sink.handle.call_count, 2)

    def test_worker_records_roundtrip_with_exception_and_timestamp(self):
        handler = DeferredHandler()
        try:
            raise RuntimeError("worker failure")
        except RuntimeError:
            record = logging.LogRecord("kana_rewriter.direct", logging.WARNING,
                                       "worker.py", 3, "failed: %s", (7,), sys.exc_info())
        handler.handle(record)
        packets = pickle.loads(pickle.dumps(handler.export()))
        received = logging.makeLogRecord(packets[0])
        self.assertEqual(received.created, record.created)
        self.assertEqual(received.getMessage(), "failed: 7")
        self.assertIn("worker failure", received.exc_text)
        self.assertIsNone(received.exc_info)
        self.assertEqual(handler.export(), [])

    def test_readback_delay_rejects_invalid_configuration(self):
        for value in [-1, 101, True, 0.5, "10"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                Config(uia_readback_initial_delay_ms=value)
        self.assertEqual(Config(uia_readback_initial_delay_ms=10).uia_readback_initial_delay_ms, 10)


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class WorkerDiagnosticsTests(unittest.TestCase):
    def test_worker_timing_packets_survive_real_process_transport(self):
        from kana_rewriter.direct import Desktop
        desktop = Desktop(Config(edit_backend="win32"), timings=True)
        try:
            with self.assertLogs("kana_rewriter", level="DEBUG") as logs:
                desktop.warmup()
            self.assertTrue(any("起動/編集環境初期化" in message for message in logs.output))
        finally:
            desktop.close()

    def test_failed_request_delivers_diagnostics_without_replaying_edit(self):
        from kana_rewriter.direct import Desktop
        handler = DeferredHandler()
        handler.handle(logging.LogRecord("kana_rewriter.direct", logging.WARNING,
                                         "worker.py", 1, "restore failed", (), None))
        desktop = Desktop(Config())
        desktop.process, desktop.connection = Mock(), Mock()
        desktop.connection.poll.return_value = True
        desktop.connection.recv.return_value = (False, "edit failed", handler.export())
        with self.assertLogs("kana_rewriter", level="WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "edit failed"):
                desktop.apply("capture", "散歩")
        self.assertIn("restore failed", logs.output[0])
        desktop.connection.send.assert_called_once_with(("apply", ("capture", "散歩")))

    def test_backend_validation_rejects_stale_state_before_selection(self):
        from kana_rewriter.direct_uia import AutomationEditor
        from kana_rewriter.editor import TextState
        editor = AutomationEditor.__new__(AutomationEditor)
        editor.window = (1, 2, 3)
        editor.check_focus = Mock()
        editor.check_writable = Mock()
        editor.confirm_expected = Mock(return_value="text_changed")
        editor.range_for = Mock()
        with patch("kana_rewriter.direct_uia.check_input_ready"), \
                patch("kana_rewriter.direct_uia.send") as send:
            with self.assertRaisesRegex(RuntimeError, "本文または選択位置"):
                editor.replace(TextState("さんぽ", 3, 3), 0, 3, "散歩", expected_tick=10)
        editor.range_for.assert_not_called()
        send.assert_not_called()

    def test_engine_delegates_validation_only_to_explicitly_opted_in_backend(self):
        from kana_rewriter.direct import EditorEngine
        from kana_rewriter.editor import TextState, make_capture
        engine = EditorEngine(Config())
        state = TextState("さんぽ", 3, 3)
        capture = make_capture(state, ((1, 2, 3), 1), "line", Config(), 1)
        editor = Mock(validates_before_replace=True)
        editor.replace.side_effect = RuntimeError("backend validation failed")
        engine.saved = capture, editor, state
        with patch("kana_rewriter.direct_win32.wait_input_release"), \
                patch("kana_rewriter.direct_win32.window_identity", return_value=(1, 2, 3)), \
                patch("kana_rewriter.direct_win32.input_tick", return_value=2):
            with self.assertRaisesRegex(RuntimeError, "backend validation failed"):
                engine.apply(capture, "散歩")
        editor.read.assert_not_called()
        editor.replace.assert_called_once()
