"""Exercise actual cross-process TSF notifications before IME commit."""
import json
from pathlib import Path
import sys
import time
import unittest

from kana_rewriter.preview import PreviewProbe, preview_position
from tests.test_tsf_probe import Fixture

DIRECTORY = Path(__file__).resolve().parents[1] / 'build/tsf-preview'


@unittest.skipUnless(sys.platform == 'win32' and (DIRECTORY / 'latest.json').is_file(),
                     'Build scripts/build_preview.ps1 first')
class NativePreviewTests(unittest.TestCase):
    def test_empty_retired_composition_does_not_end_replacement_composition(self):
        from kana_rewriter.preview_state import PreviewStream
        name = json.loads((DIRECTORY / 'latest.json').read_text(encoding='utf-8-sig'))['dll']
        with Fixture() as fixture:
            probe = PreviewProbe(fixture.tid, str(DIRECTORY / name)).start()
            try:
                self.wait_event(probe, lambda e: e['kind'] == 2)
                fixture.command('begin')
                self.wait_event(probe, lambda e: e['kind'] == 3 and e['active'])
                fixture.command('recompose')
                events = probe.drain()
                self.assertTrue(any(e['kind'] == 3 and e['active'] for e in events))
                self.assertFalse(any(e['kind'] in (4, 5) for e in events), events)
                stream = PreviewStream()
                for event in events:
                    stream.observe(event)
                self.assertTrue(stream.active)
                fixture.command('update')
                event = self.wait_event(probe, lambda e: e['kind'] == 3 and e['text'] == 'さんぽ')
                stream.observe(event)
                self.assertTrue(stream.active)
            finally:
                self.assertTrue(probe.close())

    def wait_event(self, probe, predicate):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            for event in probe.drain():
                if predicate(event):
                    return event
            time.sleep(0.01)
        self.fail('Expected live composition event did not arrive')

    def test_text_and_context_are_captured_from_the_same_tsf_document(self):
        name = json.loads((DIRECTORY / 'latest.json').read_text(encoding='utf-8-sig'))['dll']
        with Fixture() as fixture:
            probe = PreviewProbe(fixture.tid, str(DIRECTORY / name)).start()
            try:
                self.wait_event(probe, lambda e: e['kind'] == 2)
                fixture.command('contextcase')
                fixture.command('begin')
                event = self.wait_event(probe, lambda e: e['kind'] == 3 and e['active'])
                self.assertEqual(event['before'], 'a' * 10)
                self.assertEqual(event['text'], 'へんかんてすと')
                self.assertEqual(event['after'], 'z' * 10)
            finally:
                self.assertTrue(probe.close())

    def test_layout_not_ready_is_retried_on_layout_notification(self):
        name = json.loads((DIRECTORY / 'latest.json').read_text(encoding='utf-8-sig'))['dll']
        for operation in ('layoutmissing', 'layoutzero'):
            with self.subTest(operation=operation), Fixture() as fixture:
                probe = PreviewProbe(fixture.tid, str(DIRECTORY / name)).start()
                try:
                    self.wait_event(probe, lambda e: e['kind'] == 2)
                    fixture.command(operation)
                    fixture.command('begin')
                    event = self.wait_event(probe, lambda e: e['kind'] == 3 and e['active'] == 1)
                    self.assertIsNone(preview_position(event))
                    fixture.command('layoutready')
                    event = self.wait_event(probe, lambda e: e['kind'] == 3 and e['position_valid'])
                    self.assertEqual(preview_position(event), (100, 20))
                    self.assertEqual(event['text'], 'さんぽ')
                finally:
                    self.assertTrue(probe.close())

    def test_uncommitted_text_delivered_before_commit(self):
        name = json.loads((DIRECTORY / 'latest.json').read_text(encoding='utf-8-sig'))['dll']
        with Fixture() as fixture:
            probe = PreviewProbe(fixture.tid, str(DIRECTORY / name)).start()
            try:
                self.wait_event(probe, lambda e: e['kind'] == 2)
                fixture.command('begin')
                live = self.wait_event(probe, lambda e: e['kind'] == 3 and e['active'] == 1)
                self.assertEqual(live['text'], 'さんぽ')
                self.assertEqual(preview_position(live), (100, 20))
                fixture.command('update')
                live = self.wait_event(probe, lambda e: e['kind'] == 3 and e['active'] == 1)
                self.assertEqual(live['text'], 'さんぽ')
                self.assertEqual(preview_position(live), (100, 20))
                for operation, expected in (('grow', 'さんぽをしよう'),
                                            ('longupdate', 'あ' * 700), ('shrink', 'さん')):
                    fixture.command(operation)
                    live = self.wait_event(probe, lambda e: e['kind'] == 3 and e['active'] == 1)
                    self.assertEqual(live['text'], expected)
                fixture.command('cancel')
                self.wait_event(probe, lambda e: e['kind'] == 3 and e['active'] == 0)
            finally:
                self.assertTrue(probe.close())
