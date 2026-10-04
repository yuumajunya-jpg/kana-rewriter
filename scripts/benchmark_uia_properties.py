"""Compare live versus batched properties on our own hidden Edit control.

No focus change or synthetic input. Requires the repository's test fixtures.
"""
import json
import faulthandler
import multiprocessing
from pathlib import Path
import statistics
import sys
import time

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "tests"))
from test_direct import native_fixture
from kana_rewriter.direct_uia import Automation, property_values


def main():
    faulthandler.dump_traceback_later(60, exit=True)
    context = multiprocessing.get_context("spawn")
    parent, child = context.Pipe()
    process = context.Process(target=native_fixture, args=(child, False))
    process.start()
    child.close()
    try:
        if not parent.poll(10):
            raise RuntimeError("Fixture did not start")
        automation = Automation()
        element = automation.client.ElementFromHandle(parent.recv())
        print("Fixture connected", flush=True)
        result = {}
        safety = property_values(automation, element, ("IsPassword", "IsEnabled", "HasKeyboardFocus"))
        assert safety == {"IsPassword": False, "IsEnabled": True, "HasKeyboardFocus": False}
        for name, names in (("state", ("IsPassword", "IsEnabled", "HasKeyboardFocus")),
                            ("metadata_without_name", ("FrameworkId", "ClassName", "ControlType"))):
            # Require the actual cache API to work; no silent benchmark fallback.
            cached = element.BuildUpdatedCache(automation.cache_request(names))
            print(f"{name}: cache available", flush=True)
            expected = {n: getattr(element, "Current" + n) for n in names}
            assert {n: getattr(cached, "Cached" + n) for n in names} == expected
            samples = {"individual": [], "batch": []}
            for mode in ("individual", "batch", "batch", "individual"):
                for trial in range(22):
                    before = time.perf_counter()
                    values = ({n: getattr(element, "Current" + n) for n in names} if mode == "individual"
                              else property_values(automation, element, names))
                    elapsed = (time.perf_counter() - before) * 1000
                    assert values == expected
                    if trial >= 2:
                        samples[mode].append(elapsed)
                print(f"{name}/{mode}: block complete", flush=True)
            result[name] = {mode: {"samples": len(values),
                                  "median_ms": round(statistics.median(values), 3)}
                            for mode, values in samples.items()}
        print(json.dumps(result, indent=2))
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
        faulthandler.cancel_dump_traceback_later()


if __name__ == "__main__":
    main()
