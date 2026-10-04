"""Deterministic IPC peer for native adapter failure tests; no desktop APIs."""
import struct
import sys
import time

from kana_rewriter.native import Reader, Writer, MAGIC, read_frame


def main():
    mode = sys.argv[1]
    token = 0
    while True:
        reader = Reader(read_frame(sys.stdin.buffer))
        command = reader.number()
        payload = Writer()
        message = ""
        if command == 1:
            if mode == "hang":
                time.sleep(30)
                return
            if mode == "bad_header":
                sys.stdout.buffer.write(b"NOPE" + struct.pack("<I", 0))
                sys.stdout.buffer.flush()
                return
        elif command == 3:
            token += 1
            if mode == "truncated_capture":
                payload.number(token)
            else:
                for value in (token, 1, 2, 3, 10):
                    payload.number(value)
                payload.text("前😀。さんぽ。後ろ")
                payload.number(7)
                payload.number(7)
        elif command == 4:
            supplied = reader.number()
            start, end, caret = (reader.number() for _ in range(3))
            text = reader.text()
            reader.done()
            if supplied != token or (start, end, caret, text) != (3, 6, 6, "散歩"):
                message = "Invalid edit plan"
            if mode == "apply_hang":
                time.sleep(30)
                return
            token = 0
        elif command == 5:
            payload.text("win32")
            for value in (10, 7, 7):
                payload.number(value)
        records = []
        if command == 2 and mode in ("metrics", "metrics_failure"):
            records = [("uia_readback_text_query", 1250), ("count:uia_readback_probes", 5),
                       ("count:uia_readback_confirmed", int(mode == "metrics"))]
            if mode == "metrics_failure":
                message = "Measured failure"
        response = Writer()
        response.number(not message)
        response.text(message)
        response.number(len(records))
        for name, value in records:
            response.text(name)
            response.number(value)
        response.data.extend(payload.data)
        sys.stdout.buffer.write(MAGIC + struct.pack("<I", len(response.data)) + response.data)
        sys.stdout.buffer.flush()
        if command == 3 and mode == "no_read_after_capture":
            time.sleep(30)
            return
        if command == 6:
            return


if __name__ == "__main__":
    main()
