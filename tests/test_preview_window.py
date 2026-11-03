"""Observe real window-position notifications, including intermediate moves."""
import ctypes as C
from ctypes import wintypes as W
import multiprocessing
import sys
import unittest


def edge_window_fixture(connection):
    from kana_rewriter.preview_window import PreviewWindow, MonitorInfo
    class WindowPos(C.Structure):
        _fields_ = [('hwnd', W.HWND), ('after', W.HWND), ('x', C.c_int),
                    ('y', C.c_int), ('width', C.c_int), ('height', C.c_int), ('flags', W.UINT)]
    class TracedWindow(PreviewWindow):
        def __init__(self, poll):
            self.moves = []
            super().__init__(poll)
        def _message(self, hwnd, message, wp, lp):
            if message == 0x47 and hwnd == getattr(self, 'hwnd', None):
                pos = C.cast(lp, C.POINTER(WindowPos)).contents
                self.moves.append((pos.x, pos.y, pos.width, pos.height))
            return super()._message(hwnd, message, wp, lp)
    def poll():
        try:
            if not connection.poll():
                return
            command = connection.recv()
            if command == 'close':
                window.close()
                return
            text, x, y, repeat = command
            window.moves.clear()
            for _ in range(repeat):
                window.show(text, x, y)
            connection.send((window.geometry, window.moves, window.dpi,
                             window.user.IsWindowVisible(window.dpi_hwnd)))
        except EOFError:
            window.close()
    window = TracedWindow(poll)
    window.user.IsWindowVisible.argtypes = [W.HWND]
    work_areas = []
    callback_type = C.WINFUNCTYPE(W.BOOL, W.HANDLE, W.HDC, C.POINTER(W.RECT), W.LPARAM)
    def collect(monitor, dc, rect, data):
        info = MonitorInfo(size=C.sizeof(MonitorInfo))
        if window.user.GetMonitorInfoW(monitor, C.byref(info)):
            r = info.work
            work_areas.append((r.left, r.top, r.right, r.bottom))
        return True
    callback = callback_type(collect)
    window.user.EnumDisplayMonitors.argtypes = [W.HDC, C.POINTER(W.RECT), callback_type, W.LPARAM]
    window.user.EnumDisplayMonitors(None, None, callback, 0)
    connection.send(work_areas)
    try:
        window.run()
    finally:
        connection.close()


@unittest.skipUnless(sys.platform == 'win32', 'Windows window geometry')
class PreviewWindowTests(unittest.TestCase):
    def test_edges_have_one_final_move_and_repeated_frames_do_not_move(self):
        context = multiprocessing.get_context('spawn')
        parent, child = context.Pipe()
        process = context.Process(target=edge_window_fixture, args=(child,))
        process.start()
        child.close()
        try:
            self.assertTrue(parent.poll(8))
            work_areas = parent.recv()
            self.assertTrue(work_areas)
            corners = [(work, x, y) for work in work_areas
                       for x in (work[0], work[2] - 1) for y in (work[1], work[3] - 1)]
            for work, x, y in corners:
                left, top, right, bottom = work
                with self.subTest(x=x, y=y):
                    parent.send(('画面の端でもプレビューを安定して表示する' * 5, x, y, 30))
                    self.assertTrue(parent.poll(3))
                    rect, moves, dpi, helper_visible = parent.recv()
                    self.assertEqual(moves, [rect], 'Visible window visited an intermediate position')
                    self.assertFalse(helper_visible, 'DPI measurement window must stay hidden')
                    px, py, width, height = rect
                    self.assertGreaterEqual(px, left)
                    self.assertGreaterEqual(py, top)
                    self.assertLessEqual(px + width, right)
                    self.assertLessEqual(py + height, bottom)
                    parent.send(('画面の端でもプレビューを安定して表示する' * 5, x, y, 30))
                    self.assertTrue(parent.poll(3))
                    self.assertEqual(parent.recv()[1], [])
            # Near the bottom, a tall candidate moves above. A short successor
            # that could fit below must remain above the same caret line.
            y = bottom - round(100 * dpi / 96)
            parent.send(('長い候補です。' * 60, right - 1, y, 1))
            self.assertTrue(parent.poll(3))
            self.assertLess(parent.recv()[0][1], y)
            parent.send(('短い候補', right - 1, y, 30))
            self.assertTrue(parent.poll(3))
            rect, moves, _, _ = parent.recv()
            self.assertLess(rect[1] + rect[3], y)
            self.assertEqual(moves, [rect])
        finally:
            try:
                parent.send('close')
            except (EOFError, OSError):
                pass
            parent.close()
            process.join(3)
            if process.is_alive():
                process.terminate()
                process.join(2)
