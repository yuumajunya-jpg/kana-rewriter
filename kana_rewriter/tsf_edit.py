"""TSF commit-range editor, isolated from the read-only monitor and UIA writes."""
import ctypes as C
from ctypes import wintypes as W
import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
import sys
import threading

from .tsf import Packet

logger = logging.getLogger(__name__)

DIAGNOSTICS = {
    0: 'none', 1: 'stopped', 2: 'range_missing', 3: 'context_changed',
    4: 'commit_changed', 5: 'revision_changed', 6: 'focus_changed',
    7: 'expired', 8: 'foreground_changed', 9: 'active_composition', 10: 'source_mismatch',
    101: 'key', 102: 'context', 103: 'text_or_composition_update',
    104: 'selection_moved', 105: 'selection_style_changed', 106: 'inspection_failed',
    107: 'previous_apply_completed', 108: 'selection_snapshot_failed',
    109: 'text_updated', 110: 'composition_attribute_active',
    111: 'new_composition', 112: 'range_text_changed',
}


def diagnostic_reason(value):
    if not value:
        return 'unavailable'
    return (f"{DIAGNOSTICS.get(value & 0xffff, str(value & 0xffff))} / "
            f"invalidated_by={DIAGNOSTICS.get(value >> 16, str(value >> 16))}")


class EditPacket(C.Structure):
    _fields_ = Packet._fields_ + [("commit_id", C.c_uint64), ("revision", C.c_uint64)]


class EditResult(C.Structure):
    _fields_ = [("hr", W.DWORD), ("mutated", W.DWORD), ("state", W.DWORD)]


def edit_probe_path():
    directory = Path(__file__).resolve().parents[1] / "build/tsf-edit-probe"
    manifest = directory / "latest.json"
    if not manifest.is_file():
        raise RuntimeError("TSF編集DLLがありません。scripts/build_tsf_edit_probe.ps1を実行してください")
    name = json.loads(manifest.read_text(encoding="utf-8-sig"))["dll"]
    if not isinstance(name, str) or Path(name).name != name or not name.endswith(".dll"):
        raise RuntimeError("TSF編集DLLのマニフェストが不正です")
    return directory / name


class NativeEditProbe:
    def __init__(self, tid, dll=None):
        if sys.platform != "win32" or C.sizeof(C.c_void_p) != 8:
            raise RuntimeError("TSF直接編集は64ビットWindows専用です")
        path = Path(dll) if dll else edit_probe_path()
        self.dll = C.WinDLL(str(path.resolve()), use_last_error=True)
        self.dll.ProbeProtocolVersion.restype = W.DWORD
        if self.dll.ProbeProtocolVersion() != 101:
            raise RuntimeError("監視専用DLLは使えません。TSF編集DLLをビルドしてください")
        self.dll.StartProbe.argtypes = [W.DWORD]
        self.dll.StartProbe.restype = W.BOOL
        self.dll.PollProbe.argtypes = [C.POINTER(EditPacket)]
        self.dll.PollProbe.restype = W.BOOL
        self.dll.DroppedProbe.restype = W.DWORD
        self.dll.StopProbe.restype = W.BOOL
        self.dll.ReplaceCommit.argtypes = [C.c_uint64, C.c_uint64, C.c_wchar_p,
                                          C.c_wchar_p, W.DWORD, C.POINTER(EditResult)]
        self.dll.ReplaceCommit.restype = W.BOOL
        self.last_diagnostic = getattr(self.dll, 'LastEditDiagnostic', None)
        if self.last_diagnostic is not None:
            self.last_diagnostic.restype = W.DWORD
        logger.debug('TSF編集DLL: path=%s / protocol=101 / detailed_diagnostics=%s',
                     path.resolve(), self.last_diagnostic is not None)
        self.tid, self.started = tid, False
        self.lock = threading.RLock()
        self.last_candidate = None

    def start(self):
        with self.lock:
            if not self.dll.StartProbe(self.tid):
                raise C.WinError(C.get_last_error())
            self.started = True
        return self

    def drain(self):
        with self.lock:
            events, packet = [], EditPacket()
            while self.dll.PollProbe(C.byref(packet)):
                event = {name: getattr(packet, name) for name, _ in EditPacket._fields_}
                events.append(event)
                if event["kind"] == 4:
                    self.last_candidate = event.copy()
            return events

    def dropped(self):
        with self.lock:
            return self.dll.DroppedProbe()

    def replace(self, candidate, expected, replacement, timeout_ms=1500):
        if (not expected or "\0" in expected or "\0" in replacement or
                len(expected.encode("utf-16-le")) // 2 >= 1024 or
                len(replacement.encode("utf-16-le")) // 2 >= 1024):
            raise ValueError("TSF編集の文字列はNULを含まず、UTF-16で1024単位未満にしてください")
        if not 1 <= timeout_ms <= 10000:
            raise ValueError("timeout_ms must be between 1 and 10000")
        with self.lock:
            if not self.started:
                raise RuntimeError("TSF編集監視は停止しています")
            result = EditResult()
            if not self.dll.ReplaceCommit(candidate["commit_id"], candidate["revision"], expected,
                                          replacement, timeout_ms, C.byref(result)):
                raise C.WinError(C.get_last_error())
            return {"hr": result.hr, "mutated": bool(result.mutated), "state": result.state,
                    "success": result.hr == 0 and result.state == 3,
                    "diagnostic": self.last_diagnostic() if self.last_diagnostic is not None and result.state == 3 else 0}

    def close(self):
        with self.lock:
            if not self.started:
                return True
            self.started = False
            return bool(self.dll.StopProbe())


class RestartRequired(RuntimeError):
    pass


@dataclass(frozen=True)
class PreparedEdit:
    probe: object
    event: dict
    target: tuple


class TsfCommitEditor:
    def __init__(self, path=''):
        self.path = Path(path) if path else edit_probe_path()
        self.probe = None

    def probe_factory(self, tid, ignored_monitor_path=None):
        self.probe = NativeEditProbe(tid, self.path)
        return self.probe

    def prepare(self, commit):
        probe = self.probe
        if not commit.native_range or probe is None or not probe.started or probe.tid != commit.focus.tid:
            raise RuntimeError('この確定通知のTSF編集範囲を確認できません')
        identity, revision, text = commit.native_range
        if text.replace('\r\n', '\n').replace('\r', '\n') != commit.text:
            raise RuntimeError('TSF範囲と確定通知の本文が一致しません')
        return PreparedEdit(probe, dict(commit_id=identity, revision=revision, text=text), commit.focus.target)

    def apply(self, prepared, captured, result):
        return apply_commit(prepared.probe, prepared.event, captured, result, target=prepared.target) is not None


def apply_commit(probe, event, captured, result, target=None):
    """One exact retained range. Caller checks focus; native revalidates edits."""
    if event['text'].replace('\r\n', '\n').replace('\r', '\n') != captured.source:
        raise RuntimeError('TSF確定範囲と取得した対象が一致しません')
    if result == captured.source:
        return None
    if target is not None:
        from .direct_win32 import window_identity
        if window_identity() != target:
            raise RuntimeError('適用直前に入力先が変わったため中止しました')
    started = time.perf_counter()
    logger.debug('TSF置換内容: 対象=%r / 結果=%r', captured.source, result)
    # Preserve native line endings in the expected range. The converter sees
    # normalized text, while TSF must verify the original UTF-16 contents.
    reply = probe.replace(event, event['text'], result)
    reason = diagnostic_reason(reply.get('diagnostic', 0))
    logger.debug('TSF直接編集: commit=%s revision=%s / hr=0x%08x mutated=%s state=%s / %.3fms',
                 event['commit_id'], event['revision'], reply['hr'], reply['mutated'],
                 reply['state'], (time.perf_counter() - started) * 1000)
    if not reply['success']:
        logger.debug('TSF適用拒否の詳細: %s', reason)
        suffix = '編集済み、または結果不明です。本文を確認してください' if reply['mutated'] else '書き込み前に中止しました'
        if reply['mutated'] or reply['state'] != 3:
            raise RestartRequired(f"TSF直接編集 hr=0x{reply['hr']:08x}: {suffix}。再送はしません。停止するので再起動してください")
        raise RuntimeError(f"TSF直接編集 hr=0x{reply['hr']:08x}: {suffix}。再送はしません（理由: {reason}）")
    return reply

