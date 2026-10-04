"""ABBA read comparison on the same hidden fixture; never sends input."""
import argparse
import json
import math
import multiprocessing
from pathlib import Path
import statistics
import subprocess
import sys
import time
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from test_direct import native_fixture
from kana_rewriter.core import Config
from kana_rewriter.direct_win32 import NativeEditor
from kana_rewriter.direct_uia import Automation, AutomationEditor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=40)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 4 <= args.samples <= 1000 or args.samples % 2:
        parser.error("--samples must be even, from 4 to 1000")
    exe = ROOT / "build" / "native" / "kana-editor-worker.exe"
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=native_fixture, args=(child, True))
    process.start()
    child.close()
    result = {"scope": "Read-only hidden RichEdit, same HWND, ABBA; no focus checks or desktop editing",
              "uia_wait": "Fixture message pump includes a 5 ms polling interval", "results": {}}
    try:
        if not parent.poll(10):
            raise RuntimeError("Fixture did not start")
        hwnd = parent.recv()
        config = Config()
        win32 = NativeEditor(hwnd, config)
        automation = Automation()
        element = automation.client.ElementFromHandle(hwnd)
        pattern = automation.pattern(element, "TextPattern")
        uia = AutomationEditor.__new__(AutomationEditor)
        uia.automation, uia.element, uia.pattern = automation, element, pattern
        uia.limit = config.max_document_chars
        uia.check_focus = Mock()
        for backend, editor in (("win32", win32), ("uia", uia)):
            samples = {"python": [], "native": []}
            expected = editor.read()
            for mode in ("python", "native", "native", "python"):
                if mode == "native":
                    completed = subprocess.run([str(exe), "--benchmark-read", str(hwnd), backend,
                                                str(args.samples // 2)], capture_output=True, text=True,
                                               timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
                    if completed.returncode:
                        raise RuntimeError(completed.stderr)
                    samples[mode].extend(int(line) / 1000 for line in completed.stdout.splitlines())
                else:
                    for trial in range(args.samples // 2 + 2):
                        before = time.perf_counter()
                        actual = editor.read()
                        elapsed = (time.perf_counter() - before) * 1000
                        if actual != expected:
                            raise RuntimeError("Fixture changed")
                        if trial >= 2:
                            samples[mode].append(elapsed)
            result["results"][backend] = {
                mode: {"samples": len(values), "median_ms": round(statistics.median(values), 3),
                       "p95_ms": round(sorted(values)[math.ceil(len(values) * .95) - 1], 3)}
                for mode, values in samples.items()}
        rendered = json.dumps(result, ensure_ascii=False, indent=2)
        print(rendered)
        if args.output:
            args.output.write_text(rendered + "\n", encoding="utf-8")
    finally:
        try:
            parent.send("stop")
        except (BrokenPipeError, OSError):
            pass
        process.join(5)
        if process.is_alive():
            process.terminate()
            process.join()
        parent.close()


if __name__ == "__main__":
    main()
