import ctypes as C
from pathlib import Path
import sys
import time
import unittest

from kana_rewriter.tsf_edit import NativeEditProbe, edit_probe_path
from tests.test_tsf_probe import Fixture


class EditFixture(Fixture):
    executable = Path(__file__).resolve().parents[1] / 'build/tsf-edit-probe/tsf-probe-fixture.exe'


@unittest.skipUnless(sys.platform == 'win32' and EditFixture.executable.is_file(), 'Build TSF editing experiment first')
class NativeEditTests(unittest.TestCase):
    def test_paragraph_positions_and_repeated_same_selection_keep_commit(self):
        setups = {'reset': '', 'paragraph': '前の行\r',
                  'paragraph2': '前の行\r散歩', 'blankline': '前の行\r\r'}
        with EditFixture() as fixture:
            probe = NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe, lambda es: any(e['kind'] == 2 for e in es))
                for setup, prefix in setups.items():
                    for repetition in range(25):
                        with self.subTest(setup=setup, repetition=repetition):
                            fixture.command(setup)
                            probe.drain()
                            fixture.command('begin')
                            # A real committed IME has an empty selection at its end.
                            fixture.command('commitcaret')
                            event = next(e for e in self.events(probe, lambda es: any(e['kind'] == 4 for e in es))
                                         if e['kind'] == 4)
                            fixture.command('reselect')
                            notices = probe.drain()
                            self.assertEqual(event['text'], 'さんぽ')
                            self.assertTrue(any(e['kind'] == 3 for e in notices))
                            self.assertTrue(all(e['revision'] == event['revision'] for e in notices))
                            reply = probe.replace(event, 'さんぽ', '散歩')
                            self.assertTrue(reply['success'], reply)
                            self.assertEqual(self.text(fixture), prefix + '散歩')
            finally:
                self.assertTrue(probe.close())

    def test_actual_selection_change_and_same_text_rewrite_still_invalidate(self):
        with EditFixture() as fixture:
            probe = NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe, lambda es: any(e['kind'] == 2 for e in es))
                for operation in ('movecaret', 'sametext', 'reverse'):
                    with self.subTest(operation=operation):
                        event = self.candidate(fixture, probe)
                        fixture.command(operation)
                        reply = probe.replace(event, 'さんぽ', '書かない')
                        self.assertFalse(reply['success'])
                        self.assertFalse(reply['mutated'])
                        self.assertEqual(self.text(fixture), 'さんぽ')
                        self.assertEqual(reply['diagnostic'] & 0xffff, 2)
                        self.assertEqual(reply['diagnostic'] >> 16,
                                         {'movecaret': 104, 'sametext': 103, 'reverse': 105}[operation])
                for operation in ('moveleft',):
                    with self.subTest(operation=operation, caret=True):
                        fixture.command('paragraph')
                        probe.drain()
                        fixture.command('begin')
                        fixture.command('commitcaret')
                        event = next(e for e in self.events(probe, lambda es: any(e['kind'] == 4 for e in es))
                                     if e['kind'] == 4)
                        fixture.command(operation)
                        reply = probe.replace(event, 'さんぽ', '書かない')
                        self.assertFalse(reply['success'])
                        self.assertFalse(reply['mutated'])
                        self.assertEqual(self.text(fixture), '前の行\rさんぽ')
                        self.assertEqual(reply['diagnostic'] >> 16, 104)
            finally:
                self.assertTrue(probe.close())

    def test_integrated_controller_edits_immutable_commit_range(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from kana_rewriter.auto_ime import Commit, Focus
        from kana_rewriter.tsf_edit import TsfCommitEditor
        with EditFixture() as fixture:
            editor = TsfCommitEditor()
            probe = editor.probe_factory(fixture.tid).start()
            try:
                self.events(probe, lambda es: any(e['kind'] == 2 for e in es))
                event = self.candidate(fixture, probe)
                target = (fixture.hwnd, fixture.hwnd, fixture.pid)
                commit = Commit('さんぽ', Focus(target, (), fixture.tid), event['tick'], 1,
                                (event['commit_id'], event['revision'], event['text']))
                prepared = editor.prepare(commit)
                with patch('kana_rewriter.direct_win32.window_identity', return_value=target):
                    self.assertTrue(editor.apply(prepared, SimpleNamespace(source='さんぽ'), '散歩'))
                self.assertEqual(self.text(fixture), '散歩')
            finally:
                self.assertTrue(probe.close())

    def events(self, probe, predicate):
        deadline = time.monotonic() + 3
        events = []
        while time.monotonic() < deadline:
            events += probe.drain()
            if predicate(events):
                return events
            time.sleep(.001)  # fixture notification collection, not the editing implementation
        self.fail('Missing native notifications: ' + str(events))

    def candidate(self, fixture, probe):
        fixture.command('reset')
        probe.drain()
        fixture.command('begin')
        fixture.command('commit')
        return next(e for e in self.events(probe, lambda es: any(e['kind'] == 4 for e in es)) if e['kind'] == 4)

    def text(self, fixture):
        from kana_rewriter.winapi import user
        buffer = C.create_unicode_buffer(2048)
        user.SendMessageTimeoutW(fixture.hwnd, 0x000D, len(buffer), C.addressof(buffer), 3, 1000, C.byref(C.c_size_t()))
        return buffer.value

    def test_direct_tsf_set_text_and_no_replay(self):
        with EditFixture() as fixture:
            probe = NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe, lambda es: any(e['kind'] == 2 and e['hr'] == 0 for e in es))
                event = self.candidate(fixture, probe)
                result = probe.replace(event, 'さんぽ', '散歩')
                self.assertTrue(result['success'], result)
                self.assertTrue(result['mutated'])
                self.assertEqual(self.text(fixture), '散歩')
                replay = probe.replace(event, 'さんぽ', '再送')
                self.assertFalse(replay['success'])
                self.assertFalse(replay['mutated'])
                self.assertEqual(self.text(fixture), '散歩')
            finally:
                self.assertTrue(probe.close())

    def test_mismatch_and_new_edit_do_not_write(self):
        with EditFixture() as fixture:
            probe = NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe, lambda es: any(e['kind'] == 2 for e in es))
                event = self.candidate(fixture, probe)
                result = probe.replace(event, '違う本文', '書かない')
                self.assertFalse(result['success'])
                self.assertFalse(result['mutated'])
                self.assertEqual(result['diagnostic'] & 0xffff, 10)
                self.assertEqual(self.text(fixture), 'さんぽ')
                event = self.candidate(fixture, probe)
                fixture.command('begin')
                result = probe.replace(event, 'さんぽ', '書かない')
                self.assertFalse(result['success'])
                self.assertFalse(result['mutated'])
                self.assertEqual(self.text(fixture), 'さんぽ')
                fixture.command('cancel')
            finally:
                self.assertTrue(probe.close())

    def test_asynchronous_tsf_lock_grant(self):
        with EditFixture() as fixture:
            probe = NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe, lambda es: any(e['kind'] == 2 for e in es))
                event = self.candidate(fixture, probe)
                fixture.command('async')
                reply = probe.replace(event, 'さんぽ', '非同期編集')
                self.assertTrue(reply['success'], reply)
                self.assertEqual(self.text(fixture), '非同期編集')
            finally:
                self.assertTrue(probe.close())

    def test_repeated_native_edit_sessions(self):
        with EditFixture() as fixture:
            probe = NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe, lambda es: any(e['kind'] == 2 for e in es))
                durations = []
                for i in range(1000):
                    event = self.candidate(fixture, probe)
                    replacement = ['散歩', '😀散歩', '散歩です。', ''][i % 4]
                    started = time.perf_counter()
                    result = probe.replace(event, 'さんぽ', replacement)
                    durations.append((time.perf_counter() - started) * 1000)
                    self.assertTrue(result['success'], (i, result))
                    self.assertEqual(self.text(fixture), replacement)
                self.assertEqual(probe.dropped(), 0)
                import statistics
                print(f'TSF direct edit: 1000/1000, median={statistics.median(durations):.3f}ms, max={max(durations):.3f}ms')
            finally:
                self.assertTrue(probe.close())

    def test_timeout_before_dispatch_never_writes_later(self):
        self.run_timeout_process(False)

    def test_in_flight_write_timeout_is_unknown_and_never_retried(self):
        self.run_timeout_process(True)

    def run_timeout_process(self, inside_write):
        # A timed-out DLL intentionally refuses reuse for the entire host
        # process. Isolate that test so unrelated test order cannot reset or
        # accidentally bypass the poisoned-host rule.
        import subprocess
        result = subprocess.run([sys.executable, '-X', 'utf8', '-c',
            'from tests.test_tsf_edit import NativeEditTests; NativeEditTests().assert_timeout_case('
            + str(inside_write) + ')'], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_timeout_case(self, inside_write=False):
        with EditFixture() as fixture:
            probe = NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe, lambda es: any(e['kind'] == 2 for e in es))
                event = self.candidate(fixture, probe)
                fixture.command('stallread' if inside_write else 'stall')
                reply = probe.replace(event, 'さんぽ', '結果不明時も再送しない', timeout_ms=100 if inside_write else 1)
                self.assertFalse(reply['success'])
                # TSF serves the explicit range read from its cache. The slow
                # ITextStore read is inside SetText, after writing has begun.
                self.assertEqual(reply['mutated'], inside_write)
                self.assertEqual(reply['state'], 5 if inside_write else 1)
                # Reading the target window waits until its blocked thread is
                # responsive again, so this also checks for a late posted edit.
                self.assertEqual(self.text(fixture), '結果不明時も再送しない' if inside_write else 'さんぽ')
                fixture.command('reset')
                self.assertEqual(self.text(fixture), 'さんぽ')
                with self.assertRaises(OSError):
                    probe.replace(event, 'さんぽ', '再送しない')
            finally:
                self.assertTrue(probe.close())


class TrialRoutingTests(unittest.TestCase):
    def test_unknown_write_stops_the_trial_and_closes_resources(self):
        from concurrent.futures import Future
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from kana_rewriter.core import Config
        from kana_rewriter.tsf_edit_trial import run, RestartRequired
        event = dict(text='さんぽ', tick=12, commit_id=1, revision=2)
        probe = Mock(last_candidate=event)
        probe.replace.return_value = dict(success=False, hr=0x800705B4, mutated=True, state=5)
        focus = SimpleNamespace(target=(1, 2, 3), runtime=(4,))
        commit = SimpleNamespace(text='さんぽ', tick=12, focus=focus)
        monitor = Mock()
        monitor.take.return_value = commit
        monitor.begin.return_value = monitor.valid.return_value = True
        def monitor_factory(config, wake, probe_factory):
            probe_factory(0)
            return monitor
        desktop = Mock()
        desktop.capture_commit.return_value = SimpleNamespace(source='さんぽ', left_context='', right_context='')
        future = Future(); future.set_result('散歩')
        pool = Mock(); pool.submit.return_value = future
        args = SimpleNamespace(config='unused', fixed_source='さんぽ', fixed_result='散歩')
        with patch('kana_rewriter.tsf_edit_trial.Config.load', return_value=Config()), \
                patch('kana_rewriter.tsf_edit_trial.edit_probe_path', return_value=Path('unused.dll')), \
                patch('kana_rewriter.tsf_edit_trial.NativeEditProbe', return_value=probe), \
                patch('kana_rewriter.tsf_edit_trial.AutoImeMonitor', side_effect=monitor_factory), \
                patch('kana_rewriter.tsf_edit_trial.ThreadPoolExecutor', return_value=pool), \
                patch('kana_rewriter.direct.Desktop', return_value=desktop), \
                patch('kana_rewriter.direct_win32.window_identity', return_value=(1, 2, 3)), \
                patch('kana_rewriter.winapi.MessageWaiter'), \
                patch('kana_rewriter.winapi.user.PeekMessageW', return_value=0), patch('builtins.print'):
            with self.assertRaises(RestartRequired):
                run(args)
        probe.replace.assert_called_once()
        monitor.close.assert_called_once()
        pool.shutdown.assert_called_once()
        desktop.close.assert_called_once()

    def test_changed_foreground_rejects_before_native_edit(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from kana_rewriter.tsf_edit_trial import apply_commit
        probe = Mock()
        with patch('kana_rewriter.direct_win32.window_identity', return_value=(9, 9, 9)):
            with self.assertRaisesRegex(RuntimeError, '入力先が変わった'):
                apply_commit(probe, dict(text='かな'), SimpleNamespace(source='かな'), '仮名', target=(1, 2, 3))
        probe.replace.assert_not_called()

    def test_trial_never_calls_uia_or_sends_keys(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from kana_rewriter.tsf_edit_trial import apply_commit
        event = dict(text='さんぽ', commit_id=1, revision=2)
        probe = Mock()
        probe.replace.return_value = dict(success=True, hr=0, mutated=True, state=3)
        captured = SimpleNamespace(source='さんぽ')
        with patch('kana_rewriter.direct_uia.AutomationEditor.replace') as uia, \
                patch('kana_rewriter.winapi.send') as send:
            self.assertTrue(apply_commit(probe, event, captured, '散歩')['success'])
        probe.replace.assert_called_once_with(event, 'さんぽ', '散歩')
        uia.assert_not_called()
        send.assert_not_called()

    def test_error_or_unknown_outcome_is_not_retried(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from kana_rewriter.tsf_edit_trial import apply_commit
        for mutated in (False, True):
            with self.subTest(mutated=mutated):
                probe = Mock()
                probe.replace.return_value = dict(success=False, hr=0x80004005, mutated=mutated, state=3)
                with self.assertRaisesRegex(RuntimeError, '再送はしません'):
                    apply_commit(probe, dict(text='かな', commit_id=1, revision=2),
                                 SimpleNamespace(source='かな'), '仮名')
                probe.replace.assert_called_once()
