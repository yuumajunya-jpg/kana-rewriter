"""Bounded, non-replaying IPC to the isolated C++ editor worker."""
import logging
from contextlib import contextmanager
from pathlib import Path
import queue
import struct
import subprocess
import threading

from .editor import TextState, make_capture, replacement_plan
from .timing import stage

MAX_FRAME = 16 * 1024 * 1024
MAGIC = b"KRN1"


class Writer:
    def __init__(self):
        self.data = bytearray()

    def number(self, value):
        self.data.extend(struct.pack("<Q", value))

    def text(self, value):
        payload = value.encode("utf-8")
        self.number(len(payload))
        self.data.extend(payload)


class Reader:
    def __init__(self, data):
        self.data = memoryview(data)
        self.offset = 0

    def take(self, size):
        if size < 0 or size > len(self.data) - self.offset:
            raise ValueError("C++ワーカーの応答が途中で切れています")
        result = self.data[self.offset:self.offset + size]
        self.offset += size
        return result

    def number(self):
        return struct.unpack("<Q", self.take(8))[0]

    def text(self):
        return bytes(self.take(self.number())).decode("utf-8")

    def done(self):
        if self.offset != len(self.data):
            raise ValueError("C++ワーカーの応答に余分なデータがあります")


def read_exact(stream, size):
    result = bytearray()
    while len(result) < size:
        part = stream.read(size - len(result))
        if not part:
            raise EOFError("C++編集ワーカーが終了しました")
        result.extend(part)
    return bytes(result)


def read_frame(stream):
    header = read_exact(stream, 8)
    if header[:4] != MAGIC:
        raise ValueError("C++ワーカーのプロトコルが一致しません")
    size = struct.unpack("<I", header[4:])[0]
    if size > MAX_FRAME:
        raise ValueError("C++ワーカーの応答が大きすぎます")
    return read_exact(stream, size)


class Desktop:
    def __init__(self, config, debug=False, timings=False):
        self.config, self.debug, self.timings = config, debug, timings
        self.process = None
        self.failed = False
        self.saved = None
        self.responses = queue.Queue(maxsize=1)
        self.requests = queue.Queue(maxsize=1)
        self.stopping = threading.Event()
        self.reader_thread = None

    def _start(self):
        path = Path(self.config.native_worker_path)
        if not path.is_file():
            raise RuntimeError(f"C++編集ワーカーがありません: {path}。scripts/build_native.ps1でビルドしてください")
        self.process = subprocess.Popen(
            [str(path.resolve())], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.reader_thread = threading.Thread(target=self._io_loop, args=(self.process,), daemon=True,
                                              name="native-editor-ipc")
        self.reader_thread.start()
        writer = Writer()
        writer.text(self.config.edit_backend)
        writer.text(self.config.uia_ime_check)
        writer.number(round(self.config.editor_timeout_seconds * 1000))
        writer.number(self.config.max_document_chars)
        writer.number(self.config.uia_readback_initial_delay_ms)
        writer.number(self.timings or self.debug)
        writer.text(self.config.native_uia_wait)
        try:
            reader = self._exchange(1, writer)
            reader.done()
        except (ValueError, RuntimeError) as exc:
            self.failed = True
            self.close()
            raise RuntimeError("C++ワーカーの初期応答を確認できません。編集は再送しません。再起動してください") from exc

    def _io_loop(self, process):
        try:
            while not self.stopping.is_set():
                try:
                    frame = self.requests.get(timeout=0.1)
                except queue.Empty:
                    continue
                # Both pipe writing and reading are covered by the caller's
                # response timeout, including a worker that never reads stdin.
                remaining = memoryview(frame)
                while remaining:
                    count = process.stdin.write(remaining)
                    if not count:
                        raise BrokenPipeError()
                    remaining = remaining[count:]
                self.responses.put(read_frame(process.stdout), timeout=0.1)
        except (OSError, EOFError, ValueError, queue.Full) as exc:
            if not self.stopping.is_set():
                try:
                    self.responses.put(exc, timeout=0.1)
                except queue.Full:
                    pass

    def _exchange(self, command, writer):
        if self.failed:
            raise RuntimeError("C++編集ワーカーが停止しています。プログラムを再起動してください")
        try:
            payload = struct.pack("<Q", command) + writer.data
            if len(payload) > MAX_FRAME:
                raise ValueError("C++ワーカーへの要求が大きすぎます")
            frame = MAGIC + struct.pack("<I", len(payload)) + payload
            self.requests.put(frame, timeout=self.config.editor_timeout_seconds + 3)
            packet = self.responses.get(timeout=self.config.editor_timeout_seconds + 3)
            if isinstance(packet, Exception):
                raise packet
            reader = Reader(packet)
            success, message = reader.number(), reader.text()
            count = reader.number()
            if count > 100000:
                raise ValueError("C++ワーカーの計測レコードが多すぎます")
            for _ in range(count):
                name, microseconds = reader.text(), reader.number()
                logging.getLogger("kana_rewriter.timing").debug(
                    "時間: C++/%s=%.1fms (%s)", name, microseconds / 1000,
                    "完了" if success else "中止")
            if success not in (0, 1):
                raise ValueError("C++ワーカーの応答状態が不正です")
        except (OSError, EOFError, queue.Empty, queue.Full, ValueError, UnicodeError) as exc:
            self.failed = True
            self.close()
            raise RuntimeError("C++編集ワーカーの応答を確認できません。編集は再送しません。本文を確認して再起動してください") from exc
        if not success:
            try:
                reader.done()
            except ValueError:
                self.failed = True
                self.close()
                raise
            raise RuntimeError(message)
        return reader

    @contextmanager
    def _response(self, command, writer=None):
        reader = self._call(command, writer)
        try:
            yield reader
            reader.done()
        except (ValueError, UnicodeError) as exc:
            self.failed = True
            self.close()
            raise RuntimeError("C++ワーカーの応答形式が不正です。編集は再送しません") from exc

    @stage("通信/C++編集ワーカー往復")
    def _call(self, command, writer=None):
        if self.failed:
            raise RuntimeError("C++編集ワーカーが停止しています。プログラムを再起動してください")
        if self.process is None:
            self._start()
        return self._exchange(command, writer or Writer())

    def warmup(self):
        with self._response(2):
            pass
        return True

    def capture(self, mode, max_chars, trigger_keys=()):
        writer = Writer()
        writer.number(len(trigger_keys))
        for key in trigger_keys:
            writer.number(key)
        self.saved = None
        with self._response(3, writer) as reader:
            token = reader.number()
            identity = tuple(reader.number() for _ in range(3))
            tick = reader.number()
            state = TextState(reader.text(), reader.number(), reader.number())
        capture = make_capture(state, (identity, tick), mode, self.config, token)
        self.saved = capture
        return capture

    def apply(self, capture, result):
        if capture != self.saved or capture is None:
            raise RuntimeError("取得した編集対象が無効になっています")
        self.saved = None
        start, end, expected, caret = replacement_plan(capture, result)
        if not result or "\x00" in result or len(expected) > self.config.max_document_chars:
            raise ValueError("差し替え結果が空、不正、または本文上限を超えています")
        writer = Writer()
        for value in (capture.edit_token, start, end, caret):
            writer.number(value)
        writer.text(result)
        with self._response(4, writer):
            pass
        return True

    def inspect(self):
        with self._response(5) as reader:
            backend, count, start, end = reader.text(), reader.number(), reader.number(), reader.number()
        return dict(backend=backend, document_chars=count, selection=[start, end],
                    clipboard="unused", worker="native")

    def close(self):
        process = self.process
        if process is None:
            return
        if process.poll() is None:
            if not self.failed:
                try:
                    self._exchange(6, Writer()).done()
                except (OSError, RuntimeError, ValueError):
                    pass
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=3)
        self.stopping.set()
        for stream in (process.stdin, process.stdout):
            stream.close()
        if self.reader_thread is not None:
            self.reader_thread.join(timeout=1)
        self.process = None
        self.saved = None
        self.failed = True
