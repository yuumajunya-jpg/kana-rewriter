"""Pure commit lifecycle: focus, acceptance boundary and inference invalidation."""
from dataclasses import dataclass
import logging
import threading

from .tsf import eligible, INPUT_EVENTS, COMMIT_MAX_AGE_MS

logger = logging.getLogger("kana_rewriter.auto_ime")

FOCUS_RETRY_MS = 300
MODIFIER_KEYS = frozenset((0x10, 0x11, 0x12, 0x5B, 0x5C, *range(0xA0, 0xA6)))


def invalidates_commit(event):
    # OnEndEdit also fires for selection/property updates with no composition.
    # The editor backend still verifies the complete text and selection before
    # replacing. Keep cancellation for real keys and new IME compositions.
    if event["kind"] == 3:
        return event["active"] != 0
    if event["kind"] == 8:
        return event["key"] not in MODIFIER_KEYS
    return event["kind"] in INPUT_EVENTS


@dataclass(frozen=True)
class Focus:
    target: tuple
    runtime: tuple
    tid: int


@dataclass(frozen=True)
class Commit:
    text: str
    focus: Focus
    tick: int
    revision: int
    native_range: tuple | None = None


class CommitState:
    """Bounded mailbox plus an edit/focus revision for in-flight inference."""
    def __init__(self):
        self.context_generation = None
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.focus_failed_since = None
        self.focus = None
        self.revision = 0
        self.pending = None
        self.busy = False
        self.closed = False
        # Accept only compositions that began after this field became eligible.
        # The native observer owns composition history; Python owns this boundary.
        self.accept_since = 0

    def same_focus(self, first, second):
        """TSF identifies its own retained ranges independently of UIA RuntimeId.

        Matching HWND/TID only keeps the subscription. Authorization additionally
        requires a known TSF context generation and an immutable native range.
        """
        if first is not None and second is not None:
            return first.target == second.target and first.tid == second.tid
        return first == second

    def track(self, focus, now=0):
        with self.lock:
            recovered = self.focus_failed_since is not None
            if (recovered and self.focus is not None
                    and now - self.focus_failed_since >= FOCUS_RETRY_MS):
                self.revision += 1
                self.pending = None
                self.accept_since = now
            self.focus_failed_since = None
            if not self.same_focus(focus, self.focus):
                logger.debug("TSF入力先変更: old=%r / new=%r / tick=%s / pending=%s / busy=%s",
                             self.focus, focus, now, self.pending is not None, self.busy)
                self.focus = focus
                self.revision += 1
                self.pending = None
                self.accept_since = now
                self.context_generation = None
            elif focus != self.focus:
                logger.debug('TSF入力先のUIA表現変更: old=%r / new=%r / context_generation=%s',
                             self.focus, focus, self.context_generation)
                self.focus = focus
            self.changed.notify_all()
            return recovered

    def focus_failed(self, now):
        """Suspend delivery on UIA errors; retain the subscription for a retry."""
        with self.lock:
            if self.focus_failed_since is None:
                self.focus_failed_since = now
            if now - self.focus_failed_since >= FOCUS_RETRY_MS:
                if self.focus is not None:
                    self.focus = None
                    self.context_generation = None
                    self.revision += 1
                self.pending = None
                self.accept_since = now
            self.changed.notify_all()
            return self.focus

    def observe(self, events, now):
        with self.lock:
            for event in events:
                if (self.focus is None or event["tid"] != self.focus.tid
                        or event["pid"] != self.focus.target[2]):
                    if event["kind"] == 4:
                        logger.debug("TSF確定判定: 入力先不一致 / pid=%s tid=%s / focus=%r",
                                     event["pid"], event["tid"], self.focus)
                    continue
                if event['kind'] == 2:
                    self.context_generation = event['generation'] if event['hr'] == 0 else None
                    self.accept_since = max(self.accept_since, event['tick'])
                elif event['kind'] in (6, 7, 9):
                    self.context_generation = None
                elif event['kind'] != 1 and (
                        self.context_generation is None
                        or event['generation'] != self.context_generation):
                    self.revision += 1
                    self.pending = None
                    self.changed.notify_all()
                    logger.debug('TSF通知を拒否: 文脈世代不一致 / kind=%s generation=%s expected=%s',
                                 event['kind'], event['generation'], self.context_generation)
                    continue
                if invalidates_commit(event):
                    if self.pending is not None:
                        logger.debug("TSF確定判定: 保留確定を破棄 / event=%s key=%s / revision=%s",
                                     event["kind"], event["key"], self.revision)
                    self.revision += 1
                    self.pending = None
                    self.changed.notify_all()
                if self.closed or self.busy:
                    if event["kind"] == 4:
                        logger.debug("TSF確定判定: 受付停止 / closed=%s busy=%s", self.closed, self.busy)
                    continue
                valid, reason = eligible(event, now, self.focus.target)
                if valid and not (event.get('commit_id') and event.get('revision')):
                    valid, reason = False, 'native_range_missing'
                if event["kind"] == 4:
                    logger.debug("TSF確定範囲: text=%r / commit_id=%s / native_revision=%s / focus=%r",
                                 event["text"], event.get('commit_id'), event.get('revision'), self.focus)
                    logger.debug("TSF確定判定: eligible=%s reason=%s / now=%s tick=%s "
                                 "composition_tick=%s accept_since=%s / revision=%s",
                                 valid, reason, now, event["tick"], event["composition_tick"],
                                 self.accept_since, self.revision)
                if valid and self.accept_since <= event["composition_tick"] <= event["tick"]:
                    text = event["text"].replace("\r\n", "\n").replace("\r", "\n")
                    native_range = (event['commit_id'], event['revision'], event['text'])
                    self.pending = Commit(text, self.focus, event["tick"], self.revision, native_range)
                elif valid:
                    logger.debug("TSF確定破棄: 確定開始が受付期間外 / composition_tick=%s "
                                 "accept_since=%s commit_tick=%s / focus=%r",
                                 event["composition_tick"], self.accept_since, event["tick"], self.focus)
                elif event["kind"] == 4 and reason == "ctrl_confirm":
                    logger.debug("TSF自動変換: Ctrl+Enterのため置換しません")

    def take(self, now):
        with self.lock:
            if self.focus_failed_since is not None:
                return None
            commit, self.pending = self.pending, None
            if (not self.closed and not self.busy and commit is not None
                    and 0 <= now - commit.tick <= COMMIT_MAX_AGE_MS):
                return commit
        return None

    def valid(self, commit, focus_wait=0):
        with self.lock:
            # Do not authorize edits using stale focus. A transient UIA failure
            # may recover without permanently discarding the inference result.
            self.changed.wait_for(
                lambda: self.focus_failed_since is None or self.closed
                or not self.same_focus(self.focus, commit.focus) or self.revision != commit.revision,
                timeout=focus_wait)
            return (not self.closed and self.same_focus(self.focus, commit.focus)
                    and self.revision == commit.revision and self.focus_failed_since is None)

    def begin(self, commit=None):
        with self.lock:
            if self.closed:
                return False
            if commit is not None:
                if (self.busy or self.focus_failed_since is not None
                        or not self.same_focus(self.focus, commit.focus) or self.revision != commit.revision):
                    return False
            else:
                self.revision += 1
            self.pending = None
            self.busy = True
            self.changed.notify_all()
            return True

    def finish(self, now):
        with self.lock:
            self.revision += 1
            self.pending = None
            self.busy = False
            self.accept_since = max(self.accept_since, now)
            self.changed.notify_all()

    def resync(self, now):
        """Discard queued/in-flight edits without ending manual or auto work."""
        with self.lock:
            self.revision += 1
            self.pending = None
            self.context_generation = None
            self.accept_since = max(self.accept_since, now)
            self.changed.notify_all()

    def close(self):
        with self.lock:
            self.closed = True
            self.pending = None
            self.revision += 1
            self.changed.notify_all()
