"""Single-consumer TSF transport, independent of slow UI Automation calls.

Only the pump reads native packets. The bounded mailbox never hides loss:
overflow invalidates the entire subscription until its owner reconnects it.
"""
import threading


class NotificationPump:
    def __init__(self, poll, dropped, capacity=4096, interval=0.005):
        self.poll, self.native_dropped = poll, dropped
        self.capacity, self.interval = capacity, interval
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.wake = threading.Event()
        self.requested = self.completed = 0
        self.events = []
        self.loss = 0
        self.error = None
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name='tsf-packets', daemon=True)
        self.thread.start()

    def _run(self):
        try:
            while not self.stop.is_set():
                self.wake.clear()
                with self.lock:
                    requested = self.requested
                events = self.poll()
                loss = self.native_dropped()
                with self.lock:
                    self.loss = max(self.loss, loss)
                    if len(self.events) + len(events) > self.capacity:
                        self.loss += len(events)
                    if self.loss:
                        self.events.clear()
                    else:
                        self.events.extend(events)
                    self.completed = requested
                    self.changed.notify_all()
                self.wake.wait(self.interval)
        except Exception as exc:
            with self.lock:
                self.error = exc
                self.events.clear()
                self.changed.notify_all()

    def drain(self):
        # Preserve drain's barrier semantics: include notices already published
        # by the native producer when called, not just the last periodic poll.
        # The caller never becomes a second native reader.
        with self.lock:
            self.requested += 1
            requested = self.requested
            self.wake.set()
            if not self.changed.wait_for(
                    lambda: self.completed >= requested or self.error or self.stop.is_set(), timeout=3):
                raise RuntimeError('TSF通知回収がタイムアウトしました')
            if self.error:
                raise RuntimeError('TSF通知回収に失敗しました') from self.error
            events, self.events = self.events, []
            return events

    def dropped(self):
        with self.lock:
            if self.error:
                raise RuntimeError('TSF通知回収に失敗しました') from self.error
            return self.loss

    def close(self):
        self.stop.set()
        self.wake.set()
        with self.lock:
            self.changed.notify_all()
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            raise RuntimeError('TSF通知回収スレッドの終了を確認できません')
