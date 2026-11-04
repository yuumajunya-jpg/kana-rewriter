"""Preview composition state and inference ordering, independent of Windows/IPC."""
from dataclasses import dataclass
from .text import split_at_caret, TextRegion


def preview_position(event):
    if (not event.get('position_valid') or event['bottom'] <= event['top']
            or event['right'] < event['left']):
        return None
    return event['right'], event['bottom']


@dataclass(frozen=True)
class PreviewTarget(TextRegion):
    display_prefix: str = ''
    display_suffix: str = ''

    def render(self, result):
        return self.display_prefix + result + self.display_suffix


def stream_target(before, live, after, config):
    region = split_at_caret(before + live, len(before + live), config.conversion_delimiters,
                            config.stop_at_kanji, config.trailing_punctuation,
                            composition_start=len(before))
    if len(region.target) > config.max_chars:
        raise ValueError('プレビューの文字数上限です')
    return PreviewTarget(region.left, region.target, region.right + after,
                         region.left[len(before):], region.right)


class PreviewStream:
    """TSF lifecycle and layout are independent; a missing layout is not an end."""
    def __init__(self):
        self.epoch = 0
        self.clear()

    def clear(self):
        self.live = self.before = self.after = ''
        self.position = None
        self.active = False
        self.epoch += 1

    def observe(self, event):
        kind = event['kind']
        if kind in (2, 4, 5, 7):  # confirmed context change, commit/cancel, detach
            self.clear()
        elif kind == 3 and event['active'] > 0:
            self.active = True
            self.live = event['text']
            self.before, self.after = event.get('before', ''), event.get('after', '')
            position = preview_position(event)
            if position is not None:
                self.position = (self.position[0] if self.position is not None else position[0],
                                 position[1])
        # Editing(active=0) precedes the commit/cancel notification in a batch.
        # Errors, missing layout and partially replaced composition identities
        # do not prove that input has ended.

    def snapshot(self, focus, config):
        if not (self.active and self.live and self.position):
            return None
        try:
            target = stream_target(self.before, self.live, self.after, config)
        except ValueError:
            return None
        # RuntimeIds can change within one TSF composition. Context/end notices
        # advance epoch; real window/thread changes clear the stream in reader.
        session = (focus.target, focus.tid, self.epoch)
        return ((session, target), target, *self.position, focus.target[0], session)


class PreviewDisplay:
    """Show completed results monotonically within a confirmed input session."""
    def __init__(self):
        self.gate = PreviewGate()
        self.session = None
        self.result = None
        self.reading = ''
        self.completed = -1
        self.requests = {}

    def observe(self, snapshot, now):
        session = snapshot[5] if snapshot else None
        if session != self.session or snapshot is None:
            self.result = None
            self.reading = ''
            self.completed = -1
            self.requests.clear()
        self.session = session
        self.gate.observe((session, snapshot[0]) if snapshot else None, now)
        if snapshot:
            self.requests[self.gate.generation] = (session, snapshot[1])
            if len(self.requests) > 128:
                self.requests.pop(next(iter(self.requests)))

    def accept(self, generation, result):
        request = self.requests.get(generation)
        if request and request[0] == self.session and generation > self.completed and result:
            target = request[1]
            self.result = target.render(result) if isinstance(target, PreviewTarget) else result
            self.reading = (target.target if isinstance(target, PreviewTarget)
                            and result != target.target else '')
            self.completed = generation


def preview_region(state, config, composing=False):
    if state.start != state.end and not composing:
        raise ValueError("選択範囲はプレビュー対象外です")
    region = split_at_caret(state.text, state.end, config.conversion_delimiters,
                           config.stop_at_kanji, config.trailing_punctuation)
    if len(region.target) > config.max_chars:
        raise ValueError("プレビューの文字数上限です")
    return region


class PreviewGate:
    """Accept results only for the current snapshot; no debounce."""
    def __init__(self):
        self.key = None
        self.changed = 0
        self.generation = 0
        self.requested = False

    def observe(self, key, now):
        if key != self.key:
            self.key, self.changed = key, now
            self.generation += 1
            self.requested = False

    def ready(self, now):
        return self.key is not None and not self.requested

    def accepts(self, generation):
        return self.key is not None and generation == self.generation


