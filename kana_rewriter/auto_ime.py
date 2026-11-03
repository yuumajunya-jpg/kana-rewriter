"""Follow editable focus and deliver TSF commits to the normal message loop."""
import logging
import os
import threading
import time
from collections import Counter

from .tsf import NativeProbe, KINDS
from .commit_state import CommitState, Commit, Focus, FOCUS_RETRY_MS, invalidates_commit

logger = logging.getLogger(__name__)


def tick_count():
    import ctypes as C
    kernel = C.WinDLL("kernel32")
    kernel.GetTickCount64.restype = C.c_uint64
    return kernel.GetTickCount64()


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
        from .direct_uia import property_values, text_provider
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
        element, pattern = text_provider(self.automation, focused)
        runtime = tuple(element.GetRuntimeId())
        value = self.automation.pattern(element, "ValuePattern")
        if value is not None and value.CurrentIsReadOnly:
            return None
        readonly = pattern.DocumentRange.GetAttributeValue(self.automation.types.UIA_IsReadOnlyAttributeId)
        if isinstance(readonly, (bool, int)) and readonly:
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
        self.state = CommitState(native_context=config.ime_commit_backend == 'tsf')
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
        return self.thread.is_alive() and self.state.valid(commit, FOCUS_RETRY_MS / 1000 + 0.05)

    def begin(self, commit=None):
        return self.state.begin(commit)

    def finish(self):
        self.state.finish(tick_count())

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
            last_drain_at = time.perf_counter()
            while not self.stopping.is_set():
                focus_started = time.perf_counter()
                try:
                    current = self.reader.read()
                except Exception:
                    current = self.state.focus_failed(self.reader.now())
                    logger.debug("TSF自動変換: フォーカス取得不可", exc_info=True)
                else:
                    if self.state.track(current, self.reader.now()):
                        self.wake()
                focus_seconds = time.perf_counter() - focus_started
                if not self.state.same_focus(current, field):
                    if probe is not None and (self.state.native_context or current is None
                                              or field is None or current.tid != field.tid):
                        clean = probe.close()
                        probe = None
                        if not clean:
                            raise RuntimeError("入力先変更時のTSF監視解除を確認できません。自動変換を停止しました")
                    # A real target change invalidates queued notices. For TSF
                    # edits, RuntimeId-only changes are NOT target changes:
                    # process their context/commit notifications in order below.
                    if probe is not None:
                        discarded = probe.drain()
                        logger.debug("TSF入力先変更時の通知破棄: old=%r / new=%r / events=%s",
                                     field, current, [(e['kind'], e['tick'], e['composition_tick'],
                                                       len(e['text'])) for e in discarded])
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
                    drain_started = time.perf_counter()
                    events = probe.drain()
                    drained_at = time.perf_counter()
                    drain_interval = drained_at - last_drain_at
                    last_drain_at = drained_at
                    attached |= any(event["kind"] == 1 for event in events)
                    for event in events:
                        if event["kind"] in (1, 2, 4, 5, 6, 9):
                            logger.debug("TSF自動変換: %s pid=%s tid=%s key=%s mods=%s hr=0x%08x chars=%s generation=%s",
                                         KINDS.get(event["kind"]), event["pid"], event["tid"], event["key"],
                                         event["modifiers"], event["hr"], len(event["text"]), event['generation'])
                    dropped = probe.dropped()
                    if dropped:
                        # Do not consume any part of a batch with missing history.
                        # A fresh wrapper also leaves previously prepared edits
                        # pointing to a stopped probe, never the restarted one.
                        self.state.resync(now)
                        self.wake()
                        old_probe, probe = probe, None
                        if not old_probe.close():
                            raise RuntimeError("TSF通知欠落後に古い監視の解除を確認できませんでした")
                        native = self.probe_factory(0, self.config.tsf_monitor_path or None)
                        counts = Counter(KINDS.get(event['kind'], str(event['kind'])) for event in events)
                        ticks = [event['tick'] for event in events]
                        logger.warning(
                            "TSF通知が%s件欠落: 今回の変換を破棄し、監視を再接続します "
                            "(回収=%s件 / 内訳=%s / 通知期間=%sms / UIA取得=%.1fms / "
                            "回収処理=%.1fms / 回収間隔=%.1fms)",
                            dropped, len(events), dict(counts), max(ticks)-min(ticks) if ticks else 0,
                            focus_seconds*1000, (drained_at-drain_started)*1000, drain_interval*1000)
                        continue
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
