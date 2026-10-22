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
    def test_end_markers_preserved_across_positions_carets_and_refresh_cycles(self):
        setups = {'reset': ('', 'さんぽ', '散歩', ''),
                  'paragraph': ('前の行\r', 'さんぽ', '散歩', ''),
                  'paragraph2': ('前の行\r散歩', 'さんぽ', '散歩', ''),
                  'blankline': ('前の行\r\r', 'さんぽ', '散歩', ''),
                  'reported': ('a'*9, 'へんかんてすと', '変換テスト', '\r\n'),
                  'reportedfirst': ('', 'へんかんてすと', '変換テスト', '\n')}
        marker='\u200b\u200c'
        variants = (('insertmarkers',), ('insertmarkersbeforecaret',),
                    ('insertmarkers', 'removemarkers', 'insertmarkers'),
                    ('insertmarkers', 'insertmarkers'),
                    ('insertmarkerslf',), ('insertmarkerslfbeforecaret',))
        with EditFixture() as fixture:
            probe = NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe, lambda es: any(e['kind'] == 2 for e in es))
                for setup, (prefix, source, result, suffix) in setups.items():
                    for operations in variants:
                        for repetition in range(20):
                            with self.subTest(setup=setup, operations=operations, repetition=repetition):
                                fixture.command(setup); probe.drain()
                                fixture.command('begin'); fixture.command('commitcaret')
                                event = next(e for e in self.events(probe, lambda es: any(e['kind'] == 4 for e in es))
                                             if e['kind'] == 4)
                                self.assertEqual(event['text'], source)
                                for operation in operations: fixture.command(operation)
                                notices=probe.drain()
                                self.assertTrue(all(e['revision']==event['revision'] for e in notices))
                                reply=probe.replace(event,source,result)
                                self.assertTrue(reply['success'],reply)
                                count=2 if operations==('insertmarkers','insertmarkers') else 1
                                inserted=marker+'\n' if operations[0].startswith('insertmarkerslf') else marker*count
                                self.assertEqual(self.text(fixture),prefix+result+inserted+suffix)
            finally:
                self.assertTrue(probe.close())

    def test_source_changes_and_user_activity_do_not_write(self):
        with EditFixture() as fixture:
            probe = NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe, lambda es: any(e['kind']==2 for e in es))
                for operation in ('insertmarkersinside','insertlettersbefore','insertmarkerslfinside'):
                    fixture.command('reported'); probe.drain()
                    fixture.command('begin'); fixture.command('commitcaret')
                    event=next(e for e in self.events(probe,lambda es:any(e['kind']==4 for e in es)) if e['kind']==4)
                    fixture.command(operation)
                    reply=probe.replace(event,'へんかんてすと','書かない')
                    self.assertFalse(reply['success'],reply)
                    self.assertFalse(reply['mutated'])
                for operations in (('insertmarkers','begin'), ('moveleft','insertmarkers'),
                                   ('insertmarkersinside','insertmarkers')):
                    fixture.command('reported'); probe.drain()
                    fixture.command('begin'); fixture.command('commitcaret')
                    event=next(e for e in self.events(probe,lambda es:any(e['kind']==4 for e in es)) if e['kind']==4)
                    for operation in operations: fixture.command(operation)
                    reply=probe.replace(event,'へんかんてすと','書かない')
                    self.assertFalse(reply['success'],reply)
                    self.assertFalse(reply['mutated'])
                    if operations[-1]=='begin': fixture.command('cancel')
            finally:
                self.assertTrue(probe.close())

    def test_arbitrary_boundary_insertions_preserve_text_and_context(self):
        inserted = {'insertordinary':'続きの文章', 'insertordinarybeforecaret':'続きの文章',
                    'insertemoji':'\U0001f642\u2060\n', 'insertwrongmarkers':'\u200c\u200b',
                    'insertmarkersdoublelf':'\u200b\u200c\n\n', 'insertmarkerscr':'\u200b\u200c\r',
                    'insertlong':'追'*1100}
        with EditFixture() as fixture:
            probe=NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe,lambda es:any(e['kind']==2 for e in es))
                for setup in ('reportedfirst','reported'):
                    prefix,suffix=('', '\n') if setup=='reportedfirst' else ('a'*9,'\r\n')
                    for operation,addition in inserted.items():
                        for repetition in range(25):
                            with self.subTest(setup=setup,operation=operation,repetition=repetition):
                                event=self.caret_candidate(fixture,probe,setup)
                                self.assertEqual(probe.capture_context(event,64),(prefix,suffix))
                                fixture.command(operation)
                                reply=probe.replace(event,'へんかんてすと','変換テスト')
                                self.assertTrue(reply['success'],reply)
                                self.assertEqual(self.text(fixture),prefix+'変換テスト'+addition+suffix)
            finally:
                self.assertTrue(probe.close())

    def test_read_context_changes_reject_but_unreferenced_changes_continue(self):
        with EditFixture() as fixture:
            probe=NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe,lambda es:any(e['kind']==2 for e in es))
                for operation in ('replaceprefix','insertprefix','replacesuffix','deletesuffix',
                                  'replaceleftcontext','replacerightcontext'):
                    for repetition in range(25):
                        with self.subTest(operation=operation,repetition=repetition):
                            event=self.caret_candidate(fixture,probe,'contextcase')
                            self.assertEqual(probe.capture_context(event,3),('aaa','zzz'))
                            fixture.command(operation)
                            reply=probe.replace(event,'へんかんてすと','変換テスト')
                            expected_success=operation not in ('replaceleftcontext','replacerightcontext')
                            self.assertEqual(reply['success'],expected_success,reply)
                            self.assertEqual(reply['mutated'],expected_success)
                            if not expected_success:
                                self.assertEqual(reply['diagnostic']>>16,114)
                            else:
                                prefix='別'+'a'*(10 if operation=='insertprefix' else 9) if operation in ('replaceprefix','insertprefix') else 'a'*10
                                suffix='z'*10
                                if operation=='replacesuffix': suffix='z'*8+'別z'
                                if operation=='deletesuffix': suffix='z'*9
                                self.assertEqual(self.text(fixture),prefix+'変換テスト'+suffix)
            finally:
                self.assertTrue(probe.close())

    def test_native_context_handles_unicode_newlines_and_unused_right_side(self):
        with EditFixture() as fixture:
            probe=NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe,lambda es:any(e['kind']==2 for e in es))
                for characters,expected in ((0,('','')),(1,('\r\n','\U0001f642')),
                                            (2,('\U0001f642\r\n','\U0001f642\r\n'))):
                    event=self.caret_candidate(fixture,probe,'unicodecontext')
                    self.assertEqual(probe.capture_context(event,characters),expected)
                    self.assertTrue(probe.replace(event,event['text'],'変換テスト')['success'])
                event=self.caret_candidate(fixture,probe,'longcontext')
                with self.assertRaises(ValueError): probe.capture_context(event,1001)
                self.assertEqual(probe.capture_context(event,1000),('\U0001f642\r\n'*500,)*2)
                self.assertTrue(probe.replace(event,event['text'],'変換テスト')['success'])
                event=self.caret_candidate(fixture,probe,'contextcase')
                self.assertEqual(probe.capture_context(event,3,include_right=False),('aaa',''))
                fixture.command('replacerightcontext')
                self.assertTrue(probe.replace(event,event['text'],'変換テスト')['success'])
            finally:
                self.assertTrue(probe.close())

    def test_source_change_cannot_be_restored_by_later_refresh(self):
        with EditFixture() as fixture:
            probe=NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe,lambda es:any(e['kind']==2 for e in es))
                event=self.caret_candidate(fixture,probe,'reportedfirst')
                probe.capture_context(event,64)
                fixture.command('insertmarkersinside')
                fixture.command('refreshdocument')
                reply=probe.replace(event,event['text'],'書かない')
                self.assertFalse(reply['success'],reply)
                self.assertFalse(reply['mutated'])
            finally:
                self.assertTrue(probe.close())

    def caret_candidate(self,fixture,probe,setup):
        fixture.command(setup); probe.drain()
        fixture.command('begin'); fixture.command('commitcaret')
        return next(e for e in self.events(probe,lambda es:any(e['kind']==4 for e in es)) if e['kind']==4)

    def test_identical_text_refresh_preserves_exact_range_and_selection(self):
        setups = {'reset': '', 'paragraph': '前の行\r',
                  'paragraph2': '前の行\r散歩', 'blankline': '前の行\r\r'}
        with EditFixture() as fixture:
            probe = NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe, lambda es: any(e['kind'] == 2 for e in es))
                for setup, prefix in setups.items():
                    for operation in ('refreshtext', 'refreshdocument'):
                        for repetition in range(25):
                            with self.subTest(setup=setup, operation=operation, repetition=repetition):
                                fixture.command(setup); probe.drain()
                                fixture.command('begin'); fixture.command('commitcaret')
                                event = next(e for e in self.events(probe, lambda es: any(e['kind'] == 4 for e in es))
                                             if e['kind'] == 4)
                                fixture.command(operation)
                                reply = probe.replace(event, 'さんぽ', '散歩')
                                self.assertTrue(reply['success'], reply)
                                self.assertEqual(self.text(fixture), prefix + '散歩')
            finally:
                self.assertTrue(probe.close())

    def test_real_source_or_context_change_still_invalidates(self):
        with EditFixture() as fixture:
            probe=NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe,lambda es:any(e['kind']==2 for e in es))
                for operation,reason in (('insertmarkersinside',109),('changecontext',114)):
                    event=self.caret_candidate(fixture,probe,'paragraph')
                    probe.capture_context(event,64)
                    fixture.command(operation)
                    reply=probe.replace(event,'さんぽ','書かない')
                    self.assertFalse(reply['success'],reply)
                    self.assertFalse(reply['mutated'])
                    self.assertEqual(reply['diagnostic']>>16,reason)
            finally:
                self.assertTrue(probe.close())

    def test_prefix_boundary_insertions_follow_retained_source(self):
        with EditFixture() as fixture:
            probe=NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe,lambda es:any(e['kind']==2 for e in es))
                for operation,inserted in (('insertlinebefore','\r\n'),('insertlettersbefore','追加')):
                    for repetition in range(25):
                        with self.subTest(operation=operation,repetition=repetition):
                            event=self.caret_candidate(fixture,probe,'paragraph')
                            probe.capture_context(event,64)
                            fixture.command(operation)
                            reply=probe.replace(event,'さんぽ','散歩')
                            self.assertTrue(reply['success'],reply)
                            self.assertEqual(self.text(fixture),'前の行\r'+inserted+'散歩')
            finally:
                self.assertTrue(probe.close())

    def test_composition_metadata_refresh_preserves_exact_commit_at_paragraph_positions(self):
        setups = {'reset': '', 'paragraph': '前の行\r',
                  'paragraph2': '前の行\r散歩', 'blankline': '前の行\r\r'}
        with EditFixture() as fixture:
            probe = NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe, lambda es: any(e['kind'] == 2 for e in es))
                for setup, prefix in setups.items():
                    for repetition in range(25):
                        with self.subTest(setup=setup, repetition=repetition):
                            fixture.command(setup); probe.drain()
                            fixture.command('begin'); fixture.command('commitcaret')
                            event = next(e for e in self.events(probe, lambda es: any(e['kind'] == 4 for e in es))
                                         if e['kind'] == 4)
                            for operation in ('propertycycle', 'reselect', 'propertycycle'):
                                fixture.command(operation)
                            notices = probe.drain()
                            self.assertTrue(notices)
                            self.assertTrue(all(e['revision'] == event['revision'] for e in notices))
                            reply = probe.replace(event, 'さんぽ', '散歩')
                            self.assertTrue(reply['success'], reply)
                            self.assertEqual(self.text(fixture), prefix + '散歩')
            finally:
                self.assertTrue(probe.close())

    def test_metadata_refresh_cannot_restore_a_previously_invalidated_range(self):
        with EditFixture() as fixture:
            probe = NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe, lambda es: any(e['kind'] == 2 for e in es))
                for operation in ('movecaret', 'sametext'):
                    event = self.candidate(fixture, probe)
                    fixture.command('propertycycle')
                    fixture.command(operation)
                    fixture.command('propertycycle')
                    reply = probe.replace(event, 'さんぽ', '書かない')
                    self.assertFalse(reply['success'], reply)
                    self.assertFalse(reply['mutated'])
                    self.assertEqual(self.text(fixture), 'さんぽ')
            finally:
                self.assertTrue(probe.close())

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
                                         {'movecaret': 104, 'sametext': 104, 'reverse': 105}[operation])
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
                captured = editor.capture(prepared,64)
                self.assertEqual(captured.source,'さんぽ')
                with patch('kana_rewriter.direct_win32.window_identity', return_value=target):
                    self.assertTrue(editor.apply(prepared, captured, '散歩'))
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
                    self.assertEqual(probe.capture_context(event,64),('',''))
                    result = probe.replace(event, 'さんぽ', replacement)
                    durations.append((time.perf_counter() - started) * 1000)
                    self.assertTrue(result['success'], (i, result))
                    self.assertEqual(self.text(fixture), replacement)
                self.assertEqual(probe.dropped(), 0)
                import statistics
                print(f'TSF context + direct edit: 1000/1000, median={statistics.median(durations):.3f}ms, max={max(durations):.3f}ms')
            finally:
                self.assertTrue(probe.close())

    def test_capture_timeout_never_writes_and_refuses_reuse(self):
        import subprocess
        result=subprocess.run([sys.executable,'-X','utf8','-c',
            'from tests.test_tsf_edit import NativeEditTests; NativeEditTests().assert_capture_timeout_case()'],
            capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)

    def assert_capture_timeout_case(self):
        from kana_rewriter.tsf_edit import RestartRequired
        with EditFixture() as fixture:
            probe=NativeEditProbe(fixture.tid).start()
            try:
                self.events(probe,lambda es:any(e['kind']==2 for e in es))
                event=self.candidate(fixture,probe)
                fixture.command('stall')
                with self.assertRaises(RestartRequired):
                    probe.capture_context(event,64,timeout_ms=1)
                self.assertEqual(self.text(fixture),'さんぽ')
                with self.assertRaises(OSError):
                    probe.replace(event,'さんぽ','書かない')
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


@unittest.skipUnless(sys.platform=='win32','Windows DLL')
class NativeReplyTests(unittest.TestCase):
    def make_probe(self, hr=0):
        from unittest.mock import Mock, patch
        dll=Mock()
        dll.ProbeProtocolVersion.return_value=102
        def replace(*args):
            result=args[-1]._obj
            result.hr,result.state,result.mutated=hr,3,int(hr==0)
            return True
        dll.ReplaceCommit.side_effect=replace
        with patch('kana_rewriter.tsf_edit.C.WinDLL',return_value=dll):
            probe=NativeEditProbe(42,'test.dll')
        probe.started=True
        return probe,dll

    def test_success_does_not_fetch_or_format_failure_diagnostics(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from kana_rewriter.tsf_edit import apply_commit
        probe,dll=self.make_probe()
        with patch('kana_rewriter.tsf_edit.failure_reason') as reason:
            reply=apply_commit(probe,dict(commit_id=1,revision=2,text='かな'),SimpleNamespace(source='かな'),'仮名')
        self.assertTrue(reply['success'])
        self.assertEqual(reply['diagnostic'],0)
        self.assertIsNone(reply['text_diff'])
        dll.ReplaceCommit.assert_called_once()
        dll.LastEditDiagnostic.assert_not_called()
        dll.LastEditTextDiff.assert_not_called()
        dll.LastEditInsertedText.assert_not_called()
        reason.assert_not_called()

    def test_failure_keeps_range_and_inserted_units_in_message(self):
        from types import SimpleNamespace
        from kana_rewriter.tsf_edit import apply_commit
        probe,dll=self.make_probe(0x8007051a)
        dll.LastEditDiagnostic.return_value=2|(109<<16)
        def diff(pointer):
            for name,value in dict(before=8,after=11,prefix=7,suffix=1,source_equal=1).items():
                setattr(pointer._obj,name,value)
            return True
        dll.LastEditTextDiff.side_effect=diff
        def inserted(buffer,capacity):
            buffer[0],buffer[1],buffer[2]='\u200b','\u200c','\n'
            return 3
        dll.LastEditInsertedText.side_effect=inserted
        dll.LastEditOriginalUnits.return_value=7
        dll.LastEditOriginalStart.return_value=0
        with self.assertRaisesRegex(RuntimeError,'inserted_utf16=200b,200c,000a original_units=7 original_range=0:7'):
            apply_commit(probe,dict(commit_id=1,revision=2,text='へんかんてすと'),
                         SimpleNamespace(source='へんかんてすと'),'変換テスト')
        dll.ReplaceCommit.assert_called_once()
        dll.LastEditDiagnostic.assert_called_once()
        dll.LastEditTextDiff.assert_called_once()

    def test_incomplete_current_protocol_dll_is_rejected_before_start(self):
        from unittest.mock import Mock,patch
        dll=Mock()
        dll.ProbeProtocolVersion.return_value=102
        del dll.CaptureCommitContext
        with patch('kana_rewriter.tsf_edit.C.WinDLL',return_value=dll), self.assertRaisesRegex(RuntimeError,'機能が不足'):
            NativeEditProbe(42,'incomplete.dll')
        dll.StartProbe.assert_not_called()


class TrialRoutingTests(unittest.TestCase):
    @unittest.skipUnless(sys.platform=='win32','Windows DLL')
    def test_old_edit_protocol_is_rejected_before_starting(self):
        from unittest.mock import Mock,patch
        dll=Mock()
        dll.ProbeProtocolVersion.return_value=101
        with patch('kana_rewriter.tsf_edit.C.WinDLL',return_value=dll), self.assertRaisesRegex(RuntimeError,'一緒に更新'):
            NativeEditProbe(42,'old.dll')
        dll.StartProbe.assert_not_called()
        dll.PollProbe.assert_not_called()

    def test_unknown_write_stops_the_trial_and_closes_resources(self):
        self.assert_trial_cleanup()

    def test_trial_stops_executor_even_if_monitor_cleanup_fails(self):
        self.assert_trial_cleanup(close_error=RuntimeError('close failed'))

    def assert_trial_cleanup(self,close_error=None):
        from concurrent.futures import Future
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        from kana_rewriter.core import Config
        from kana_rewriter.tsf_edit_trial import run, RestartRequired
        event = dict(text='さんぽ', tick=12, commit_id=1, revision=2)
        probe = Mock(last_candidate=event)
        probe.capture_context.return_value = ('', '')
        probe.replace.return_value = dict(success=False, hr=0x800705B4, mutated=True, state=5)
        focus = SimpleNamespace(target=(1, 2, 3), runtime=(4,))
        commit = SimpleNamespace(text='さんぽ', tick=12, focus=focus)
        monitor = Mock()
        monitor.close.side_effect = close_error
        monitor.take.return_value = commit
        monitor.begin.return_value = monitor.valid.return_value = True
        def monitor_factory(config, wake, probe_factory):
            probe_factory(0)
            return monitor
        future = Future(); future.set_result('散歩')
        pool = Mock(); pool.submit.return_value = future
        args = SimpleNamespace(config='unused', fixed_source='さんぽ', fixed_result='散歩')
        with patch('kana_rewriter.tsf_edit_trial.Config.load', return_value=Config()), \
                patch('kana_rewriter.tsf_edit_trial.edit_probe_path', return_value=Path('unused.dll')), \
                patch('kana_rewriter.tsf_edit_trial.NativeEditProbe', return_value=probe), \
                patch('kana_rewriter.tsf_edit_trial.AutoImeMonitor', side_effect=monitor_factory), \
                patch('kana_rewriter.tsf_edit_trial.ThreadPoolExecutor', return_value=pool), \
                patch('kana_rewriter.direct.Desktop') as desktop_factory, \
                patch('kana_rewriter.direct_win32.window_identity', return_value=(1, 2, 3)), \
                patch('kana_rewriter.winapi.MessageWaiter'), \
                patch('kana_rewriter.winapi.user.PeekMessageW', return_value=0), patch('builtins.print'):
            with self.assertRaises(type(close_error) if close_error else RestartRequired):
                run(args)
        probe.replace.assert_called_once()
        monitor.close.assert_called_once()
        pool.shutdown.assert_called_once()
        desktop_factory.assert_not_called()

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
