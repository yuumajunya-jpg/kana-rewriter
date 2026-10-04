"""Summarize --timings logs without starting desktop automation."""
import argparse
import json
import math
from pathlib import Path
import re
import statistics

FIELDS = ("取得", "AI", "適用", "AI以外", "合計")
SUMMARY = re.compile(r"時間集計: " + r" / ".join(
    re.escape(field) + r"=([0-9.]+)ms" for field in FIELDS))


def summarize(text):
    rows = [tuple(map(float, match.groups())) for match in SUMMARY.finditer(text)]
    result = {"completed_samples": len(rows), "aborted": text.count("中止:"), "timings_ms": {}}
    for index, field in enumerate(FIELDS):
        values = sorted(row[index] for row in rows)
        if values:
            result["timings_ms"][field] = {
                "median": round(statistics.median(values), 2),
                "p95_nearest_rank": round(values[math.ceil(0.95 * len(values)) - 1], 2),
                "min": min(values), "max": max(values),
            }
    return result


def read_log(path):
    payload = path.read_bytes()
    if payload.startswith((b"\xff\xfe", b"\xfe\xff")):
        return payload.decode("utf-16")
    try:
        return payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return payload.decode("cp932")


def main():
    parser = argparse.ArgumentParser(description="--timingsログの中央値・p95を集計")
    parser.add_argument("logs", nargs="+", type=Path)
    args = parser.parse_args()
    for path in args.logs:
        print(json.dumps({"file": str(path), **summarize(read_log(path))}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
