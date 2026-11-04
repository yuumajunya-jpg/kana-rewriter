"""Per-monitor DPI aware, non-activating Win32 preview with ClearType text."""
import ctypes as C
from ctypes import wintypes as W


BACKGROUND = 0x30241C  # #1C2430
TEXT = 0xFCF9F7        # #F7F9FC
READING = 0xB7A79B     # #9BA7B7
BORDER = 0x4F4137      # #37414F
ACCENT = 0xFF9B6E      # #6E9BFF


def place_preview(x, y, width, height, work, above=False, gap=8, clearance=24):
    left, top, right, bottom = work
    width, height = min(width, right - left), min(height, bottom - top)
    x = max(left, min(x, right - width))
    y = y + gap if not above and y + height + gap <= bottom else y - height - clearance
    return x, max(top, min(y, bottom - height)), width, height


def layout_metrics(dpi):
    """Design values at 96 DPI scaled to the destination monitor."""
    scale = dpi / 96
    return {'left': round(18 * scale), 'right': round(15 * scale),
            'top': round(8 * scale), 'bottom': round(9 * scale),
            'single_top': round(11 * scale), 'single_bottom': round(12 * scale),
            'reading': round(15 * scale), 'result': round(23 * scale),
            'bar_left': round(8 * scale),
            'bar_top': round(9 * scale), 'bar_width': max(2, round(3 * scale)),
            'border': max(1, round(scale))}


class MonitorInfo(C.Structure):
    _fields_ = [('size', W.DWORD), ('monitor', W.RECT), ('work', W.RECT), ('flags', W.DWORD)]


class Paint(C.Structure):
    _fields_ = [('dc', W.HDC), ('erase', W.BOOL), ('rect', W.RECT),
                ('restore', W.BOOL), ('update', W.BOOL), ('reserved', C.c_byte * 32)]


class PreviewWindow:
    def __init__(self, poll):
        self.user = u = C.WinDLL('user32', use_last_error=True)
        self.gdi = g = C.WinDLL('gdi32')
        kernel = C.WinDLL('kernel32')
        u.SetProcessDpiAwarenessContext.argtypes = [W.HANDLE]
        u.SetProcessDpiAwarenessContext(W.HANDLE(-4))  # PER_MONITOR_AWARE_V2
        self.proc_type = C.WINFUNCTYPE(W.LPARAM, W.HWND, W.UINT, W.WPARAM, W.LPARAM)
        class WindowClass(C.Structure):
            _fields_ = [('style', W.UINT), ('proc', self.proc_type), ('extra', C.c_int),
                        ('window_extra', C.c_int), ('instance', W.HINSTANCE), ('icon', W.HANDLE),
                        ('cursor', W.HANDLE), ('background', W.HBRUSH), ('menu', W.LPCWSTR),
                        ('name', W.LPCWSTR)]
        u.DefWindowProcW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        u.DefWindowProcW.restype = W.LPARAM
        u.CreateWindowExW.argtypes = [W.DWORD, W.LPCWSTR, W.LPCWSTR, W.DWORD,
                                    C.c_int, C.c_int, C.c_int, C.c_int,
                                    W.HWND, W.HMENU, W.HINSTANCE, C.c_void_p]
        u.CreateWindowExW.restype = W.HWND
        u.SetWindowPos.argtypes = [W.HWND, W.HWND, C.c_int, C.c_int, C.c_int, C.c_int, W.UINT]
        u.MonitorFromPoint.argtypes = [W.POINT, W.DWORD]
        u.MonitorFromPoint.restype = W.HANDLE
        u.GetMonitorInfoW.argtypes = [W.HANDLE, C.POINTER(MonitorInfo)]
        u.BeginPaint.argtypes = [W.HWND, C.POINTER(Paint)]
        u.BeginPaint.restype = W.HDC
        u.EndPaint.argtypes = [W.HWND, C.POINTER(Paint)]
        u.GetMessageW.argtypes = [C.POINTER(W.MSG), W.HWND, W.UINT, W.UINT]
        u.TranslateMessage.argtypes = [C.POINTER(W.MSG)]
        u.DispatchMessageW.argtypes = [C.POINTER(W.MSG)]
        u.DispatchMessageW.restype = W.LPARAM
        u.GetDC.argtypes = [W.HWND]
        u.GetDC.restype = W.HDC
        u.ReleaseDC.argtypes = [W.HWND, W.HDC]
        u.ShowWindow.argtypes = [W.HWND, C.c_int]
        u.GetForegroundWindow.restype = W.HWND
        u.GetDpiForWindow.argtypes = [W.HWND]
        u.InvalidateRect.argtypes = [W.HWND, C.c_void_p, W.BOOL]
        u.DestroyWindow.argtypes = [W.HWND]
        u.SetWindowTextW.argtypes = [W.HWND, W.LPCWSTR]
        u.SetTimer.argtypes = [W.HWND, C.c_size_t, W.UINT, C.c_void_p]
        u.PostMessageW.argtypes = [W.HWND, W.UINT, W.WPARAM, W.LPARAM]
        u.DrawTextW.argtypes = [W.HDC, W.LPCWSTR, C.c_int, C.POINTER(W.RECT), W.UINT]
        u.FillRect.argtypes = [W.HDC, C.POINTER(W.RECT), W.HBRUSH]
        g.CreateSolidBrush.argtypes = [W.DWORD]
        g.CreateSolidBrush.restype = W.HBRUSH
        g.CreateRectRgn.argtypes = [C.c_int] * 4
        g.CreateRectRgn.restype = W.HANDLE
        g.FrameRgn.argtypes = [W.HDC, W.HANDLE, W.HBRUSH, C.c_int, C.c_int]
        g.CreateFontW.argtypes = [C.c_int] * 5 + [W.DWORD] * 8 + [W.LPCWSTR]
        g.CreateFontW.restype = W.HANDLE
        g.SelectObject.argtypes = [W.HDC, W.HANDLE]
        g.SelectObject.restype = W.HANDLE
        g.DeleteObject.argtypes = [W.HANDLE]
        g.SetTextColor.argtypes = [W.HDC, W.DWORD]
        g.SetBkMode.argtypes = [W.HDC, C.c_int]
        self.text = ''
        self.reading = ''
        self.font = None
        self.reading_font = None
        self.dpi = 0
        self.dpi_monitor = None
        self.geometry = None
        self.layout_key = None
        self.anchor_line = None
        self.above = False
        self.visible = False
        self.brush = g.CreateSolidBrush(BACKGROUND)
        self.accent_brush = g.CreateSolidBrush(ACCENT)
        self.border_brush = g.CreateSolidBrush(BORDER)
        self.rect = W.RECT()
        self.callback = self.proc_type(self._message)
        kernel.GetModuleHandleW.restype = W.HINSTANCE
        self.instance = kernel.GetModuleHandleW(None)
        # CS_DROPSHADOW gives the card a shadow without a layered window, so
        # ClearType stays available.
        cls = WindowClass(0x00020000, self.callback, 0, 0, self.instance, None, None, None, None,
                          'KanaConversionPreview')
        if not u.RegisterClassW(C.byref(cls)):
            raise C.WinError(C.get_last_error())
        self.hwnd = u.CreateWindowExW(0x080000A8, cls.name, '', 0x80000000,
                                      0, 0, 1, 1, None, None, self.instance, None)
        if not self.hwnd:
            raise C.WinError(C.get_last_error())
        self._disable_rounded_corners()
        # GetDpiForWindow requires a window on the destination monitor. Move a
        # hidden STATIC helper, never the visible candidate, to obtain that DPI.
        self.dpi_hwnd = u.CreateWindowExW(0x08000080, 'STATIC', '', 0x80000000,
                                         0, 0, 1, 1, None, None, self.instance, None)
        if not self.dpi_hwnd:
            u.DestroyWindow(self.hwnd)
            raise C.WinError(C.get_last_error())
        self.poll = poll
        if not u.SetTimer(self.hwnd, 1, 16, None):
            self.close()
            raise C.WinError(C.get_last_error())

    def _disable_rounded_corners(self):
        try:
            dwm = C.WinDLL('dwmapi')
            dwm.DwmSetWindowAttribute.argtypes = [W.HWND, W.DWORD, C.c_void_p, W.DWORD]
            preference = C.c_int(1)  # DWMWCP_DONOTROUND
            dwm.DwmSetWindowAttribute(
                self.hwnd, 33, C.cast(C.byref(preference), C.c_void_p),
                C.sizeof(preference))
        except OSError:
            pass

    def _paint(self, dc):
        metrics = layout_metrics(self.dpi)
        self.user.FillRect(dc, C.byref(self.rect), self.brush)
        region = self.gdi.CreateRectRgn(0, 0, self.rect.right, self.rect.bottom)
        if region:
            self.gdi.FrameRgn(dc, region, self.border_brush, metrics['border'], metrics['border'])
            self.gdi.DeleteObject(region)
        top = metrics['bar_top']
        bar = W.RECT(metrics['bar_left'], top, metrics['bar_left'] + metrics['bar_width'],
                     max(top + 1, self.rect.bottom - top))
        self.user.FillRect(dc, C.byref(bar), self.accent_brush)

        self.gdi.SetBkMode(dc, 1)
        left, right = metrics['left'], self.rect.right - metrics['right']
        top = metrics['top']
        if self.reading:
            previous = self.gdi.SelectObject(dc, self.reading_font)
            self.gdi.SetTextColor(dc, READING)
            area = W.RECT(left, top, right, top + metrics['reading'])
            self.user.DrawTextW(dc, self.reading, -1, C.byref(area), 0x20 | 0x800 | 0x8000)
            self.gdi.SelectObject(dc, previous)
            top += metrics['reading']
            bottom = metrics['bottom']
        else:
            top, bottom = metrics['single_top'], metrics['single_bottom']
        previous = self.gdi.SelectObject(dc, self.font)
        self.gdi.SetTextColor(dc, TEXT)
        area = W.RECT(left, top, right, self.rect.bottom - bottom)
        self.user.DrawTextW(dc, self.text, -1, C.byref(area), 0x10 | 0x800 | 0x8000)
        self.gdi.SelectObject(dc, previous)

    def _message(self, hwnd, message, wp, lp):
        if message in (0x113, 0x8001):
            self.poll()
            return 0
        if message == 0xF:
            paint = Paint()
            dc = self.user.BeginPaint(hwnd, C.byref(paint))
            self._paint(dc)
            self.user.EndPaint(hwnd, C.byref(paint))
            return 0
        if message == 0x14:
            return 1
        if message == 0x21:
            return 3  # MA_NOACTIVATE
        if message == 2:
            self.user.PostQuitMessage(0)
            return 0
        return self.user.DefWindowProcW(hwnd, message, wp, lp)

    def request_poll(self):
        # Wake the UI without waiting for its periodic timer or calling it off-thread.
        self.user.PostMessageW(self.hwnd, 0x8001, 0, 0)

    def hide(self):
        if self.visible:
            self.user.ShowWindow(self.hwnd, 0)
        self.visible = False
        self.anchor_line = None
        self.above = False

    def show(self, text, x, y, reading=''):
        monitor = self.user.MonitorFromPoint(W.POINT(x, y), 2)
        info = MonitorInfo(size=C.sizeof(MonitorInfo))
        if not self.user.GetMonitorInfoW(monitor, C.byref(info)):
            raise C.WinError(C.get_last_error())
        work = info.work
        bounds = (work.left, work.top, work.right, work.bottom)
        screen = info.monitor
        dpi_monitor = (monitor, screen.left, screen.top, screen.right, screen.bottom)
        if dpi_monitor != self.dpi_monitor:
            if not self.user.SetWindowPos(self.dpi_hwnd, None, work.left, work.top, 1, 1, 0x14):
                raise C.WinError(C.get_last_error())
            self.dpi_monitor = dpi_monitor
        dpi = self.user.GetDpiForWindow(self.dpi_hwnd)
        if not dpi:
            raise C.WinError(C.get_last_error())
        if dpi != self.dpi:
            for font in (self.font, self.reading_font):
                if font:
                    self.gdi.DeleteObject(font)
            self.dpi = dpi
            self.font = self.gdi.CreateFontW(-round(17 * dpi / 96), 0, 0, 0, 400,
                                             0, 0, 0, 1, 0, 0, 5, 0, 'Yu Gothic UI')
            self.reading_font = self.gdi.CreateFontW(-round(12 * dpi / 96), 0, 0, 0, 400,
                                                     0, 0, 0, 1, 0, 0, 5, 0, 'Yu Gothic UI')
        metrics = layout_metrics(dpi)
        limit = work.right - work.left
        max_width = min(round(480 * dpi / 96), limit)
        content_limit = max(1, min(max_width - metrics['left'] - metrics['right'],
                                   limit - metrics['left'] - metrics['right']))
        layout_key = (text, reading, dpi, content_limit, work.bottom - work.top)
        changed = layout_key != self.layout_key
        if changed:
            dc = self.user.GetDC(self.hwnd)
            reading_width = 0
            if reading:
                old = self.gdi.SelectObject(dc, self.reading_font)
                area = W.RECT(0, 0, 1 << 20, 0)
                self.user.DrawTextW(dc, reading, -1, C.byref(area), 0x400 | 0x20 | 0x800)
                self.gdi.SelectObject(dc, old)
                reading_width = min(area.right, content_limit)
            old = self.gdi.SelectObject(dc, self.font)
            area = W.RECT(0, 0, content_limit, 0)
            self.user.DrawTextW(dc, text, -1, C.byref(area), 0x400 | 0x10 | 0x800)
            content = max(1, reading_width, min(area.right, content_limit))
            # Re-measure at the final width: a width based on the longest
            # wrapped line can otherwise change the line count.
            area = W.RECT(0, 0, content, 0)
            self.user.DrawTextW(dc, text, -1, C.byref(area), 0x400 | 0x10 | 0x800)
            if area.right > content:
                content = min(content_limit, area.right)
                area = W.RECT(0, 0, content, 0)
                self.user.DrawTextW(dc, text, -1, C.byref(area), 0x400 | 0x10 | 0x800)
            self.gdi.SelectObject(dc, old)
            self.user.ReleaseDC(self.hwnd, dc)
            result_height = max(area.bottom, metrics['result'])
            height = (metrics['top'] + metrics['reading'] + result_height + metrics['bottom']
                      if reading else
                      metrics['single_top'] + result_height + metrics['single_bottom'])
            self.measured = (content + metrics['left'] + metrics['right'], height)
            self.layout_key = layout_key
        width, height = self.measured
        gap, clearance = round(8 * dpi / 96), round(24 * dpi / 96)
        line = (monitor, bounds, y, dpi)
        if line != self.anchor_line:
            self.anchor_line = line
            self.above = False
        # Once the candidate has moved above this caret line, shrinking results
        # must not flip it below again. A new line or hide resets the choice.
        if y + height + gap > work.bottom:
            self.above = True
        x, y, width, height = place_preview(x, y, width, height, bounds,
                                            above=self.above, gap=gap, clearance=clearance)
        if changed:
            self.text = text
            self.reading = reading
            self.user.SetWindowTextW(self.hwnd, text)
            self.rect = W.RECT(0, 0, width, height)
        geometry = (x, y, width, height)
        if geometry != self.geometry or not self.visible:
            if not self.user.SetWindowPos(self.hwnd, -1, *geometry, 0x50):
                raise C.WinError(C.get_last_error())
            self.geometry = geometry
            self.visible = True
        if changed:
            self.user.InvalidateRect(self.hwnd, None, False)

    def close(self):
        self.user.DestroyWindow(self.dpi_hwnd)
        self.user.DestroyWindow(self.hwnd)

    def run(self):
        message = W.MSG()
        while self.user.GetMessageW(C.byref(message), None, 0, 0) > 0:
            self.user.TranslateMessage(C.byref(message))
            self.user.DispatchMessageW(C.byref(message))
        for font in (self.font, self.reading_font):
            if font:
                self.gdi.DeleteObject(font)
        for brush in (self.brush, self.accent_brush, self.border_brush):
            self.gdi.DeleteObject(brush)
