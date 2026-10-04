"""Compare SendInput hook overhead without delivering text to any application.

A guard hook consumes ONLY this benchmark's tagged events in both variants.
The production monitor is installed after the guard, so it sees these events
before forwarding them to the guard. No focus changes or actual typing occur.
This measures input dispatch overhead, not browser rendering or UIA latency.
"""
import ctypes as C
from ctypes import wintypes as W
import json
from pathlib import Path
import statistics
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kana_rewriter.input_activity import InputActivity, user
from kana_rewriter.direct_win32 import unicode_events
from kana_rewriter.winapi import send

TAG = 0x4B524254


class KeyEvent(C.Structure):
    _fields_ = [("vk", W.DWORD), ("scan", W.DWORD), ("flags", W.DWORD),
                ("time", W.DWORD), ("extra", C.c_size_t)]


class Guard(InputActivity):
    def __init__(self):
        super().__init__()
        self.seen = 0
        self.done = threading.Event()

    def _keyboard(self, code, message, data):
        if code == 0 and C.cast(data, C.POINTER(KeyEvent)).contents.extra == TAG:
            self.seen += 1
            if self.seen == 50:
                self.done.set()
            return 1  # Never forward the benchmark's input to an application.
        return user.CallNextHookEx(None, code, message, data)


def main():
    events = unicode_events("最近雨続きで運動していないので、散歩でもしようかな")
    assert len(events) == 50
    for event in events:
        event.extra = TAG
    samples = {"guard_only": [], "guard_and_production_monitor": []}
    guard = Guard().start()
    try:
        # ABBA blocks help expose drift; each block discards two warmup batches.
        for monitored in (False, True, True, False):
            monitor = InputActivity().start() if monitored else None
            try:
                for trial in range(22):
                    guard.snapshot()  # fail closed if the hook thread stopped
                    guard.seen = 0
                    guard.done.clear()
                    before = time.perf_counter()
                    send(events)
                    if not guard.done.wait(2) or guard.seen != 50:
                        raise RuntimeError("Guard did not observe all 50 events; stop benchmark")
                    elapsed = (time.perf_counter() - before) * 1000
                    if trial >= 2:
                        key = "guard_and_production_monitor" if monitored else "guard_only"
                        samples[key].append(elapsed)
            finally:
                if monitor is not None:
                    monitor.close()
    finally:
        guard.close()
    summary = {}
    for key, values in samples.items():
        ordered = sorted(values)
        summary[key] = {"batches": len(values), "median_ms": round(statistics.median(values), 3),
                        "p95_ms": round(ordered[int(0.95 * (len(ordered) - 1))], 3)}
    summary["median_added_ms"] = round(summary["guard_and_production_monitor"]["median_ms"]
                                        - summary["guard_only"]["median_ms"], 3)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
