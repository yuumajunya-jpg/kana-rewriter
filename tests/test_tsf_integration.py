from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
import sys
import threading
import time
from unittest.mock import patch

from kana_rewriter.core import Config
from kana_rewriter.auto_ime import CommitState
from kana_rewriter.tsf_edit import TsfCommitEditor
from tests.test_auto_ime import FIELD, commit_events


class RuntimeChurnIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform == 'win32', 'Windows TSF')
    def test_native_commit_survives_uia_round_trip_and_edits_retained_range(self):
        from tests.test_tsf_edit import EditFixture, NativeEditTests
        from kana_rewriter.auto_ime import AutoImeMonitor, Focus, tick_count
        from kana_rewriter.tsf import eligible
        if not EditFixture.executable.is_file():
            self.skipTest('Build TSF edit fixture first')
        pause, blocked, release = threading.Event(), threading.Event(), threading.Event()
        class Reader:
            current = None
            def read(self):
                if pause.is_set():
                    blocked.set()
                    if not release.wait(3):
                        raise RuntimeError('test focus gate timeout')
                return self.current
            def now(self):
                return tick_count()
        def wait_for(predicate):
            deadline = time.monotonic() + 3
            while not predicate() and time.monotonic() < deadline:
                time.sleep(.005)
            self.assertTrue(predicate())
        # The synthetic text store commits without a keyboard. Supply only the
        # Enter metadata; context, ranges, revisions and the write use real TSF.
        with patch('kana_rewriter.commit_state.eligible', side_effect=lambda e, now, target:
                   eligible({**e, 'key': 13}, now, target)), EditFixture() as fixture:
            reader = Reader()
            original = Focus((fixture.hwnd, 0, fixture.pid), (40389,), fixture.tid)
            reader.current = original
            editor = TsfCommitEditor()
            monitor = AutoImeMonitor(Config(), lambda: None,
                                     reader_factory=lambda: reader,
                                     probe_factory=editor.probe_factory).start()
            try:
                wait_for(lambda: monitor.state.context_generation is not None)
                pause.set()
                self.assertTrue(blocked.wait(2))
                fixture.command('begin')
                fixture.command('commitcaret')
                reader.current = Focus(original.target, (-109666,), original.tid)
                pause.clear()
                release.set()
                wait_for(lambda: monitor.state.pending is not None)
                commit = monitor.take()
                self.assertTrue(monitor.begin(commit))
                prepared = editor.prepare(commit)
                captured = editor.capture(prepared, 64)
                reader.current = original
                wait_for(lambda: monitor.state.focus == original)
                self.assertTrue(monitor.valid(commit))
                with patch('kana_rewriter.direct_win32.window_identity', return_value=original.target):
                    self.assertTrue(editor.apply(prepared, captured, '散歩'))
                self.assertEqual(NativeEditTests().text(fixture), '散歩')
                self.assertIsNone(monitor.error)
            finally:
                pause.clear()
                release.set()
                monitor.close()


class IntegrationSettingsTests(unittest.TestCase):
    def test_text_change_summary_is_included_in_abort_message(self):
        from unittest.mock import Mock
        from kana_rewriter.tsf_edit import apply_commit
        probe = Mock()
        probe.replace.return_value = dict(success=False, hr=0x8007051a, mutated=False,
                                         state=3, diagnostic=(109 << 16) | 2,
                                         text_diff=dict(before=7, after=8, prefix=0, suffix=7, source_equal=1))
        with self.assertRaisesRegex(RuntimeError, 'document_chars=7->8 changed_span=0:0->0:1 source_equal=True'):
            apply_commit(probe, dict(text='かな', commit_id=1, revision=2),
                         SimpleNamespace(source='かな'), '仮名')
        probe.replace.assert_called_once()

    def test_rejection_message_distinguishes_native_validation_and_invalidation(self):
        from unittest.mock import Mock
        from kana_rewriter.tsf_edit import apply_commit, diagnostic_reason
        probe = Mock()
        probe.replace.return_value = dict(success=False, hr=0x8007051a, mutated=False,
                                         state=3, diagnostic=(104 << 16) | 2)
        with self.assertRaisesRegex(RuntimeError, 'range_missing / invalidated_by=selection_moved'):
            apply_commit(probe, dict(text='かな', commit_id=1, revision=2),
                         SimpleNamespace(source='かな'), '仮名')
        probe.replace.assert_called_once()
        self.assertEqual(diagnostic_reason(0), 'unavailable')




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
