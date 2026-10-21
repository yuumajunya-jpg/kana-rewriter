import io
import logging
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from kana_rewriter.diagnostic_log import DiagnosticLog
from kana_rewriter.timing import buffered_logs


class DiagnosticLogTests(unittest.TestCase):
    def test_utf8_log_contains_deferred_records_prints_and_cancellation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "診断.log"
            original_out, original_err = sys.stdout, sys.stderr
            original_handlers = list(logging.getLogger().handlers)
            with patch("sys.stdout", io.StringIO()), patch("sys.stderr", io.StringIO()):
                with DiagnosticLog(path):
                    with buffered_logs():
                        logging.getLogger("kana_rewriter.test").debug("選択要求: %s:%s", 416, 423)
                    print("中止: UIAの選択が反映されませんでした", flush=True)
                    print("エラーも保存", file=sys.stderr)
                self.assertEqual(logging.getLogger().handlers, original_handlers)
            self.assertIs(sys.stdout, original_out)
            self.assertIs(sys.stderr, original_err)
            content = path.read_text(encoding="utf-8")
            self.assertEqual(content.count("選択要求: 416:423"), 1)
            self.assertIn("中止: UIAの選択が反映されませんでした", content)
            self.assertIn("エラーも保存", content)

    def test_worker_timestamp_and_process_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdout", io.StringIO()):
            path = Path(directory) / "worker.log"
            before = logging.getLogger().level
            with DiagnosticLog(path) as session:
                record = logging.makeLogRecord(dict(name="kana_rewriter.direct_uia", levelno=10,
                    levelname="DEBUG", msg="ワーカー診断", args=(), created=1.125, msecs=125,
                    process=777, processName="editor-worker", threadName="worker-thread"))
                session.handler.handle(record)
            content = path.read_text(encoding="utf-8")
            self.assertIn("editor-worker/777 worker-thread", content)
            self.assertIn(".125", content)
            self.assertEqual(logging.getLogger().level, before)

    def test_cli_diagnostic_option_enables_worker_debug_without_editing(self):
        from kana_rewriter.__main__ import main
        from kana_rewriter.core import Config
        with tempfile.TemporaryDirectory() as directory, patch("sys.stdout", io.StringIO()):
            path = Path(directory) / "cli.log"
            args = ["kana-rewriter", "--diagnostic-log", str(path)]
            with patch.object(sys, "argv", args), \
                    patch("kana_rewriter.__main__.logging.basicConfig"), \
                    patch("kana_rewriter.__main__.Config", return_value=Config()), \
                    patch("kana_rewriter.__main__.Converter"), \
                    patch("kana_rewriter.__main__.run_windows", return_value=0) as run:
                self.assertEqual(main(), 0)
            self.assertTrue(run.call_args.kwargs["debug"])
            self.assertIn("診断設定:", path.read_text(encoding="utf-8"))


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class SelectionDiagnosticTests(unittest.TestCase):
    def test_abort_diagnostics_do_not_select_send_or_mask_original_failure(self):
        from tests.test_direct import UiaRangeTests, Range
        import kana_rewriter.direct_uia as uia
        editor, model = UiaRangeTests().prepare()
        editor.timeout = 0
        state = editor.read()
        with patch.object(Range, "Select") as select, \
                patch.object(uia, "input_tick", return_value=10), \
                patch.object(uia, "window_identity", return_value=editor.window), \
                patch.object(uia, "check_input_ready"), patch.object(uia, "send") as send, \
                self.assertLogs(uia.logger, level="DEBUG") as logs:
            with self.assertRaisesRegex(RuntimeError, "選択が反映されません"):
                editor.replace(state, 5, 8, "散歩")
        select.assert_called_once()
        send.assert_not_called()
        self.assertTrue(any("UIA選択失敗詳細:" in line for line in logs.output))
        self.assertTrue(any("UIA選択観測: position_changed" in line for line in logs.output))

    def test_success_does_not_read_failure_metadata(self):
        from tests.test_direct import UiaRangeTests
        import kana_rewriter.direct_uia as uia
        editor, _ = UiaRangeTests().prepare()
        state = editor.read()
        with patch.object(uia, "input_tick", return_value=10), \
                patch.object(uia, "check_input_ready"), patch.object(uia, "send"), \
                patch.object(editor, "log_selection_failure") as details:
            editor.replace(state, 5, 8, "散歩")
        details.assert_not_called()
