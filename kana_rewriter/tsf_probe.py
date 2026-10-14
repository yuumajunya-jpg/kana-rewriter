"""Opt-in TSF experiment; a fixed target thread, no production hotkeys."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes as C
from ctypes import wintypes as W
from dataclasses import replace
import json
import logging
from pathlib import Path
import sys
import time

from .core import Config, Converter, apply_result
from .text import reading_spans

KINDS = {1: "attached", 2: "context", 3: "editing", 4: "commit_candidate",
         5: "canceled_or_empty", 6: "error", 7: "detached", 8: "key", 9: "unsupported"}


class Packet(C.Structure):
    _fields_ = [(name, C.c_uint32) for name in ("kind", "pid", "tid", "hr", "key", "active")] + [
        (name, C.c_uint64) for name in ("tick", "hwnd", "generation")] + [("text", C.c_wchar * 1024)]


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
            raise RuntimeError("TSF試験は64ビットWindows/Python専用です")
        path = Path(dll) if dll else probe_path()
        if not path.is_file():
            raise RuntimeError("TSF監視DLLがありません。scripts/build_tsf_probe.ps1を実行してください")
        self.dll = C.WinDLL(str(path.resolve()), use_last_error=True)
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
    """A conservative trial gate; not a general composition classifier."""
    if event["kind"] != 4:
        return False, "not_candidate"
    if event["key"] != 0x0D:
        return False, "enter_not_observed"
    if event["pid"] != target[2] or event["hwnd"] != target[1]:
        return False, "focus_changed"
    if not 0 <= now - event["tick"] <= 1500:
        return False, "expired"
    if not event["text"] or not any(reading_spans(event["text"])):
        return False, "no_kana"
    return True, "eligible"


def invalidates(events):
    return any(event["kind"] in (2, 3, 4, 5, 6, 7, 8, 9) for event in events)


def main():
    parser = argparse.ArgumentParser(description="対象アプリ内TSF監視の実験（既定は文字変更なし）")
    parser.add_argument("--config", default="config.toml")
    parser.add_argument("--auto-replace", action="store_true", help="Enterを伴う確定候補で既存の置換処理を試す")
    parser.add_argument("--fixed-source", help="AIなし試験用の確定文字列")
    parser.add_argument("--fixed-result", help="AIなし試験用の置換結果")
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--focus-delay", type=float, default=5)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.seconds <= 600 or not 1 <= args.focus_delay <= 30:
        parser.error("secondsは1〜600、focus-delayは1〜30です")
    if (args.fixed_source is None) != (args.fixed_result is None) or args.fixed_source == "" or args.fixed_result == "":
        parser.error("fixed-sourceとfixed-resultは空でない文字列を両方指定してください")
    if args.fixed_source is not None and not args.auto_replace:
        parser.error("固定結果試験には--auto-replaceが必要です")
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING, format="[診断] %(message)s")
    logging.getLogger("comtypes").setLevel(logging.WARNING)
    if sys.platform != "win32":
        parser.error("Windows専用です")
    from .direct_win32 import window_identity
    from .direct import Desktop
    from .winapi import user
    kernel = C.WinDLL("kernel32", use_last_error=True)
    kernel.GetTickCount64.restype = C.c_uint64
    kernel.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    kernel.OpenProcess.restype = W.HANDLE
    kernel.IsWow64Process.argtypes = [W.HANDLE, C.POINTER(W.BOOL)]
    kernel.CloseHandle.argtypes = [W.HANDLE]
    desktop = probe = pool = None
    pending = None

    def output(event):
        data = dict(event)
        if "kind" in data:
            data["event"] = KINDS.get(data.pop("kind"), "unknown")
            data["chars"] = len(data.pop("text"))
            data["hr"] = f"0x{data['hr']:08x}"
        print("[TSF試験] " + json.dumps(data, ensure_ascii=False), flush=True)

    try:
        converter = None
        if args.auto_replace:
            config = replace(Config.load(args.config), editor_worker="python")
            if config.edit_backend == "clipboard":
                raise RuntimeError("自動置換試験は直接編集方式を使用してください")
            if args.fixed_source is not None:
                from .__main__ import FixedConverter
                converter = FixedConverter(args.fixed_source, args.fixed_result)
            else:
                converter = Converter(config)
                print("モデルを読み込んでいます…", flush=True)
                converter.load_model()
            desktop = Desktop(config, debug=args.debug)
            desktop.warmup()
            pool = ThreadPoolExecutor(max_workers=1)
        print(f"{args.focus_delay:g}秒以内に対象の入力欄へ移動してください。監視対象をそのスレッドに固定します。", flush=True)
        time.sleep(args.focus_delay)
        target = window_identity()
        runtime = desktop.focus_runtime() if desktop is not None else None
        process = kernel.OpenProcess(0x1000, False, target[2])
        wow64 = W.BOOL()
        try:
            if not process or not kernel.IsWow64Process(process, C.byref(wow64)) or wow64.value:
                raise RuntimeError("対象の64ビット確認に失敗したか、対象が32ビットです")
        finally:
            if process:
                kernel.CloseHandle(process)
        tid = user.GetWindowThreadProcessId(target[1], None)
        probe = NativeProbe(tid).start()
        output({"event": "started", "pid": target[2], "tid": tid, "auto_replace": args.auto_replace})
        deadline = time.monotonic() + args.seconds
        attached = False
        attachment_deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            events = probe.drain()
            for event in events:
                output(event)
                attached |= event["kind"] == 1
            if not attached and time.monotonic() >= attachment_deadline:
                raise RuntimeError("対象スレッドのTSF監視を登録できませんでした。error/contextのログを確認してください")
            if probe.dropped():
                raise RuntimeError("監視通知が欠落したため試験を中止しました")
            if pending is not None:
                future, captured, generation = pending
                if invalidates(events):
                    generation = None
                    pending = (future, captured, generation)
                if future.done():
                    pending = None
                    result = future.result()
                    if generation is None or invalidates(probe.drain()) or window_identity() != target:
                        output({"event": "aborted", "reason": "input_during_inference"})
                    else:
                        changed = apply_result(desktop, captured, result)
                        output({"event": "applied"} if changed else
                               {"event": "unchanged", "reason": "model_returned_original"})
                    # Ignore notifications caused by our own replacement.
                    for event in probe.drain():
                        output(event)
            elif args.auto_replace:
                candidates = [event for event in events if event["kind"] == 4]
                if candidates:
                    event = candidates[-1]
                    valid, reason = eligible(event, kernel.GetTickCount64(), window_identity())
                    candidate_index = max(index for index, item in enumerate(events) if item["kind"] == 4)
                    if invalidates(events[candidate_index + 1:]):
                        valid, reason = False, "input_after_candidate"
                    if window_identity() != target:
                        valid, reason = False, "target_changed"
                    if valid:
                        try:
                            # TSF may report before the renderer's UIA snapshot updates.
                            # Retry reads only; never replay a write.
                            capture_deadline = time.monotonic() + 0.7
                            while True:
                                try:
                                    captured = desktop.capture_commit(event["text"], target, runtime)
                                    break
                                except RuntimeError:
                                    if time.monotonic() >= capture_deadline or window_identity() != target:
                                        raise
                                    time.sleep(0.025)
                            if invalidates(probe.drain()):
                                raise RuntimeError("本文取得中にTSF入力が変わりました")
                            future = pool.submit(converter.convert_selection, captured.source,
                                                 captured.left_context, captured.right_context)
                            pending = (future, captured, event["generation"])
                            output({"event": "converting", "chars": len(captured.source)})
                        except Exception as exc:
                            output({"event": "aborted", "reason": str(exc)})
                    else:
                        output({"event": "skipped", "reason": reason})
            time.sleep(0.01)
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        output({"event": "failed", "reason": str(exc)})
        return 1
    finally:
        if probe is not None:
            output({"event": "stopped", "clean_detach": probe.close()})
        if pool is not None:
            pool.shutdown(wait=True, cancel_futures=True)
        if desktop is not None:
            desktop.close()


if __name__ == "__main__":
    raise SystemExit(main())
