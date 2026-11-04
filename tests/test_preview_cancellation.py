"""Preview preemption without desktop input or a model dependency."""
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from kana_rewriter.core import Config, Converter, ConversionCancelled
from kana_rewriter.preview import LivePreview
from kana_rewriter.text import TextRegion


def preview_fixture(converter, pool):
    preview = LivePreview.__new__(LivePreview)
    preview.converter, preview.pool = converter, pool
    preview.stop = threading.Event()
    preview.paused = threading.Event()
    preview.request_lock = threading.Lock()
    preview.cancel = None
    preview.pause_revision = 0
    return preview


class ConversionCancellationTests(unittest.TestCase):
    def test_cancelled_before_start_never_loads_model(self):
        converter = Converter(Config())
        cancel = threading.Event()
        cancel.set()
        with patch.object(converter, 'load_model') as load:
            with self.assertRaises(ConversionCancelled):
                converter.convert_selection('さんぽ', cancel=cancel)
        load.assert_not_called()

    def test_partial_stop_is_discarded_and_next_conversion_has_no_cancel(self):
        for model_format in ('zenz_v3_2', 'jinen_v1', 'jinen_v2', 'chat'):
            with self.subTest(model_format=model_format):
                converter = Converter(Config(model_format=model_format))
                converter.llm = Mock()
                converter.llm.tokenize.return_value = [1, 2]
                cancel = threading.Event()
                def generate(**request):
                    if 'stopping_criteria' in request:
                        check = request['stopping_criteria'][0]
                        self.assertFalse(check(None, None))
                        cancel.set()
                        self.assertTrue(check(None, None))
                    return {'choices': [{'text': '散歩', 'message': {'content': '散歩'},
                                         'finish_reason': 'stop'}]}
                converter.llm.create_completion.side_effect = generate
                converter.llm.create_chat_completion.side_effect = generate
                with patch.dict('sys.modules', {'llama_cpp': SimpleNamespace(StoppingCriteriaList=list)}):
                    with self.assertRaises(ConversionCancelled):
                        converter.convert_selection('さんぽ', cancel=cancel)
                    self.assertEqual(converter.convert_selection('さんぽ'), '散歩')
                call = (converter.llm.create_chat_completion if model_format == 'chat'
                        else converter.llm.create_completion)
                self.assertNotIn('stopping_criteria', call.call_args.kwargs)

    def test_cancel_between_spans_does_not_start_next_span(self):
        converter = Converter(Config())
        cancel = threading.Event()
        def first(*args, **kwargs):
            cancel.set()
            return '散歩'
        with patch.object(converter, 'convert', side_effect=first) as convert:
            with self.assertRaises(ConversionCancelled):
                converter.convert_selection('さんぽ、てんき', cancel=cancel)
        convert.assert_called_once()


class PreviewPreemptionTests(unittest.TestCase):
    def test_running_preview_yields_to_commit_and_resumes_with_fresh_token(self):
        started = threading.Event()
        tokens = []
        order = []
        def convert(source, left, right, *, cancel):
            tokens.append(cancel)
            if source == 'さん':
                started.set()
                if not cancel.wait(3):
                    raise AssertionError('Preview never received cancellation')
                order.append('cancelled')
                raise ConversionCancelled()
            self.assertFalse(cancel.is_set())
            order.append('new-preview')
            return '散歩'
        converter = Mock()
        converter.convert_selection.side_effect = convert
        parent, child = multiprocessing.Pipe()
        with ThreadPoolExecutor(max_workers=1) as pool:
            preview = preview_fixture(converter, pool)
            preview.connection = parent
            thread = threading.Thread(target=preview._run)
            thread.start()
            try:
                child.send((1, TextRegion('', 'さん', '')))
                self.assertTrue(started.wait(3))
                preview.pause()
                commit = pool.submit(lambda: order.append('commit'))
                commit.result(timeout=3)
                self.assertFalse(child.poll(.05))
                self.assertEqual(order, ['cancelled', 'commit'])
                preview.resume()
                child.send((2, TextRegion('', 'さんぽ', '')))
                self.assertTrue(child.poll(3))
                self.assertEqual(child.recv(), (2, '散歩'))
                self.assertIsNot(tokens[0], tokens[1])
                self.assertEqual(order, ['cancelled', 'commit', 'new-preview'])
            finally:
                preview.pause()
                preview.stop.set()
                thread.join(timeout=3)
                parent.close()
                child.close()
            self.assertFalse(thread.is_alive())

    def test_queued_preview_is_skipped_before_commit(self):
        release = threading.Event()
        submitted = threading.Event()
        converter = Mock()
        parent, child = multiprocessing.Pipe()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(release.wait, 3)
            preview = preview_fixture(converter, pool)
            preview.connection = parent
            submit = pool.submit
            def record(*args, **kwargs):
                future = submit(*args, **kwargs)
                if args[0] == preview._convert:
                    submitted.set()
                return future
            with patch.object(pool, 'submit', side_effect=record):
                thread = threading.Thread(target=preview._run)
                thread.start()
                try:
                    child.send((1, TextRegion('', 'さんぽ', '')))
                    self.assertTrue(submitted.wait(3))
                    preview.pause()
                    commit = pool.submit(lambda: 'commit')
                    release.set()
                    self.assertEqual(commit.result(timeout=3), 'commit')
                    converter.convert_selection.assert_not_called()
                    self.assertFalse(child.poll(.05))
                finally:
                    release.set()
                    preview.stop.set()
                    thread.join(timeout=3)
                    parent.close()
                    child.close()
            self.assertFalse(thread.is_alive())

    def test_pause_discards_completed_result_before_publication(self):
        preview = preview_fixture(Mock(), Mock())
        preview.connection = Mock()
        preview.stop = Mock()
        preview.stop.wait.side_effect = [False, False, True]
        preview.connection.poll.side_effect = [True, False, False]
        preview.connection.recv.return_value = (1, TextRegion('', 'さんぽ', ''))
        future = Mock()
        def completed():
            preview.pause()
            preview.resume()
            return True
        future.done.side_effect = completed
        future.result.return_value = '散歩'
        preview.pool.submit.return_value = future
        preview._run()
        preview.connection.send.assert_not_called()

    def test_paused_request_is_not_submitted(self):
        preview = preview_fixture(Mock(), Mock())
        preview.connection = Mock()
        preview.stop = Mock()
        preview.stop.wait.side_effect = [False, True]
        preview.connection.poll.side_effect = [True, False]
        preview.connection.recv.return_value = (1, TextRegion('', 'さんぽ', ''))
        preview.pause()
        preview._run()
        preview.pool.submit.assert_not_called()
