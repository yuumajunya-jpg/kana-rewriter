import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts.diagnose_obsidian import evaluate, main


class ObsidianDiagnosisTests(unittest.TestCase):
    def test_evaluate_rejects_cli_error_even_with_zero_exit_code(self):
        with patch('scripts.diagnose_obsidian.subprocess.run', return_value=
                   subprocess.CompletedProcess([], 0, 'Error: no application', '')):
            with self.assertRaisesRegex(RuntimeError, 'no application'):
                evaluate('Obsidian.com', '0')

    def test_evaluate_reads_json_result(self):
        with patch('scripts.diagnose_obsidian.subprocess.run', return_value=
                   subprocess.CompletedProcess([], 0, '=> {"stopped":false}\n', '')):
            self.assertEqual(evaluate('Obsidian.com', '0'), {'stopped': False})

    def run_session(self, restart=False, tsf_edit=False):
        with tempfile.TemporaryDirectory() as directory:
            def observe(cli, code):
                if 'readFileSync' in code:
                    # Read the JSON path assigned by the launcher, not its JS source path.
                    assigned = code.split('window.__kanaSelectionTracePath=', 1)[1].split(';', 1)[0]
                    self.dom = json.loads(assigned)
                    return dict(path=self.dom, rendererPid=123, stopped=False, error=None)
                return None if restart else dict(path=self.dom, rendererPid=123, stopped=True,
                                                 error=None, dropped=0)
            with patch('scripts.diagnose_obsidian.shutil.which', return_value='Obsidian.com'), \
                    patch('scripts.diagnose_obsidian.evaluate', side_effect=observe) as eval_, \
                    patch('scripts.diagnose_obsidian.subprocess.call', return_value=0) as run, \
                    patch('builtins.print'):
                self.assertEqual(main(['--log-dir', directory] + (['--tsf-edit'] if tsf_edit else [])), 0)
            self.assertEqual(eval_.call_count, 2)
            self.assertIn('--diagnostic-log', run.call_args.args[0])
            self.assertEqual(run.call_args.args[0][2], 'kana_rewriter')
            self.assertEqual('--ime-commit-backend' in run.call_args.args[0], tsf_edit)
            session = json.loads(next(Path(directory).glob('*-session.json')).read_text(encoding='utf-8'))
            self.assertEqual(session['valid'], not restart)
            self.assertEqual(session['start']['path'], session['dom'])

    def test_verified_start_and_stop_share_one_session(self):
        self.run_session()

    def test_restart_invalidates_session(self):
        self.run_session(restart=True)

    def test_explicit_tsf_edit_routes_to_separate_module(self):
        self.run_session(tsf_edit=True)

    def test_failed_observer_start_does_not_launch_conversion(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch('scripts.diagnose_obsidian.shutil.which', return_value='Obsidian.com'), \
                patch('scripts.diagnose_obsidian.evaluate', side_effect=RuntimeError('not running')), \
                patch('scripts.diagnose_obsidian.subprocess.call') as run, patch('builtins.print'):
            self.assertEqual(main(['--log-dir', directory]), 1)
            run.assert_not_called()
