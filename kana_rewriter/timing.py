"""Opt-in stage timings, including stages that end in an exception."""
from contextlib import contextmanager
import logging
import time

logger = logging.getLogger(__name__)


class DeferredHandler(logging.Handler):
    """Keep diagnostics off the editing path; retain creation timestamps."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def take(self):
        with self.lock:
            records, self.records = self.records, []
        return records

    def export(self):
        packets = []
        for record in self.take():
            packet = record.__dict__.copy()
            packet["msg"], packet["args"] = record.getMessage(), ()
            if record.exc_info:
                packet["exc_text"] = logging.Formatter().formatException(record.exc_info)
            packet["exc_info"] = None
            packets.append(packet)
        return packets


@contextmanager
def buffered_logs():
    package = logging.getLogger("kana_rewriter")
    previous, propagate = package.handlers[:], package.propagate
    sinks = previous[:]
    ancestor = package
    while ancestor.propagate and ancestor.parent is not None:
        ancestor = ancestor.parent
        sinks.extend(ancestor.handlers)
    handler = DeferredHandler()
    handler.sinks = sinks or [logging.lastResort]
    package.handlers, package.propagate = [handler], False
    try:
        yield
    finally:
        try:
            flush_logs()
        finally:
            package.handlers, package.propagate = previous, propagate


def flush_logs():
    for handler in logging.getLogger("kana_rewriter").handlers:
        if isinstance(handler, DeferredHandler) and hasattr(handler, "sinks"):
            for record in handler.take():
                for sink in handler.sinks:
                    if sink is not None and record.levelno >= sink.level:
                        sink.handle(record)


@contextmanager
def stage(name):
    if not logger.isEnabledFor(logging.DEBUG):
        yield
        return
    started = time.perf_counter()
    status = "完了"
    try:
        yield
    except BaseException:
        status = "中止"
        raise
    finally:
        logger.debug("時間: %s=%.1fms (%s)", name, (time.perf_counter() - started) * 1000, status)
