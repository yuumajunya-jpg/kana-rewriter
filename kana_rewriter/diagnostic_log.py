"""Opt-in UTF-8 diagnostic files; disk writes run outside the edit path."""
from datetime import datetime
import logging
from logging.handlers import QueueHandler, QueueListener
import os
from pathlib import Path
import queue
import sys
import threading


class LoggedStream:
    def __init__(self, stream, handler, level):
        self.stream, self.handler, self.level = stream, handler, level
        self.pending = ""
        self.lock = threading.Lock()

    def _record(self, line):
        if line:
            self.handler.handle(logging.LogRecord(
                "kana_rewriter.console", self.level, "", 0, line, (), None))

    def write(self, text):
        with self.lock:
            result = self.stream.write(text)
            self.pending += text
            while "\n" in self.pending:
                line, self.pending = self.pending.split("\n", 1)
                self._record(line.rstrip("\r"))
            return result

    def flush(self):
        with self.lock:
            self.stream.flush()
            self._record(self.pending)
            self.pending = ""

    def __getattr__(self, name):
        return getattr(self.stream, name)


class DiagnosticLog:
    def __init__(self, path=""):
        if not path:
            name = datetime.now().astimezone().strftime("diagnostic-%Y%m%d-%H%M%S-%f")
            path = Path("logs") / f"{name}-{os.getpid()}.log"
        self.path = Path(path).resolve()

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = logging.FileHandler(self.path, mode="a", encoding="utf-8")
        self.file.setFormatter(logging.Formatter(
            "%(asctime)s.%(msecs)03d %(processName)s/%(process)d %(threadName)s "
            "%(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"))
        self.handler = QueueHandler(queue.Queue())
        self.listener = QueueListener(self.handler.queue, self.file)
        self.root = logging.getLogger()
        self.level = self.root.level
        self.root.setLevel(logging.DEBUG)
        self.root.addHandler(self.handler)
        self.stdout, self.stderr = sys.stdout, sys.stderr
        sys.stdout = LoggedStream(self.stdout, self.handler, logging.INFO)
        sys.stderr = LoggedStream(self.stderr, self.handler, logging.ERROR)
        self.listener.start()
        logging.getLogger(__name__).info("診断ログ開始: Python=%s / path=%s", sys.version, self.path)
        print(f"診断ログ: {self.path}", flush=True)
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc is not None:
            logging.getLogger(__name__).error("診断セッション終了: %s", exc,
                                             exc_info=(exc_type, exc, traceback))
        try:
            sys.stdout.flush()
            sys.stderr.flush()
        finally:
            sys.stdout, sys.stderr = self.stdout, self.stderr
            self.root.removeHandler(self.handler)
            self.root.setLevel(self.level)
            self.listener.stop()
            self.handler.close()
            self.file.close()
