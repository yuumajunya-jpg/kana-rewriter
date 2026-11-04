"""Real TSF fixture -> production reader -> IPC -> visible native candidate.

Only focus metadata is supplied because the fixture has no visible editor.
Candidate replies are deterministic; the test does not load a language model.
No desktop keyboard/mouse automation is used.
"""
import ctypes as C
from ctypes import wintypes as W
import multiprocessing
from pathlib import Path
from types import SimpleNamespace
import sys
import time
import unittest
import queue

from kana_rewriter.auto_ime import Focus
from kana_rewriter.preview import _preview_window, _read_snapshots
from tests.test_tsf_probe import Fixture


def fixture_reader(config, output, stop):
    focus = Focus(config.target, (1,), config.tid)
    def read():
        nonlocal focus
        try:
            runtime = config.focus_changes.get_nowait()
            focus = Focus(config.target, runtime, config.tid)
        except queue.Empty:
            pass
        return focus
    reader = SimpleNamespace(read=read)
    _read_snapshots(config, output, stop, reader_factory=lambda: reader,
                    identity=lambda: config.target)


@unittest.skipUnless(sys.platform == 'win32' and
                    Path('build/tsf-preview/latest.json').is_file(), 'Build TSF fixtures first')
class StreamIntegrationTests(unittest.TestCase):
    def test_continuous_growth_and_missing_layout_do_not_hide_or_truncate(self):
        u = C.WinDLL('user32')
        u.GetForegroundWindow.restype = W.HWND
        u.IsWindowVisible.argtypes = [W.HWND]
        u.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
        with Fixture() as fixture:
            config = SimpleNamespace(target=(u.GetForegroundWindow(), 0, fixture.pid), tid=fixture.tid,
                                     conversion_delimiters='。', stop_at_kanji=True,
                                     trailing_punctuation='。', max_chars=1000,
                                     auto_replace_on_ime_commit=False)
            context = multiprocessing.get_context('spawn')
            config.focus_changes = context.Queue()
            parent, child = context.Pipe()
            process = context.Process(target=_preview_window, args=(config, child, fixture_reader))
            process.start()
            child.close()
            try:
                self.assertTrue(parent.poll(8))
                kind, hwnd = parent.recv()
                self.assertEqual(kind, 'ready')
                fixture.command('begin')
                deadline = time.monotonic() + 3
                while not parent.poll(0.02) and time.monotonic() < deadline:
                    fixture.command('update')
                self.assertTrue(parent.poll(0), 'Production reader never delivered TSF input')
                generation, target = parent.recv()
                self.assertEqual(target.target, 'さんぽ')
                parent.send((generation, '散歩'))

                def text():
                    buffer = C.create_unicode_buffer(2048)
                    u.GetWindowTextW(hwnd, buffer, len(buffer))
                    return buffer.value

                deadline = time.monotonic() + 3
                while (not u.IsWindowVisible(hwnd) or text() != '散歩') and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(u.IsWindowVisible(hwnd))
                fixture.command('layoutmissing')
                fixture.command('grow')
                self.assertTrue(parent.poll(3))
                generation, target = parent.recv()
                self.assertEqual(target.target, 'さんぽをしよう')
                # Sample actual HWND visibility while no new candidate is ready.
                for _ in range(60):
                    self.assertTrue(u.IsWindowVisible(hwnd), 'Preview flickered during layout failure')
                    self.assertEqual(text(), '散歩')
                    time.sleep(0.005)
                parent.send((generation, '散歩をしよう'))
                fixture.command('longupdate')
                self.assertTrue(parent.poll(3))
                generation, target = parent.recv()
                self.assertEqual(target.target, 'あ' * 700)
                parent.send((generation, '亜' * 700))
                deadline = time.monotonic() + 3
                while text() != '亜' * 700 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(text(), '亜' * 700)
                def candidate(expected, result):
                    self.assertTrue(parent.poll(3), 'No preview request after editing composition')
                    generation, target = parent.recv()
                    self.assertEqual(target.target, expected)
                    parent.send((generation, result))
                    deadline = time.monotonic() + 3
                    while (text() != result or not u.IsWindowVisible(hwnd)) and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(u.IsWindowVisible(hwnd))
                    self.assertEqual(text(), result)
                    return generation

                fixture.command('shrink')
                old_generation = candidate('さん', '三')
                # A UIA element recreation must not detach TSF or hide a result.
                config.focus_changes.put((-109666,))
                for _ in range(30):
                    time.sleep(.01)
                    self.assertTrue(u.IsWindowVisible(hwnd), 'RuntimeId change hid preview')
                self.assertFalse(parent.poll(.05), 'RuntimeId change restarted inference')

                fixture.command('erase')
                deadline = time.monotonic() + 3
                while u.IsWindowVisible(hwnd) and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertFalse(u.IsWindowVisible(hwnd))
                parent.send((old_generation, '削除前の候補'))
                time.sleep(.05)
                self.assertFalse(u.IsWindowVisible(hwnd))
                fixture.command('update')
                candidate('さんぽ', '散歩')

                # IME replaces an emptied composition in one edit. Layout is
                # still unavailable: losing the last confirmed position here
                # would prevent all subsequent typing from showing a preview.
                fixture.command('recompose')
                fixture.command('grow')
                candidate('さんぽをしよう', '散歩をしよう')
                for operation, source in (('previewprefix', 'ぷれびゅ'),
                                          ('previewreading', 'ぷれびゅー'),
                                          ('previewkatakana', 'プレビュー')):
                    fixture.command(operation)
                    candidate(source, 'プレビュー')
                fixture.command('cancel')
                deadline = time.monotonic() + 3
                while u.IsWindowVisible(hwnd) and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertFalse(u.IsWindowVisible(hwnd))
            finally:
                parent.close()
                process.join(timeout=3)
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=2)
                config.focus_changes.close()
                config.focus_changes.join_thread()
