"""Real spawned preview process, IPC, HWND visibility and displayed text.

Only the editor snapshot is supplied by the fixture. No desktop input is sent.
Real TSF notifications are covered separately by test_preview_native.
"""
import ctypes as C
from ctypes import wintypes as W
import multiprocessing
import sys
import time
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace
import queue

from kana_rewriter.core import Config
from kana_rewriter.preview import PreviewFocusReader, _preview_window
from kana_rewriter.preview_state import PreviewTarget
from kana_rewriter.text import TextRegion


def snapshot_fixture(config, output, stop):
    u = C.WinDLL('user32')
    u.GetForegroundWindow.restype = W.HWND
    foreground = u.GetForegroundWindow()
    value = None
    while not stop.wait(0.01):
        try:
            value = config.commands.get_nowait()
        except queue.Empty:
            pass
        snapshot = None
        if value:
            key, source, session = value
            snapshot = (key, PreviewTarget('', source, '', reading_text=source),
                        320, 224, foreground, session)
        with output['lock']:
            output['value'] = (time.monotonic(), snapshot)



@unittest.skipUnless(sys.platform == 'win32', 'Windows native preview')
class PreviewPipelineTests(unittest.TestCase):
    def test_latest_reading_is_rendered_before_request_and_kept_with_older_result(self):
        connection, window = Mock(), Mock()
        connection.poll.return_value = False
        window.user.GetForegroundWindow.return_value = 1
        shared = {}
        def snapshot(live):
            return (live, PreviewTarget('', live, '', reading_text=live), 10, 20, 1, 'session')
        def reader(config, output, stop):
            shared.update(output=output)
            output['value'] = (time.monotonic(), snapshot('さん'))
        def make_thread(*, target, **kwargs):
            thread = Mock()
            thread.start.side_effect = target
            return thread
        def run():
            poll = create.call_args.args[0]
            poll()
            window.show.assert_called_with('', 10, 20, 'さん')
            first = connection.send.call_args.args[0][0]
            shared['output']['value'] = (time.monotonic(), snapshot('さんぽ'))
            poll()
            window.show.assert_called_with('', 10, 20, 'さんぽ')
            connection.poll.side_effect = [True, False]
            connection.recv.return_value = (first, '三')
            poll()
            window.show.assert_called_with('三', 10, 20, 'さんぽ')
            # Input ends while a result exists: both lines must disappear.
            shared['output']['value'] = (time.monotonic(), None)
            connection.poll.side_effect = None
            poll()
            window.hide.assert_called()
        window.run.side_effect = run
        with patch.dict(sys.modules, {'comtypes': Mock()}), \
                patch('kana_rewriter.preview.threading.Thread', side_effect=make_thread), \
                patch('kana_rewriter.preview_window.PreviewWindow', return_value=window) as create:
            _preview_window(Config(), connection, reader)

    def test_uia_validation_does_not_block_each_composition_update(self):
        from kana_rewriter.preview import _read_snapshots
        from kana_rewriter.commit_state import Focus
        import threading
        focus = Focus((1, 2, 3), (10,), 4)
        reader = Mock()
        def slow_read():
            time.sleep(.05)
            return focus
        reader.read.side_effect = slow_read
        probe = Mock()
        probe.start.return_value = probe
        probe.close.return_value = True
        probe.dropped.return_value = 0
        def edit(text):
            return dict(kind=3, pid=3, hwnd=2, active=1, text=text, position_valid=1,
                        left=10, right=20, top=10, bottom=30)
        probe.drain.side_effect = [[dict(kind=2, pid=3, hwnd=2)],
                                  [edit('ぷれびゅ')], [edit('ぷれびゅー')]]
        stop = Mock()
        stop.wait.side_effect = [False, False, False, True]
        output = {'lock': threading.Lock(), 'wake': Mock()}
        with patch('kana_rewriter.preview.PreviewProbe', return_value=probe):
            _read_snapshots(Config(), output, stop, reader_factory=lambda: reader,
                            identity=lambda: focus.target)
        reader.read.assert_called_once()
        self.assertEqual(output['value'][1][1].target, 'ぷれびゅー')
        self.assertEqual(output['wake'].call_count, 2)

    def test_context_change_revalidates_and_hides_ineligible_field(self):
        from kana_rewriter.preview import _read_snapshots
        from kana_rewriter.commit_state import Focus
        import threading
        focus = Focus((1, 2, 3), (10,), 4)
        reader = Mock()
        reader.read.side_effect = [focus, None]
        probe = Mock()
        probe.start.return_value = probe
        probe.close.return_value = True
        probe.dropped.return_value = 0
        probe.drain.side_effect = [[dict(kind=2, pid=3, hwnd=2)],
                                  [dict(kind=2, pid=3, hwnd=2)]]
        stop = Mock()
        stop.wait.side_effect = [False, False, True]
        output = {'lock': threading.Lock()}
        with patch('kana_rewriter.preview.PreviewProbe', return_value=probe):
            _read_snapshots(Config(), output, stop, reader_factory=lambda: reader,
                            identity=lambda: focus.target)
        self.assertEqual(reader.read.call_count, 2)
        self.assertIsNone(output['value'][1])
        probe.close.assert_called_once()

    def test_reading_before_inference_and_candidate_hold_and_hide_after_input(self):
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        commands = context.Queue()
        process = context.Process(target=_preview_window,
                                  args=(SimpleNamespace(commands=commands), child, snapshot_fixture))
        process.start()
        child.close()
        try:
            self.assertTrue(parent.poll(8), 'UI process did not signal ready')
            kind, hwnd = parent.recv()
            self.assertEqual(kind, 'ready')
            u = C.WinDLL('user32')
            u.IsWindowVisible.argtypes = [W.HWND]
            time.sleep(0.05)
            self.assertFalse(u.IsWindowVisible(hwnd), 'Idle preview must be hidden')
            commands.put(('input-1', 'さんぽ', 'session-1'))
            self.assertTrue(parent.poll(3), 'Snapshot did not reach inference queue')
            generation, region = parent.recv()
            self.assertEqual(region.target, 'さんぽ')
            u = C.WinDLL('user32')
            u.IsWindowVisible.argtypes = [W.HWND]
            u.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
            u.GetWindowRect.argtypes = [W.HWND, C.POINTER(W.RECT)]
            def wait_text(expected):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    text = C.create_unicode_buffer(2048)
                    u.GetWindowTextW(hwnd, text, len(text))
                    if expected in text.value and u.IsWindowVisible(hwnd):
                        return text.value
                    time.sleep(0.01)
                self.fail('Native window never displayed: ' + expected)
            self.assertTrue(u.IsWindowVisible(hwnd), 'Reading must show before first candidate')
            parent.send((generation, '散歩'))
            self.assertEqual(wait_text('散歩'), '散歩')
            u.GetWindowLongW.argtypes = [W.HWND, C.c_int]
            u.GetForegroundWindow.restype = W.HWND
            self.assertTrue(u.GetWindowLongW(hwnd, -20) & 8, 'Preview must be topmost')
            self.assertNotEqual(u.GetForegroundWindow(), hwnd, 'Preview stole keyboard focus')
            commands.put(('input-2', 'さんぽする', 'session-1'))
            self.assertTrue(parent.poll(3))
            next_generation, next_region = parent.recv()
            self.assertEqual(next_region.target, 'さんぽする')
            time.sleep(0.05)
            self.assertEqual(wait_text('散歩'), '散歩')
            parent.send((next_generation, '散歩する'))
            self.assertEqual(wait_text('散歩する'), '散歩する')
            commands.put(None)
            deadline = time.monotonic() + 3
            while u.IsWindowVisible(hwnd) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(u.IsWindowVisible(hwnd), 'Composition ended but UI remains')
            parent.send((next_generation, '古い候補'))
            time.sleep(0.05)
            self.assertFalse(u.IsWindowVisible(hwnd), 'Late result revived hidden UI')
            rect = W.RECT()
            # The renderer uses physical per-monitor coordinates. Query in the
            # same DPI context instead of the test runner's virtualized pixels.
            u.SetThreadDpiAwarenessContext.argtypes = [W.HANDLE]
            u.SetThreadDpiAwarenessContext.restype = W.HANDLE
            previous_dpi = u.SetThreadDpiAwarenessContext(W.HANDLE(-4))
            try:
                self.assertTrue(u.GetWindowRect(hwnd, C.byref(rect)))
            finally:
                u.SetThreadDpiAwarenessContext(previous_dpi)
            self.assertGreater(rect.right - rect.left, 50)
            self.assertGreater(rect.bottom - rect.top, 20)
            u.GetDpiForWindow.argtypes = [W.HWND]
            gap = round(8 * u.GetDpiForWindow(hwnd) / 96)
            self.assertEqual((rect.left, rect.top), (320, 224 + gap))
        finally:
            parent.close()
            process.join(timeout=3)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)

    def test_tsf_focus_does_not_require_text_pattern(self):
        reader = PreviewFocusReader.__new__(PreviewFocusReader)
        reader.automation = Mock()
        element = reader.automation.client.GetFocusedElement.return_value
        element.GetRuntimeId.return_value = [1, 2]
        reader.automation.pattern.return_value = None
        values = dict(IsPassword=False, IsEnabled=True, HasKeyboardFocus=True, ControlType=50004)
        with patch('kana_rewriter.direct_uia.property_values', return_value=values), \
                patch('kana_rewriter.direct_win32.window_identity', return_value=(1, 2, 3)), \
                patch('kana_rewriter.winapi.user.GetWindowThreadProcessId', return_value=4):
            self.assertEqual(reader.read().tid, 4)
        reader.automation.pattern.assert_called_once_with(element, 'ValuePattern')

    def test_text_child_uses_edit_owner_identity(self):
        reader = PreviewFocusReader.__new__(PreviewFocusReader)
        reader.automation = Mock()
        owner = reader.automation.client.ControlViewWalker.GetParentElement.return_value
        owner.GetRuntimeId.return_value = [100]
        reader.automation.pattern.return_value = None
        child_values = dict(IsPassword=False, IsEnabled=True, HasKeyboardFocus=True, ControlType=50020)
        owner_values = dict(IsPassword=False, IsEnabled=True, ControlType=50004)
        with patch('kana_rewriter.direct_uia.property_values', side_effect=[child_values, owner_values]), \
                patch('kana_rewriter.direct_win32.window_identity', return_value=(1, 2, 3)), \
                patch('kana_rewriter.winapi.user.GetWindowThreadProcessId', return_value=4):
            self.assertEqual(reader.read().runtime, (100,))

    def test_password_and_read_only_fields_are_excluded(self):
        for password, readonly in ((True, False), (False, True)):
            reader = PreviewFocusReader.__new__(PreviewFocusReader)
            reader.automation = Mock()
            reader.automation.pattern.return_value.CurrentIsReadOnly = readonly
            values = dict(IsPassword=password, IsEnabled=True, HasKeyboardFocus=True, ControlType=50004)
            with patch('kana_rewriter.direct_uia.property_values', return_value=values), \
                    patch('kana_rewriter.direct_win32.window_identity', return_value=(1, 2, 3)):
                self.assertIsNone(reader.read())
