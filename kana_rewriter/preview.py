"""Read-only live preview. UIA and the non-activating window live in a child."""
import multiprocessing
import threading
import time
import logging
from .core import ConversionCancelled, check_cancelled

from .preview_state import (PreviewStream, PreviewDisplay, PreviewGate, PreviewTarget,
                            preview_position, preview_region, stream_target)
from .tsf import NativeProbe, Packet
import ctypes as C


class PreviewFocusReader:
    """TSF attachment must not require UIA to expose the document text."""
    def __init__(self):
        from .direct_uia import Automation
        self.automation = Automation()

    def read(self):
        from .auto_ime import Focus
        from .direct_uia import property_values
        from .direct_win32 import window_identity
        from .winapi import user
        target = window_identity()
        element = self.automation.client.GetFocusedElement()
        if not element:
            return None
        values = property_values(self.automation, element,
                                 ('IsPassword', 'IsEnabled', 'HasKeyboardFocus', 'ControlType'))
        if values['IsPassword'] or not values['IsEnabled'] or not values['HasKeyboardFocus']:
            return None
        # Chromium may focus a text child while its Edit/Document owner remains
        # unchanged. Resolve that owner without requiring text retrieval via UIA.
        for _ in range(8):
            if values['ControlType'] in (50004, 50030):
                break
            element = self.automation.client.ControlViewWalker.GetParentElement(element)
            if not element:
                return None
            values = property_values(self.automation, element,
                                     ('IsPassword', 'IsEnabled', 'ControlType'))
            if values['IsPassword'] or not values['IsEnabled']:
                return None
        else:
            return None
        value = self.automation.pattern(element, 'ValuePattern')
        if value and value.CurrentIsReadOnly:
            return None
        runtime = tuple(element.GetRuntimeId())
        tid = user.GetWindowThreadProcessId(target[1], None)
        if not runtime or not tid or target != window_identity():
            return None
        return Focus(target, runtime, tid)


class PreviewPacket(C.Structure):
    _fields_ = (Packet._fields_
                + [(name, C.c_long) for name in ('left', 'top', 'right', 'bottom')]
                + [('position_valid', C.c_uint32),
                   ('before', C.c_wchar * 1024), ('after', C.c_wchar * 1024)])


class PreviewProbe(NativeProbe):
    packet_type = PreviewPacket
    protocol_version = 5


def _read_snapshots(config, output, stop, reader_factory=PreviewFocusReader, identity=None):
    import logging
    from pathlib import Path
    import json
    from .direct_win32 import window_identity
    log = logging.getLogger(__name__)
    identity = identity or window_identity
    reader = reader_factory()
    directory = Path(__file__).resolve().parents[1] / 'build/tsf-preview'
    name = json.loads((directory / 'latest.json').read_text(encoding='utf-8-sig'))['dll']
    if Path(name).name != name or not name.endswith('.dll'):
        raise ValueError('Invalid preview DLL manifest')
    dll = directory / name
    probe = focus = None
    stream = PreviewStream()
    composition_tick = None
    def publish(snapshot):
        with output['lock']:
            changed = snapshot != output.get('value', (0, None))[1]
            output['value'] = (time.monotonic(), snapshot)
        if changed and output.get('wake'):
            output['wake']()
    try:
        while not stop.wait(0.016):
            events = probe.drain() if probe else []
            if probe and probe.dropped():
                raise RuntimeError('プレビューのTSF通知が欠落しました')
            try:
                target = identity()
            except Exception:
                target = None
            context_changed = any(e['kind'] in (2, 7) for e in events)
            active_ticks = [e.get('composition_tick', 0) for e in events
                            if e['kind'] == 3 and e.get('active', 0) > 0]
            next_tick = active_ticks[-1] if active_ticks else composition_tick
            composition_changed = (composition_tick is not None and next_tick != composition_tick)
            # UIA is eligibility metadata, not the source of live text. Re-read
            # it at a window/TSF-context boundary; a slow provider must not gate
            # every keystroke in an already validated input context.
            if focus is None or target != focus.target or context_changed or composition_changed:
                if focus is None or target != focus.target or context_changed:
                    stream.clear()
                    composition_tick = None
                publish(None)
                started = time.monotonic()
                try:
                    current = reader.read() if target is not None else None
                except Exception:
                    current = None
                    log.debug('プレビューの入力先検証失敗', exc_info=True)
                log.debug('プレビューの入力先検証: %.1fms', (time.monotonic() - started) * 1000)
            else:
                current = focus
            same_target = (current is not None and focus is not None
                           and current.target == focus.target and current.tid == focus.tid)
            if current != focus and not same_target:
                if probe:
                    if not probe.close():
                        raise RuntimeError('プレビューのTSF監視解除を確認できません')
                    probe = None
                    events = []
                focus = current
                stream.clear()
                if current:
                    probe = PreviewProbe(current.tid, str(dll)).start()
                    events = probe.drain()
            focus = current
            snapshot = None
            if current and probe:
                if probe.dropped():
                    raise RuntimeError('プレビューのTSF通知が欠落しました')
                for event in events:
                    if event['pid'] == current.target[2] and event['hwnd'] == current.target[1]:
                        stream.observe(event)
                        if event['kind'] == 3 and event.get('active', 0) > 0:
                            composition_tick = event.get('composition_tick', 0)
                snapshot = stream.snapshot(current, config)
            if current:
                try:
                    if current.target != identity():
                        snapshot = None
                except Exception:
                    snapshot = None
            publish(snapshot)
    finally:
        if probe:
            if not probe.close():
                raise RuntimeError('プレビューのTSF監視解除を確認できません')


def _preview_window(config, connection, snapshot_reader=_read_snapshots, diagnostics=False):
    import sys
    import signal
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Parent owns console shutdown.
    if diagnostics:
        logging.basicConfig(level=logging.DEBUG, format='[診断] %(message)s')
    logging.getLogger('comtypes').setLevel(logging.WARNING)
    sys.coinit_flags = 0
    import comtypes  # Initialize the import-owned apartment on the main thread.
    from .preview_window import PreviewWindow
    shared = {'lock': threading.Lock(), 'value': (0, None), 'error': None}
    stop = threading.Event()
    def read():
        initialized = False
        try:
            comtypes.CoInitializeEx(0)
            initialized = True
            snapshot_reader(config, shared, stop)
        except Exception as exc:
            import traceback
            with shared['lock']:
                shared['value'] = (0, None)
                shared['error'] = traceback.format_exc()
            shared['wake']()
        finally:
            if initialized:
                comtypes.CoUninitialize()
    reader_thread = threading.Thread(target=read, name='preview-tsf', daemon=True)
    display = PreviewDisplay()
    gate = display.gate
    def poll():
        try:
            now = time.monotonic()
            with shared['lock']:
                timestamp, snapshot = shared['value']
                error = shared['error']
                shared['error'] = None
            if error:
                connection.send(('error', error))
            if (snapshot is None or now - timestamp > 0.5
                    or window.user.GetForegroundWindow() != snapshot[4]):
                snapshot = None
            display.observe(snapshot, now)
            while connection.poll():
                generation, result = connection.recv()
                if generation == 'close':
                    stop.set()
                    window.close()
                    return
                display.accept(generation, result)
            if snapshot and (display.result or display.reading):
                window.show(display.result or '', snapshot[2], snapshot[3], display.reading)
            else:
                window.hide()
            if gate.ready(now):
                connection.send((gate.generation, snapshot[1]))
                gate.requested = True
        except (EOFError, BrokenPipeError, OSError):
            stop.set()
            window.close()
        except Exception:
            import traceback
            try:
                connection.send(('error', traceback.format_exc()))
            finally:
                stop.set()
                window.close()
    window = PreviewWindow(poll)
    shared['wake'] = window.request_poll
    connection.send(('ready', window.hwnd))
    reader_thread.start()
    try:
        window.run()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        reader_thread.join(timeout=3)
        connection.close()


class LivePreview:
    def __init__(self, config, converter, pool):
        self.converter, self.pool = converter, pool
        self.stop = threading.Event()
        self.paused = threading.Event()
        self.request_lock = threading.Lock()
        self.cancel = None
        self.pause_revision = 0
        context = multiprocessing.get_context('spawn')
        self.connection, child = context.Pipe()
        self.process = context.Process(target=_preview_window, args=(config, child), daemon=True,
                                       kwargs={'diagnostics': logging.getLogger().isEnabledFor(logging.DEBUG)})
        try:
            self.process.start()
            child.close()
            if not self.connection.poll(8):
                raise RuntimeError('プレビューUIの起動を確認できませんでした')
            kind, detail = self.connection.recv()
            if kind != 'ready':
                raise RuntimeError('プレビューUI起動失敗: ' + str(detail))
            self.thread = threading.Thread(target=self._run, name='preview-inference', daemon=True)
            self.thread.start()
        except BaseException:
            child.close()
            if self.process.pid is not None:
                if self.process.is_alive():
                    self.process.terminate()
                self.process.join(timeout=2)
            self.connection.close()
            raise

    def pause(self):
        # Serialize admission with pause: no preview may slip in ahead of a commit.
        with self.request_lock:
            self.paused.set()
            self.pause_revision += 1
            if self.cancel is not None:
                self.cancel.set()

    def resume(self):
        with self.request_lock:
            self.paused.clear()

    def _convert(self, region, queued_at, cancel):
        started = time.monotonic()
        try:
            check_cancelled(cancel)
            result = self.converter.convert_selection(region.target, region.left, region.right,
                                                      cancel=cancel)
            check_cancelled(cancel)
            return result
        finally:
            logging.getLogger(__name__).debug('プレビュー推論: 待機=%.1fms / 推論=%.1fms',
                                              (started - queued_at) * 1000,
                                              (time.monotonic() - started) * 1000)

    def _run(self):
        pending = None
        latest = None
        while not self.stop.wait(0.01):
            try:
                while self.connection.poll():
                    message = self.connection.recv()
                    if message[0] == 'error':
                        logging.getLogger(__name__).error('プレビュー: %s', message[1])
                    else:
                        with self.request_lock:
                            latest = None if self.paused.is_set() else (self.pause_revision, message)
                if pending is not None and pending[1].done():
                    generation, future, cancel = pending
                    pending = None
                    try:
                        result = future.result()
                    except ConversionCancelled:
                        logging.getLogger(__name__).debug('プレビュー推論: 確定変換のため中断')
                        result = ''
                    except Exception:
                        logging.getLogger(__name__).warning('プレビュー推論に失敗しました', exc_info=True)
                        result = ''
                    with self.request_lock:
                        publish = not cancel.is_set()
                        if self.cancel is cancel:
                            self.cancel = None
                    # Pipe I/O must not block the main thread's pause request.
                    if publish:
                        self.connection.send((generation, result))
                with self.request_lock:
                    if latest and pending is None:
                        revision, (generation, region) = latest
                        latest = None
                        if not self.paused.is_set() and revision == self.pause_revision:
                            cancel = threading.Event()
                            self.cancel = cancel
                            pending = (generation, self.pool.submit(
                                self._convert, region, time.monotonic(), cancel), cancel)
            except (EOFError, BrokenPipeError, OSError, RuntimeError):
                return

    def close(self):
        self.pause()
        self.stop.set()
        self.thread.join(timeout=1)
        try:
            self.connection.send(('close', None))
        except (EOFError, BrokenPipeError, OSError):
            pass
        self.process.join(timeout=4)
        if self.process.is_alive():
            self.process.terminate()
        self.process.join(timeout=2)
        self.connection.close()
