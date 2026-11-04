import unittest
from unittest.mock import Mock, patch

from kana_rewriter.core import Config
from kana_rewriter.editor import TextState
from kana_rewriter.preview import (PreviewGate, preview_region, PreviewDisplay, preview_position,
                                  PreviewStream, stream_target)
from kana_rewriter.text import TextRegion


class PreviewTests(unittest.TestCase):
    def test_inference_error_is_reported_and_next_candidate_still_runs(self):
        import threading
        from kana_rewriter.preview import LivePreview
        preview = LivePreview.__new__(LivePreview)
        preview.stop = Mock()
        preview.stop.wait.side_effect = [False, False, False, False, True]
        preview.paused = threading.Event()
        preview.request_lock = threading.Lock()
        preview.cancel = None
        preview.pause_revision = 0
        preview.connection = Mock()
        preview.connection.poll.side_effect = [True, False, False, True, False, False]
        region = TextRegion('', 'ぷれびゅー', '')
        preview.connection.recv.side_effect = [(1, region), (2, region)]
        failed, successful = Mock(), Mock()
        failed.result.side_effect = RuntimeError('model failed')
        successful.result.return_value = 'プレビュー'
        preview.pool = Mock()
        preview.pool.submit.side_effect = [failed, successful]
        with self.assertLogs('kana_rewriter.preview', level='WARNING') as logs:
            preview._run()
        self.assertIn('プレビュー推論に失敗', logs.output[0])
        self.assertEqual([c.args for c in preview.connection.send.call_args_list],
                         [((1, ''),), ((2, 'プレビュー'),)])

    def test_preview_word_and_long_vowels_survive_every_input_prefix(self):
        for word in ('ぷれびゅー', 'プレビュー', 'こーひー', 'コーヒー', 'か\u3099'):
            for end in range(1, len(word) + 1):
                with self.subTest(word=word, end=end):
                    target = stream_target('前。', word[:end], '後', Config())
                    self.assertEqual(target.target, word[:end])
                    self.assertEqual(target.left, '前。')
        self.assertEqual(stream_target('コーヒー', 'ぷれびゅー', '', Config()).target, 'ぷれびゅー')

    def test_deleted_text_has_no_old_snapshot_and_retyping_resumes(self):
        from kana_rewriter.commit_state import Focus
        focus = Focus((1, 2, 3), (10,), 4)
        stream = PreviewStream()
        def edit(text):
            stream.observe(dict(kind=3, active=1, text=text, position_valid=1,
                                left=10, right=20, top=10, bottom=30))
        edit('さんぽ')
        original = stream.snapshot(focus, Config())
        self.assertIsNotNone(original)
        # Invalid/non-kana intermediate text must not restore deleted content.
        for text in ('', '。', '漢字'):
            edit(text)
            self.assertIsNone(stream.snapshot(focus, Config()))
        edit('さんぽ')
        changed = Focus(focus.target, (-109666,), focus.tid)
        self.assertEqual(stream.snapshot(changed, Config()), original)

    def test_child_exit_during_startup_releases_pipe_and_process(self):
        from kana_rewriter.preview import LivePreview
        context, parent, child = Mock(), Mock(), Mock()
        context.Pipe.return_value = parent, child
        parent.recv.side_effect = EOFError
        with patch('kana_rewriter.preview.multiprocessing.get_context', return_value=context):
            with self.assertRaises(EOFError):
                LivePreview(Config(), Mock(), Mock())
        parent.close.assert_called_once()
        context.Process.return_value.terminate.assert_called_once()
        context.Process.return_value.join.assert_called_once()

    def test_identical_text_in_new_session_rejects_previous_result(self):
        display = PreviewDisplay()
        display.observe(('same', None, 10, 10, 1, 'first'), 0)
        first = display.gate.generation
        display.observe(('same', None, 10, 10, 1, 'second'), 1)
        display.accept(first, '古い候補')
        self.assertIsNone(display.result)
        self.assertGreater(display.gate.generation, first)

    def test_window_stays_in_destination_monitor_work_area(self):
        from kana_rewriter.preview_window import place_preview
        self.assertEqual(place_preview(-10, 900, 400, 100, (-1920, 0, 0, 960)),
                         (-400, 776, 400, 100))
        self.assertEqual(place_preview(50, 0, 2000, 2000, (0, 0, 800, 600)),
                         (0, 0, 800, 600))

    def test_stream_survives_layout_failure_and_composition_identity_changes(self):
        stream = PreviewStream()
        def edit(text, active=1, valid=1, tick=1):
            return dict(kind=3, active=active, text=text, before='前。きょうは', after='後',
                        position_valid=valid, left=100, top=100, right=110, bottom=120,
                        composition_tick=tick)
        stream.observe(edit('いい'))
        epoch = stream.epoch
        stream.observe(edit('いいてんき', valid=0, tick=2))
        self.assertEqual(stream.position, (110, 120))
        self.assertEqual(stream.live, 'いいてんき')
        stream.observe(dict(kind=6))
        stream.observe(edit('', active=0))
        stream.observe(edit('いいてんきです', active=2, tick=3))
        self.assertEqual(stream.epoch, epoch)
        self.assertTrue(stream.active)
        self.assertEqual(stream.live, 'いいてんきです')
        stream.observe(dict(kind=4))
        self.assertFalse(stream.active)
        self.assertIsNone(stream.position)

    def test_stream_keeps_initial_x_and_tracks_y_until_session_ends(self):
        stream = PreviewStream()
        def edit(x, y, valid=1):
            stream.observe(dict(kind=3, active=1, text='さんぽ', position_valid=valid,
                                left=x - 10, right=x, top=y - 20, bottom=y))
        edit(100, 120, valid=0)
        self.assertIsNone(stream.position)
        edit(110, 120)
        edit(200, 120)
        self.assertEqual(stream.position, (110, 120))
        edit(20, 150)
        self.assertEqual(stream.position, (110, 150))
        edit(300, 200, valid=0)
        self.assertEqual(stream.position, (110, 150))
        for kind in (2, 4, 5, 7):
            stream.observe(dict(kind=kind))
            self.assertIsNone(stream.position)
            edit(250, 180)
            self.assertEqual(stream.position, (250, 180))
            edit(300, 210)
            self.assertEqual(stream.position, (250, 210))

    def test_full_target_and_preserved_parts_are_not_dropped(self):
        target = stream_target('前。きょうは', 'いいてんき。', '後の文', Config())
        self.assertEqual(target.target, 'きょうはいいてんき')
        self.assertEqual(target.left, '前。')
        self.assertEqual(target.right, '。後の文')
        self.assertEqual(target.render('今日は良い天気'), '今日は良い天気。')
        target = stream_target('前。', '私がさんぽ。', '', Config())
        self.assertEqual(target.target, 'がさんぽ')
        self.assertEqual(target.render('が散歩'), '私が散歩。')

    def test_auto_preview_only_converts_live_text_and_keeps_context(self):
        target = stream_target('前。きょうは', 'いいてんき。', '後の文',
                               Config(auto_replace_on_ime_commit=True))
        self.assertEqual(target.target, 'いいてんき。')
        self.assertEqual(target.left, '前。きょうは')
        self.assertEqual(target.right, '後の文')
        self.assertEqual(target.render('良い天気。'), '良い天気。')
        self.assertEqual(target.reading_text, 'いいてんき。')
        mixed = stream_target('確定済み', '私がさんぽ。', '後',
                              Config(auto_replace_on_ime_commit=True))
        self.assertEqual(mixed.target, '私がさんぽ。')
        self.assertEqual(mixed.render('私が散歩。'), '私が散歩。')
        for live in ('', '。', '漢字'):
            with self.subTest(live=live), self.assertRaises(ValueError):
                stream_target('きょう', live, '', Config(auto_replace_on_ime_commit=True))
        config = Config(auto_replace_on_ime_commit=True, max_chars=3)
        self.assertEqual(stream_target('あ' * 1000, 'さんぽ', '後', config).target, 'さんぽ')
        with self.assertRaises(ValueError):
            stream_target('', 'さんぽする', '', config)

    def test_reading_updates_before_inference_and_late_results_never_revert_it(self):
        display = PreviewDisplay()
        def observe(live):
            target = stream_target('前。', live, '', Config())
            display.observe((live, target, 100, 200, 1, 'session'), 0)
            return display.gate.generation
        first = observe('さん')
        self.assertIsNone(display.result)
        self.assertEqual(display.reading, 'さん')
        observe('さんぽ')
        self.assertEqual(display.reading, 'さんぽ')
        display.accept(first, '三')
        self.assertEqual(display.result, '三')
        self.assertEqual(display.reading, 'さんぽ')
        observe('さ')
        self.assertEqual(display.reading, 'さ')
        self.assertEqual(display.result, '三')
        display.observe(None, 1)
        self.assertEqual(display.reading, '')
        self.assertIsNone(display.result)

    def test_inference_slower_than_typing_still_publishes_completed_candidate(self):
        display = PreviewDisplay()
        target = stream_target('', 'さんぽ', '', Config())
        display.observe(('first', target, 100, 200, 1, 'input'), 0)
        first = display.gate.generation
        for index in range(10):
            display.observe((index, target, 100, 200, 1, 'input'), index + 1)
        display.accept(first, '散歩')
        self.assertEqual(display.result, '散歩')
        display.accept(display.gate.generation, '散歩する')
        display.accept(first, '古い候補')
        self.assertEqual(display.result, '散歩する')

    def test_reading_line_retains_identical_result_and_hides_absent_target(self):
        target = stream_target('', 'ありがとう', '', Config())
        display = PreviewDisplay()
        display.observe((target, target, 10, 20, 1, 's1'), 0)
        display.accept(display.gate.generation, 'ありがとう')
        self.assertEqual(display.result, 'ありがとう')
        self.assertEqual(display.reading, 'ありがとう')
        display.observe((target, target, 10, 20, 1, 's2'), 1)
        display.accept(display.gate.generation, 'ありがとう')
        self.assertEqual(display.reading, 'ありがとう')
        region = TextRegion('', 'さんぽ', '')
        display.observe((region, region, 10, 20, 1, 's3'), 2)
        display.accept(display.gate.generation, '散歩')
        self.assertEqual(display.result, '散歩')
        self.assertEqual(display.reading, '')
        display.observe(None, 3)
        self.assertEqual(display.reading, '')

    def test_reading_is_the_conversion_target_of_the_result(self):
        target = stream_target('前。', 'さんぽ', '後', Config())
        display = PreviewDisplay()
        display.observe(('key', target, 100, 200, 1, 'session'), 0)
        display.accept(display.gate.generation, '散歩')
        self.assertEqual(display.result, '散歩')
        self.assertEqual(display.reading, 'さんぽ')

    def test_uses_same_caret_boundaries_and_retains_punctuation(self):
        text = 'コーヒーをのむ。後ろ'
        caret = len('コーヒーをのむ。')
        self.assertEqual(preview_region(TextState(text, caret, caret), Config()),
                         TextRegion('コーヒー', 'をのむ', '。後ろ'))

    def test_selection_is_not_treated_as_caret(self):
        with self.assertRaises(ValueError):
            preview_region(TextState('きょう', 0, 3), Config())

    def test_no_target_and_length_limit(self):
        for text in ('漢字', 'カタカナ', 'あ' * 1001):
            with self.subTest(text=text[:10]), self.assertRaises(ValueError):
                preview_region(TextState(text, len(text), len(text)), Config())

    def test_immediate_request_and_discard_late_result(self):
        gate = PreviewGate()
        gate.observe('きょ', 0)
        self.assertTrue(gate.ready(0))
        first = gate.generation
        gate.observe('きょう', 0.2)
        self.assertTrue(gate.ready(0.2))
        self.assertTrue(gate.ready(0.6))
        self.assertFalse(gate.accepts(first))
        self.assertTrue(gate.accepts(gate.generation))
        gate.requested = True
        self.assertFalse(gate.ready(1))
        gate.observe(None, 1)
        self.assertFalse(gate.accepts(gate.generation))

    def test_result_is_retained_only_during_same_composition(self):
        display = PreviewDisplay()
        def snapshot(key, session):
            return (key, None, 100, 200, 1, session)
        display.observe(snapshot('a', 'session-1'), 0)
        display.accept(display.gate.generation, '散歩')
        display.observe(snapshot('b', 'session-1'), 1)
        self.assertEqual(display.result, '散歩')
        display.accept(display.gate.generation - 1, '古い結果')
        self.assertEqual(display.result, '散歩')
        display.accept(display.gate.generation, '')
        self.assertEqual(display.result, '散歩')
        display.observe(snapshot('a', 'session-2'), 2)
        self.assertIsNone(display.result)
        display.accept(display.gate.generation, '散歩')
        display.observe(None, 3)
        self.assertIsNone(display.result)

    def test_missing_or_empty_position_has_no_origin_fallback(self):
        self.assertIsNone(preview_position(dict(position_valid=0)))
        self.assertIsNone(preview_position(dict(position_valid=1,left=0,top=0,right=0,bottom=0)))
        self.assertEqual(preview_position(dict(position_valid=1,left=-400,top=100,right=-390,bottom=124)),
                         (-390, 124))

    def test_focus_and_caret_changes_invalidate_same_text(self):
        gate = PreviewGate()
        gate.observe(('field-a', 'きょう', 3), 0)
        original = gate.generation
        gate.observe(('field-b', 'きょう', 3), 1)
        self.assertFalse(gate.accepts(original))
        original = gate.generation
        gate.observe(('field-b', 'きょう', 2), 2)
        self.assertFalse(gate.accepts(original))
