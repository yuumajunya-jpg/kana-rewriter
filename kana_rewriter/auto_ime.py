"""Follow editable focus and deliver TSF commits to the normal message loop."""
from dataclasses import dataclass
import logging
import os
import threading
import time

from .tsf_probe import NativeProbe, KINDS, eligible, invalidates

logger = logging.getLogger(__name__)


def tick_count():
    import ctypes as C
    kernel = C.WinDLL("kernel32")
    kernel.GetTickCount64.restype = C.c_uint64
    return kernel.GetTickCount64()


@dataclass(frozen=True)
class Focus:
    target: tuple
    runtime: tuple
    tid: int


@dataclass(frozen=True)
class Commit:
    text: str
    focus: Focus
    tick: int
    revision: int


class CommitState:
    """Bounded mailbox plus an edit/focus revision for in-flight inference."""
    def __init__(self):
        self.lock = threading.Lock()
        self.focus = None
        self.revision = 0
        self.pending = None
        self.busy = False
        self.closed = False
        self.armed = False
        self.saw_key = False
        self.focus_tick = 0

    def track(self, focus, now=0):
        with self.lock:
            if focus != self.focus:
                self.focus = focus
                self.revision += 1
                self.pending = None
                self.armed = self.saw_key = False
                self.focus_tick = now

    def observe(self, events, now):
        with self.lock:
            for event in events:
                if invalidates([event]):
                    self.revision += 1
                    self.pending = None
                if self.closed or self.busy or self.focus is None:
                    continue
                if event["tid"] != self.focus.tid:
                    continue
                if event["tick"] < self.focus_tick:
                    continue
                if event["kind"] in (2, 5, 6, 7, 9):
                    self.armed = self.saw_key = False
                elif event["kind"] == 8:
                    if event["key"] == 0x1B:
                        self.saw_key = False
                        self.armed = False
                    elif event["key"] not in (0x0D, 0x10, 0x11, 0x12, 0x5B, 0x5C):
                        self.saw_key = True
                elif event["kind"] == 3 and event["active"] and self.saw_key:
                    self.armed = True
                valid, _ = eligible(event, now, self.focus.target)
                if valid and self.armed:
                    text = event["text"].replace("\r\n", "\n").replace("\r", "\n")
                    self.pending = Commit(text, self.focus, event["tick"], self.revision)
                    self.armed = self.saw_key = False

    def take(self, now):
        with self.lock:
            commit, self.pending = self.pending, None
            if (not self.closed and not self.busy and commit is not None
                    and 0 <= now - commit.tick <= 1500):
                return commit
        return None

    def valid(self, commit):
        with self.lock:
            return (not self.closed and self.focus == commit.focus
                    and self.revision == commit.revision)

    def begin(self, commit=None):
        with self.lock:
            if self.closed:
                return False
            if commit is not None:
                if self.busy or self.focus != commit.focus or self.revision != commit.revision:
                    return False
            else:
                self.revision += 1
            self.pending = None
            self.busy = True
            return True

    def finish(self):
        with self.lock:
            self.revision += 1
            self.pending = None
            self.busy = False
            self.armed = self.saw_key = False

    def close(self):
        with self.lock:
            self.closed = True
            self.pending = None
            self.revision += 1


class FocusReader:
    """Owned exclusively by the observer thread; no worker COM pointers."""
    def __init__(self):
        import ctypes as C
        from ctypes import wintypes as W
        from .direct_uia import Automation
        self.automation = Automation()
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        self.kernel.GetTickCount64.restype = C.c_uint64
        self.kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        self.kernel.OpenProcess.restype = W.HANDLE
        self.kernel.IsWow64Process.argtypes = [W.HANDLE, C.POINTER(W.BOOL)]
        self.kernel.CloseHandle.argtypes = [W.HANDLE]
        self.C, self.W = C, W
        self.checked = None
        self.last_focus = None

    def now(self):
        return self.kernel.GetTickCount64()

    def read(self):
        from .direct_win32 import window_identity
        from .direct_uia import property_values
        from .winapi import user
        target = window_identity()
        if target[2] == os.getpid():
            return None
        client = self.automation.client
        focused = client.GetFocusedElement()
        if not focused:
            return None
        metadata = property_values(self.automation, focused,
                                   ("IsPassword", "IsEnabled", "HasKeyboardFocus"))
        if metadata["IsPassword"] or not metadata["IsEnabled"] or not metadata["HasKeyboardFocus"]:
            return None
        runtime = tuple(focused.GetRuntimeId())
        if (self.last_focus is not None and self.last_focus.target == target
                and self.last_focus.runtime == runtime):
            return self.last_focus
        element = focused
        for _ in range(8):
            pattern = self.automation.pattern(element, "TextPattern")
            if pattern is not None:
                value = self.automation.pattern(element, "ValuePattern")
                if value is not None and value.CurrentIsReadOnly:
                    return None
                readonly = pattern.DocumentRange.GetAttributeValue(self.automation.types.UIA_IsReadOnlyAttributeId)
                if isinstance(readonly, (bool, int)) and readonly:
                    return None
                break
            element = client.ControlViewWalker.GetParentElement(element)
            if not element:
                return None
        else:
            return None
        if not runtime or window_identity() != target:
            return None
        if self.checked != target[2]:
            handle = self.kernel.OpenProcess(0x1000, False, target[2])
            wow64 = self.W.BOOL()
            try:
                if not handle or not self.kernel.IsWow64Process(handle, self.C.byref(wow64)) or wow64.value:
                    return None
            finally:
                if handle:
                    self.kernel.CloseHandle(handle)
            self.checked = target[2]
        tid = user.GetWindowThreadProcessId(target[1], None)
        if not tid or window_identity() != target:
            return None
        self.last_focus = Focus(target, runtime, tid)
        return self.last_focus


class AutoImeMonitor:
    def __init__(self, config, wake, reader_factory=FocusReader, probe_factory=NativeProbe):
        self.config, self.wake = config, wake
        self.reader_factory, self.probe_factory = reader_factory, probe_factory
        self.state = CommitState()
        self.stopping = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.reader = None
        self.thread = threading.Thread(target=self._run, name="auto-ime-tsf", daemon=True)

    def start(self):
        # comtypes initializes COM on the thread of its first import and installs
        # a main-thread shutdown hook. Import it here, then explicitly initialize
        # the observer's own apartment in _run.
        import sys
        sys.coinit_flags = 0
        logging.getLogger("comtypes").setLevel(logging.WARNING)
        try:
            import comtypes  # noqa: F401
        except ImportError as exc:
            raise RuntimeError('TSF監視にはcomtypesが必要です: pip install -e ".[desktop]"') from exc
        self.thread.start()
        if not self.ready.wait(5):
            self.close()
            raise RuntimeError("TSF自動変換監視の初期化がタイムアウトしました")
        if self.error is not None:
            self.close()
            raise RuntimeError(f"TSF自動変換監視を開始できません: {self.error}") from self.error
        return self

    def take(self):
        return self.state.take(tick_count())

    def valid(self, commit):
        return self.thread.is_alive() and self.state.valid(commit)

    def begin(self, commit=None):
        return self.state.begin(commit)

    def finish(self):
        self.state.finish()

    def _run(self):
        probe = None
        initialized = False
        try:
            import sys
            sys.coinit_flags = 0
            logging.getLogger("comtypes").setLevel(logging.WARNING)
            import comtypes
            comtypes.CoInitializeEx(0)
            initialized = True
            # Fail startup for a missing/wrong-architecture DLL, not silently
            # after the user has enabled the setting.
            native = self.probe_factory(0, self.config.tsf_monitor_path or None)
            self.reader = self.reader_factory()
            self.ready.set()
            field = rejected = None
            retry_at = 0
            attached_at = 0
            attached = False
            while not self.stopping.is_set():
                try:
                    current = self.reader.read()
                except Exception:
                    current = None
                    logger.debug("TSF自動変換: フォーカス取得不可", exc_info=True)
                self.state.track(current, self.reader.now())
                if current != field:
                    if probe is not None and (current is None or field is None or current.tid != field.tid):
                        clean = probe.close()
                        probe = None
                        if not clean:
                            raise RuntimeError("入力先変更時のTSF監視解除を確認できません。自動変換を停止しました")
                    # Same GUI thread: retain its subscription, discard the old
                    # field's queued notices and keep the new UIA identity.
                    if probe is not None:
                        probe.drain()
                    field = current
                now = self.reader.now()
                if field is not None and probe is None and (field != rejected or now >= retry_at):
                    try:
                        native.tid = field.tid
                        probe = native.start()
                        attached_at, attached = now, False
                        logger.debug("TSF自動変換: 監視開始 pid=%s tid=%s", field.target[2], field.tid)
                    except Exception as exc:
                        rejected, retry_at = field, now + 5000
                        logger.debug("TSF自動変換: この入力先の監視を開始できません: %s", exc)
                if probe is not None:
                    events = probe.drain()
                    attached |= any(event["kind"] == 1 for event in events)
                    for event in events:
                        if event["kind"] in (1, 2, 4, 5, 6, 9):
                            logger.debug("TSF自動変換: %s pid=%s tid=%s key=%s hr=0x%08x chars=%s",
                                         KINDS.get(event["kind"]), event["pid"], event["tid"], event["key"],
                                         event["hr"], len(event["text"]))
                    if probe.dropped():
                        raise RuntimeError("TSF通知が欠落したため自動変換を停止しました")
                    self.state.observe(events, now)
                    if events:
                        self.wake()
                    if not attached and now - attached_at >= 3000:
                        clean = probe.close()
                        probe = None
                        self.state.track(None)
                        rejected, retry_at = field, now + 5000
                        if not clean:
                            raise RuntimeError("TSF監視の起動失敗後に解除を確認できません")
                        logger.debug("TSF自動変換: この入力先にTSF監視を登録できません")
                self.stopping.wait(0.03)
        except Exception as exc:
            self.error = exc
            self.state.close()
            if self.ready.is_set():
                logger.warning("自動変換監視を停止しました: %s（手動ショートカットは使用できます）", exc)
                self.wake()
        finally:
            self.state.close()
            try:
                if probe is not None and not probe.close():
                    logger.warning("終了時のTSF監視解除を確認できませんでした")
            except Exception:
                logger.warning("終了時のTSF監視解除に失敗しました", exc_info=True)
            self.ready.set()
            self.reader = None
            if initialized:
                comtypes.CoUninitialize()

    def close(self):
        self.state.close()
        self.stopping.set()
        if self.thread.ident is not None:
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                logger.warning("TSF監視スレッドの終了を確認できませんでした")


def capture_commit(desktop, monitor, commit):
    """Wait for the real editor snapshot; retries are read-only."""
    deadline = time.monotonic() + 0.7
    while True:
        if not monitor.valid(commit):
            raise RuntimeError("IME確定後に入力・フォーカスが変わったため中止しました")
        try:
            return desktop.capture_commit(commit.text, commit.focus.target, commit.focus.runtime)
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.025)
