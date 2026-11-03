import queue
import threading
import time
import unittest

from kana_rewriter.notifications import NotificationPump


class NotificationTests(unittest.TestCase):
    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.005)
        self.assertTrue(predicate())

    def test_drain_collects_already_published_native_events_without_periodic_delay(self):
        source = queue.Queue()
        polled = threading.Event()
        def poll():
            polled.set()
            try:
                return [source.get_nowait()]
            except queue.Empty:
                return []
        pump = NotificationPump(poll, lambda: 0, interval=30)
        try:
            self.assertTrue(polled.wait(2))
            source.put('new native event')
            self.assertEqual(pump.drain(), ['new native event'])
        finally:
            pump.close()

    def test_slow_consumer_keeps_order_and_delivers_once(self):
        source = queue.Queue()
        def poll():
            try:
                return [source.get_nowait()]
            except queue.Empty:
                return []
        pump = NotificationPump(poll, lambda: 0, interval=.001)
        try:
            for index in range(100):
                source.put(index)
            self.wait_for(lambda: source.empty())
            # Synchronize with the producer's publication of the final batch.
            self.wait_for(lambda: len(pump.events) == 100)
            self.assertEqual(pump.drain(), list(range(100)))
            self.assertEqual(pump.drain(), [])
            self.assertEqual(pump.dropped(), 0)
        finally:
            pump.close()
        self.assertFalse(pump.thread.is_alive())

    def test_local_or_native_loss_discards_entire_history(self):
        for native_loss in (0, 1):
            pump = NotificationPump(lambda: [1, 2, 3], lambda: native_loss, capacity=2)
            try:
                self.wait_for(lambda: pump.dropped() > 0)
                self.assertEqual(pump.drain(), [])
            finally:
                pump.close()

    def test_poll_failure_reaches_consumer(self):
        called = threading.Event()
        def poll():
            called.set()
            raise ValueError('transport failed')
        pump = NotificationPump(poll, lambda: 0)
        try:
            self.assertTrue(called.wait(2))
            self.wait_for(lambda: pump.error is not None)
            with self.assertRaises(RuntimeError):
                pump.drain()
            with self.assertRaises(RuntimeError):
                pump.dropped()
        finally:
            pump.close()
