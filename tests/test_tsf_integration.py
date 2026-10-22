from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from kana_rewriter.core import Config
from kana_rewriter.auto_ime import CommitState
from kana_rewriter.tsf_edit import TsfCommitEditor
from tests.test_auto_ime import FIELD, commit_events


class IntegrationSettingsTests(unittest.TestCase):
    def test_paragraph_start_commit_matches_without_offset_guessing(self):
        from kana_rewriter.editor import TextState, make_capture
        for separator in ('\n', '\r\n', '\r', '\u2029'):
            for prefix in ('前の行', '😀前の行'):
                with self.subTest(separator=repr(separator), prefix=prefix):
                    left = prefix + separator
                    text = left + 'さんぽ'
                    captured = make_capture(TextState(text, len(text), len(text)),
                                            None, 'commit', Config(), 1, 'さんぽ')
                    self.assertEqual(captured.source, 'さんぽ')
                    self.assertEqual(captured.region.left, left)

    def test_cli_overrides_config_backend_for_normal_startup(self):
        from kana_rewriter.__main__ import main
        with patch('sys.argv', ['kana_rewriter', '--config', 'config.toml', '--ime-commit-backend', 'tsf']), \
                patch('kana_rewriter.__main__.Config.load', return_value=Config(backend='http')), \
                patch('kana_rewriter.__main__.Converter'), \
                patch('kana_rewriter.__main__.run_windows', return_value=0) as run:
            self.assertEqual(main(), 0)
        self.assertEqual(run.call_args.args[1].ime_commit_backend, 'tsf')

    def test_default_preserves_old_config_and_new_mode_validates(self):
        self.assertEqual(Config().ime_commit_backend, 'uia')
        self.assertEqual(Config(ime_commit_backend='tsf').ime_commit_backend, 'tsf')
        for value in (None, [], 'auto'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Config(ime_commit_backend=value)

    def test_relative_edit_dll_path_resolves_against_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.toml'
            path.write_text('ime_commit_backend="tsf"\ntsf_edit_path="edit.dll"', encoding='utf-8')
            config = Config.load(path)
            self.assertEqual(config.tsf_edit_path, str(Path(directory) / 'edit.dll'))

    def test_tsf_commit_preserves_immutable_native_identity(self):
        state = CommitState(); state.track(FIELD)
        events = commit_events()
        for event in events:
            if event['kind'] == 4:
                event.update(commit_id=7, revision=19)
        state.observe(events, 100)
        commit = state.take(100)
        self.assertEqual(commit.native_range, (7, 19, 'さんぽ'))
        editor = TsfCommitEditor('unused.dll')
        editor.probe = SimpleNamespace(started=True, tid=FIELD.tid)
        request = editor.prepare(commit)
        self.assertEqual(request.event, dict(commit_id=7, revision=19, text='さんぽ'))
        self.assertEqual(request.target, FIELD.target)
        editor.probe = object()  # the request remains bound to its original probe
        self.assertEqual(request.probe.tid, FIELD.tid)
        with self.assertRaises(RuntimeError):
            editor.prepare(replace(commit, native_range=None))

    def test_prepare_rejects_wrong_thread_or_text(self):
        editor = TsfCommitEditor('unused.dll')
        commit = SimpleNamespace(native_range=(7, 19, 'かな'), text='かな', focus=FIELD)
        editor.probe = SimpleNamespace(started=True, tid=FIELD.tid + 1)
        with self.assertRaises(RuntimeError): editor.prepare(commit)
        editor.probe.tid = FIELD.tid
        commit.text = '別の対象'
        with self.assertRaises(RuntimeError): editor.prepare(commit)
