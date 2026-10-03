"""Opt-in stage timings, including stages that end in an exception."""
from contextlib import contextmanager
import logging
import time

logger = logging.getLogger(__name__)


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
