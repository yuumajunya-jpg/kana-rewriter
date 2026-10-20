"""TSF observer transport and shared replacement policy (no command-line UI)."""
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import sys

from .text import reading_spans

KINDS = {1: "attached", 2: "context", 3: "editing", 4: "commit_candidate",
         5: "canceled_or_empty", 6: "error", 7: "detached", 8: "key", 9: "unsupported"}
INPUT_EVENTS = frozenset((2, 3, 4, 5, 6, 7, 8, 9))
COMMIT_MAX_AGE_MS = 1500
PROTOCOL_VERSION = 3
MODIFIER_CTRL = 1


class Packet(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in ("kind", "pid", "tid", "hr", "key", "active", "modifiers")] + [
        (name, C.c_uint64) for name in ("tick", "hwnd", "generation", "composition_tick")] + [("text", C.c_wchar * 1024)]


def probe_path():
    directory = Path(__file__).resolve().parents[1] / "build/tsf-probe"
    manifest = directory / "latest.json"
    if manifest.is_file():
        name = json.loads(manifest.read_text(encoding="utf-8-sig"))["dll"]
        if not isinstance(name, str) or Path(name).name != name or not name.endswith(".dll"):
            raise RuntimeError("TSF監視DLLのビルド情報が不正です")
        return directory / name
    return directory / "kana-tsf-probe.dll"


class NativeProbe:
    def __init__(self, tid, dll=None):
        if sys.platform != "win32" or C.sizeof(C.c_void_p) != 8:
            raise RuntimeError("TSF監視は64ビットWindows/Python専用です")
        path = Path(dll) if dll else probe_path()
        if not path.is_file():
            raise RuntimeError("TSF監視DLLがありません。scripts/build_tsf_probe.ps1を実行してください")
        self.dll = C.WinDLL(str(path.resolve()), use_last_error=True)
        version = getattr(self.dll, "ProbeProtocolVersion", None)
        if version is None:
            raise RuntimeError("TSF監視DLLが古い形式です。scripts/build_tsf_probe.ps1で再ビルドしてください")
        version.restype = W.DWORD
        if version() != PROTOCOL_VERSION:
            raise RuntimeError("TSF監視DLLの通知形式が非対応です。scripts/build_tsf_probe.ps1で再ビルドしてください")
        self.dll.StartProbe.argtypes = [W.DWORD]
        self.dll.StartProbe.restype = W.BOOL
        self.dll.PollProbe.argtypes = [C.POINTER(Packet)]
        self.dll.PollProbe.restype = W.BOOL
        self.dll.DroppedProbe.restype = W.DWORD
        self.dll.StopProbe.restype = W.BOOL
        self.tid, self.started = tid, False

    def start(self):
        if not self.dll.StartProbe(self.tid):
            raise C.WinError(C.get_last_error())
        self.started = True
        return self

    def drain(self):
        events = []
        packet = Packet()
        while self.dll.PollProbe(C.byref(packet)):
            events.append({name: getattr(packet, name) for name, _ in Packet._fields_})
        return events

    def dropped(self):
        return self.dll.DroppedProbe()

    def close(self):
        if self.started:
            self.started = False
            return bool(self.dll.StopProbe())
        return True


def eligible(event, now, target):
    """Application policy: plain Enter commits containing kana; Ctrl+Enter is
    a user-requested commit without replacement."""
    if event["kind"] != 4:
        return False, "not_candidate"
    if event["key"] != 0x0D:
        return False, "enter_not_observed"
    # Ctrl+Enter is an IME commit too, but the user uses it to keep the
    # committed reading unchanged.
    if event["modifiers"] & MODIFIER_CTRL:
        return False, "ctrl_confirm"
    if event["pid"] != target[2] or event["hwnd"] != target[1]:
        return False, "focus_changed"
    if not 0 <= now - event["tick"] <= COMMIT_MAX_AGE_MS:
        return False, "expired"
    if not event["text"] or not any(reading_spans(event["text"])):
        return False, "no_kana"
    return True, "eligible"


def invalidates(events):
    return any(event["kind"] in INPUT_EVENTS for event in events)
