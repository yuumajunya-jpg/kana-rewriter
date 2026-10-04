"""UIA ranges and Unicode input. Used only inside the editor worker process."""
import time
import logging

from .editor import TextState
from .direct_win32 import check_input_ready, unicode_events, window_identity, input_tick, class_name
from .winapi import KeyboardInput
from .waiting import sleep as poll_sleep
from .winapi import send
from .ime import ImeSession
from .timing import stage, logger as timing_logger

logger = logging.getLogger(__name__)


class SnapshotPending(RuntimeError):
    """Text and range reads straddled an editor update; retry reads only."""


def normalize(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


def property_values(automation, element, names):
    """Fetch a fresh element-only snapshot; cache requests, never live values."""
    try:
        cached = element.BuildUpdatedCache(automation.cache_request(names))
        return {name: getattr(cached, "Cached" + name) for name in names}
    except Exception:
        # Keep providers without a usable cache on the original live path.
        logger.debug("UIA属性一括取得不可: 個別取得へ切り替え", exc_info=True)
        return {name: getattr(element, "Current" + name) for name in names}


class Automation:
    def __init__(self):
        import sys
        sys.coinit_flags = 0  # initialize comtypes in this windowless MTA process
        try:
            import comtypes
            import comtypes.client
        except ImportError as exc:
            raise RuntimeError('UIAにはcomtypesが必要です: python -m pip install -e ".[desktop]"') from exc
        self.types = comtypes.client.GetModule("UIAutomationCore.dll")
        self.client = comtypes.client.CreateObject(self.types.CUIAutomation,
                                                   interface=self.types.IUIAutomation)
        self.cache_requests = {}

    def cache_request(self, names):
        key = tuple(names)
        if key not in self.cache_requests:
            request = self.client.CreateCacheRequest()
            request.TreeScope = self.types.TreeScope_Element
            for name in key:
                request.AddProperty(getattr(self.types, "UIA_" + name + "PropertyId"))
            self.cache_requests[key] = request
        return self.cache_requests[key]

    def pattern(self, element, name):
        types = self.types
        try:
            return element.GetCurrentPattern(getattr(types, "UIA_" + name + "Id")).QueryInterface(
                getattr(types, "IUIAutomation" + name))
        except Exception:
            return None

    def focused_editor(self, config):
        focused = self.client.GetFocusedElement()
        if not focused:
            raise RuntimeError("入力先を取得できません")
        state = property_values(self, focused, ("IsPassword", "IsEnabled", "HasKeyboardFocus"))
        if state["IsPassword"] or not state["IsEnabled"]:
            raise RuntimeError("入力先を取得できないか、パスワード・無効な入力欄です")
        element = focused
        # A focused text child can expose TextPattern on its editable ancestor.
        for _ in range(8):
            pattern = self.pattern(element, "TextPattern")
            if pattern is not None:
                return AutomationEditor(self, focused, element, pattern, config, initial_state=state)
            element = self.client.ControlViewWalker.GetParentElement(element)
            if not element:
                break
        raise RuntimeError("この入力欄はUIAのTextPatternによる取得・選択に対応していません")


class AutomationEditor:
    kind = "uia"
    validates_before_replace = True
    positions_with_input = True

    def __init__(self, automation, focused, element, pattern, config, initial_state=None):
        self.automation = automation
        self.focused = focused
        self.element = element
        self.pattern = pattern
        self.runtime_id = tuple(focused.GetRuntimeId())
        self.limit = config.max_document_chars
        self.timeout = min(config.editor_timeout_seconds, 2)
        self.readback_initial_delay = config.uia_readback_initial_delay_ms / 1000
        self.window = window_identity()
        self.ime_check = config.uia_ime_check
        self.ime_session_factory = ImeSession
        names = ("FrameworkId", "ClassName")
        if logger.isEnabledFor(logging.DEBUG):
            names += ("ControlType",)
        metadata = property_values(automation, focused, names)
        provider = metadata if element is focused else property_values(
            automation, element, ("FrameworkId", "ClassName"))
        self.focused_class = metadata["ClassName"]
        frameworks = {str(metadata["FrameworkId"]).casefold(), str(provider["FrameworkId"]).casefold()}
        self.chromium = bool(frameworks & {"chrome", "chromium"}) or class_name(
            self.window[1]).startswith("chrome_")
        logger.debug("UIA入力欄: framework=%s / Chromium=%s / uia_ime_check=%s",
                     sorted(frameworks), self.chromium, self.ime_check)
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("UIA要素: focused class=%r / control=%s / provider class=%r",
                         metadata["ClassName"], metadata["ControlType"], provider["ClassName"])
        self.check_writable(initial_state)

    def check_focus(self):
        current = self.automation.client.GetFocusedElement()
        if window_identity() != self.window or tuple(current.GetRuntimeId()) != self.runtime_id:
            raise RuntimeError("入力先が変わったため中止しました")

    def check_probe_focus(self):
        if window_identity() != self.window:
            raise RuntimeError("入力先が変わったため中止しました")

    def check_writable(self, initial_state=None):
        state = initial_state if initial_state is not None else property_values(
            self.automation, self.focused, ("IsPassword", "IsEnabled", "HasKeyboardFocus"))
        if state["IsPassword"] or not state["IsEnabled"]:
            raise RuntimeError("パスワード・無効な入力欄は対象外です")
        if not state["HasKeyboardFocus"]:
            raise RuntimeError("入力欄にキーボードフォーカスがありません")
        value = self.automation.pattern(self.element, "ValuePattern")
        if value is not None and value.CurrentIsReadOnly:
            raise RuntimeError("読み取り専用の入力欄です")
        readonly = self.pattern.DocumentRange.GetAttributeValue(self.automation.types.UIA_IsReadOnlyAttributeId)
        if isinstance(readonly, (bool, int)) and readonly:
            raise RuntimeError("読み取り専用の入力欄です")
        if self.ime_check == "off":
            logger.debug("IME判定: uia_ime_check=offのため照会を省略")
            return
        # In auto mode Chromium's cached composition is advisory only.
        # Keep the diagnostic when requested, without querying it on the hot path.
        if self.ime_check == "auto" and self.chromium and not logger.isEnabledFor(logging.DEBUG):
            return
        composing = self.automation.pattern(self.element, "TextEditPattern")
        if composing is not None:
            active = composing.GetActiveComposition()
            nonempty = bool(active) and active.CompareEndpoints(0, active, 1) != 0
            has_text = bool(nonempty and active.GetText(1))
            logger.debug("IME判定: UIA TextEditPattern / 未確定範囲あり=%s", has_text)
            if has_text and self.ime_check == "auto" and self.chromium:
                # Chromium providers can retain composition offsets after
                # finalization. A range alone cannot distinguish that cached
                # state from live composition. Do not synthesize Enter/Escape
                # or reset the IME to try to clear the provider's state.
                logger.debug("IME判定: Chromiumの範囲は確定後も残る場合があるため中止判定に使いません")
            elif has_text:
                raise RuntimeError("IMEの入力を確定してから変換してください（UIA TextEditPattern）")
        else:
            logger.debug("IME判定: TextEditPattern非対応 / 未確定状態は検出できません")

    def get_text(self, range_):
        # Empty BSTRs can arrive as None. At document/line starts we also use
        # collapsed ranges to measure prefixes and construct insertion points.
        raw = range_.GetText(self.limit * 2 + 1)
        if raw is None:
            if range_.CompareEndpoints(0, range_, 1) == 0:
                return ""
            raise SnapshotPending("空ではないUIA文字範囲の本文を取得できません")
        text = normalize(raw)
        if len(text) > self.limit:
            raise RuntimeError("入力欄の本文がmax_document_charsを超えています")
        return text

    def report_caret_status(self, message):
        if getattr(self, "last_caret_status", None) != message:
            logger.debug("UIA位置: %s", message)
            self.last_caret_status = message

    def selection(self):
        ranges = self.pattern.GetSelection()
        if ranges and ranges.Length > 1:
            raise RuntimeError("複数の選択範囲は未対応です")
        selected = ranges.GetElement(0) if ranges and ranges.Length == 1 else None
        if selected and selected.CompareEndpoints(0, selected, 1) != 0:
            self.selection_empty = False
            self.selection_source = "GetSelection（選択範囲）"
            return selected
        # Some custom editors expose a placeholder collapsed selection at 0
        self.selection_empty = True
        # even while the actual caret is elsewhere. Prefer the dedicated API
        # for an unselected caret, while keeping real selections for J mode.
        # Pattern support is stable for this captured editor. Cache even None
        # so Chromium's unsupported pattern is not queried on every readback.
        if not hasattr(self, "caret_pattern"):
            self.caret_pattern = self.automation.pattern(self.element, "TextPattern2")
        pattern2 = self.caret_pattern
        if pattern2 is not None:
            try:
                active, range_ = pattern2.GetCaretRange()
            except Exception as exc:
                logger.debug("UIA位置: GetCaretRangeの取得失敗=%s", exc)
                active, range_ = False, None
            if active and range_:
                if range_.CompareEndpoints(0, range_, 1) != 0:
                    raise RuntimeError("UIAのカーソルAPIが空でない範囲を返しました")
                self.selection_source = "GetCaretRange（アクティブ）"
                return range_
            self.report_caret_status("GetCaretRangeのアクティブなカーソルなし")
        else:
            self.report_caret_status("TextPattern2非対応")
        if selected:
            self.selection_source = "GetSelection（空範囲・代替）"
            return selected
        raise RuntimeError("この入力欄の選択範囲・カーソルを取得できません")

    @stage("UIA/本文と位置取得（再取得を含む）")
    def read(self):
        deadline = time.monotonic() + min(getattr(self, "timeout", 0.5), 0.5)
        attempts = 0
        while True:
            try:
                state = self.read_once()
                if attempts:
                    logger.debug("UIA再取得: %s回の読み直しで整合 / 本文=%s文字 / 選択=%s:%s",
                                 attempts, len(state.text), state.start, state.end)
                return state
            except SnapshotPending as exc:
                attempts += 1
                if attempts == 1:
                    logger.debug("UIA再取得: 本文と範囲の更新待ち / %s", exc)
                self.check_focus()
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"UIAの本文・選択位置が整合する状態を確認できませんでした: {exc}") from exc
                poll_sleep(0.01)

    def validate_label(self, text):
        focused = getattr(self, "focused", None)
        focused_class = getattr(self, "focused_class", None)
        if focused_class is None and focused is not None:
            focused_class = focused.CurrentClassName
        if (focused is not None and focused_class == "native-edit-context"
                and text and text == normalize(focused.CurrentName)):
            logger.debug("UIA本文取得不可: native-edit-contextがエディター本文ではなく案内ラベルを公開")
            raise RuntimeError("VS Code/Monacoがエディター本文をUIAに公開していません。"
                               "VS CodeでShift+Alt+F1を押してスクリーンリーダー最適化モードを有効にするか、"
                               '設定で"editor.accessibilitySupport": "on"にして再実行してください')

    def read_once(self):
        self.check_focus()
        document = self.pattern.DocumentRange
        text = self.get_text(document)
        self.validate_label(text)
        selection = self.selection()
        prefix = document.Clone()
        prefix.MoveEndpointByRange(1, selection, 0)
        left = self.get_text(prefix)
        collapsed = getattr(self, "selection_empty", None)
        if collapsed is None:
            collapsed = selection.CompareEndpoints(0, selection, 1) == 0
        if collapsed:
            through_selection, selected = left, ""
        else:
            prefix.MoveEndpointByRange(1, selection, 1)
            through_selection = self.get_text(prefix)
            selected = self.get_text(selection)
        if not text.startswith(left) or not text.startswith(through_selection):
            raise SnapshotPending(f"本文とUIA文字範囲を対応づけられません"
                                  f"（本文{len(text)}文字、左{len(left)}文字、範囲末尾{len(through_selection)}文字）")
        start, end = len(left), len(through_selection)
        if text[start:end] != selected:
            raise SnapshotPending("UIA選択範囲の本文が一致しません")
        # A range is live: even Clone() does not freeze the document content.
        # Reject mixed generations, including changes outside the selection.
        if self.get_text(self.pattern.DocumentRange) != text:
            raise SnapshotPending("本文の読み取り中に内容が更新されました")
        position = (getattr(self, "selection_source", "GetSelection"), len(text), start, end)
        if getattr(self, "last_position", None) != position:
            logger.debug("UIA位置: %s / 本文=%s文字 / 選択=%s:%s", *position)
            self.last_position = position
        self.check_focus()
        state = TextState(text, start, end)
        self.read_anchor = (state, selection)  # live; revalidated by range_for
        return state

    @stage("UIA/期待状態専用確認")
    def expected_once(self, expected, document=None):
        if document is None:
            document = self.pattern.DocumentRange
            if self.get_text(document) != expected.text:
                self.check_focus()
                return "text_changed"
        self.validate_label(expected.text)
        if not 0 <= expected.start <= expected.end <= len(expected.text):
            raise RuntimeError("期待する選択位置が不正です")
        if expected.start != expected.end and hasattr(self.pattern, "GetSelection"):
            ranges = self.pattern.GetSelection()
            count = ranges.Length if ranges else 0
            if count > 1:
                raise RuntimeError("複数の選択範囲は未対応です")
            selected = ranges.GetElement(0) if count == 1 else None
            correct = bool(selected) and self.get_text(selected) == expected.text[expected.start:expected.end]
        else:
            selected = self.selection()
            empty = getattr(self, "selection_empty", None)
            if empty is None:
                empty = selected.CompareEndpoints(0, selected, 1) == 0
            correct = (empty if expected.start == expected.end else
                       self.get_text(selected) == expected.text[expected.start:expected.end])
        if correct:
            prefix = document.Clone()
            prefix.MoveEndpointByRange(1, selected, 0)
            correct = self.get_text(prefix) == expected.text[:expected.start]
            if correct and expected.start != expected.end:
                prefix.MoveEndpointByRange(1, selected, 1)
                correct = self.get_text(prefix) == expected.text[:expected.end]
        if self.get_text(self.pattern.DocumentRange) != expected.text:
            raise SnapshotPending("期待状態の確認中に本文が変更されました")
        self.check_focus()
        if not correct:
            return "position_changed"
        self.read_anchor = (expected, selected)
        return "match"

    def confirm_expected(self, expected, focus_checked=False):
        if not focus_checked:
            self.check_focus()
        deadline = time.monotonic() + min(getattr(self, "timeout", 0.5), 0.5)
        while True:
            try:
                return self.expected_once(expected)
            except SnapshotPending:
                self.check_focus()
                if time.monotonic() >= deadline:
                    raise RuntimeError("UIAの期待状態が整合する状態を確認できません")
                poll_sleep(0.01)

    @stage("UIA/期待本文の反映待ちと最終確認")
    def wait_for_state(self, expected, deadline):
        """Avoid fetching selection/prefixes while Unicode input is incomplete.

        A matching text probe is only a gate, never proof of success. The full
        snapshot validation still runs before returning or restoring the IME.
        """
        probes = confirmations = 0
        probe_seconds = confirmation_seconds = sleep_seconds = 0.0
        wait_started = time.perf_counter()
        last_length = None
        try:
            initial_delay = getattr(self, "readback_initial_delay", 0)
            if initial_delay:
                self.check_probe_focus()
                delay = min(initial_delay, max(0, deadline - time.monotonic()))
                started = time.perf_counter()
                poll_sleep(delay)
                sleep_seconds += time.perf_counter() - started
            while True:
                started = time.perf_counter()
                self.check_probe_focus()
                try:
                    document = self.pattern.DocumentRange
                    text = self.get_text(document)
                except SnapshotPending:
                    text = None
                probes += 1
                probe_seconds += time.perf_counter() - started
                if text is not None and len(text) != last_length:
                    logger.debug("UIA反映待ち: 本文=%s文字 / 期待=%s文字", len(text), len(expected.text))
                    last_length = len(text)
                if text == expected.text:
                    confirmations += 1
                    started = time.perf_counter()
                    try:
                        self.check_focus()
                        if self.expected_once(expected, document=document) == "match":
                            return expected
                    except SnapshotPending:
                        pass  # input or provider state is still being updated
                    finally:
                        confirmation_seconds += time.perf_counter() - started
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RuntimeError("差し替え結果を確認できませんでした。本文を確認してください。編集は再送しません")
                # Fast initial probes; bounded backoff avoids busy polling a
                # slow/unresponsive provider. No nested 0.5-second read loop.
                delay = min(0.002 if probes <= 2 else 0.005 if probes <= 5 else 0.01, remaining)
                started = time.perf_counter()
                poll_sleep(delay)
                sleep_seconds += time.perf_counter() - started
        finally:
            total = time.perf_counter() - wait_started
            timing_logger.debug("反映待ち内訳: 本文照会=%s回/%.1fms / 最終確認=%s回/%.1fms / 休止=%.1fms / その他=%.1fms",
                         probes, probe_seconds * 1000, confirmations, confirmation_seconds * 1000,
                         sleep_seconds * 1000,
                         max(0, total - probe_seconds - confirmation_seconds - sleep_seconds) * 1000)

    def point(self, document, text, offset):
        # UIA Character units can differ from Python code points. Move in bulk,
        # then verify the prefix rather than assuming a one-to-one mapping.
        prefix = document.Clone()
        prefix.MoveEndpointByRange(1, document, 0)
        if offset:
            prefix.MoveEndpointByUnit(1, 0, offset)
        for _ in range(8):
            actual = self.get_text(prefix)
            if actual == text[:offset]:
                prefix.MoveEndpointByRange(0, prefix, 1)
                return prefix
            delta = offset - len(actual)
            if not delta or not prefix.MoveEndpointByUnit(1, 0, delta):
                break
        raise RuntimeError("UIAの文字単位と変換位置を対応づけられません")

    @stage("UIA/範囲構築")
    def range_for(self, state, start, end):
        document = self.pattern.DocumentRange
        if self.get_text(document) != state.text:
            raise RuntimeError("文字範囲の準備中に本文が変更されました")
        anchor = getattr(self, "read_anchor", None)
        nearby = (anchor[1] if anchor is not None and anchor[0] == state else self.selection()).Clone()
        if (start, end) != (state.start, state.end):
            nearby.MoveEndpointByRange(0, nearby, 1)
            if end != state.end:
                nearby.MoveEndpointByUnit(1, 0, end - state.end)
                nearby.MoveEndpointByRange(0, nearby, 1)
            if start != end:
                nearby.MoveEndpointByUnit(0, 0, start - end)
        if self.range_matches(document, nearby, state.text, start, end):
            return nearby
        # Keep verified document-based fallbacks for unusual provider units.
        begin = self.point(document, state.text, start)
        finish = self.point(document, state.text, end)
        begin.MoveEndpointByRange(1, finish, 1)
        if self.range_matches(document, begin, state.text, start, end):
            return begin
        logger.debug("UIA範囲再構築: 要求=%s:%s / 要求文字=%r / 実際文字=%r",
                     start, end, state.text[start:end], self.get_text(begin))
        # Paragraph boundaries may have distinct provider anchors with the same
        # prefix text. Collapsing the left prefix can retain the preceding blank
        # paragraph's anchor. Build backwards from the right endpoint instead;
        # never trim the returned text or select an unverified range.
        anchors = [finish]
        if end == state.end:
            live = self.selection().Clone()
            live.MoveEndpointByRange(0, live, 1)
            anchors.append(live)
        for anchor in anchors:
            candidate = anchor.Clone()
            candidate.MoveEndpointByUnit(0, 0, start - end)
            for _ in range(8):
                if self.range_matches(document, candidate, state.text, start, end):
                    logger.debug("UIA範囲再構築: 末尾からの移動で一致=%s:%s", start, end)
                    return candidate
                prefix = document.Clone()
                prefix.MoveEndpointByRange(1, candidate, 0)
                left = self.get_text(prefix)
                if not state.text.startswith(left):
                    break
                delta = start - len(left)
                if not delta or not candidate.MoveEndpointByUnit(0, 0, delta):
                    break
        raise RuntimeError(f"UIAの置換対象範囲を確認できませんでした（要求{start}:{end}）。"
                           "文字は送信していません。--debugで範囲の診断を確認できます")

    def range_matches(self, document, range_, text, start, end):
        if self.get_text(range_) != text[start:end]:
            return False
        prefix = document.Clone()
        prefix.MoveEndpointByRange(1, range_, 0)
        if self.get_text(prefix) != text[:start]:
            return False
        prefix.MoveEndpointByRange(1, range_, 1)
        if self.get_text(prefix) != text[:end]:
            return False
        if self.get_text(self.pattern.DocumentRange) != text:
            raise RuntimeError("文字範囲の準備中に本文が変更されました")
        return True

    def replace_positioned(self, state, start, end, result, expected_tick, caret):
        inserted = start + len(result)
        steps = 0
        if getattr(self, "chromium", False) and caret > inserted:
            count = caret - inserted
            from .editor import simple_caret_text
            if (count <= 16 and end + count <= len(state.text) and
                    all(c in "。、，．！？!?.,:;" for c in state.text[end:end + count]) and
                    simple_caret_text(state.text) and simple_caret_text(result)):
                try:
                    direction = self.pattern.DocumentRange.GetAttributeValue(
                        self.automation.types.UIA_TextFlowDirectionsAttributeId)
                    if (self.element.CurrentControlType == self.automation.types.UIA_EditControlTypeId
                            and type(direction) is int and direction == 0):
                        steps = count
                except Exception:
                    pass  # unknown direction/control retains UIA caret restoration
        self.replace(state, start, end, result, expected_tick=expected_tick, caret_steps=steps)
        return inserted + steps

    def replace(self, state, start, end, result, expected_tick=None, caret_steps=0):
        before_tick = input_tick() if expected_tick is None else expected_tick
        with stage("UIA/書き込み準備と状態確認"):
            events = unicode_events(result)  # validate before changing selection
            if caret_steps:
                with stage("UIA/句読点後への一括カーソル移動"):
                    events.extend(KeyboardInput(0x27, 0, flags, 0, 0)
                                  for _ in range(caret_steps) for flags in (0, 2))
            self.check_focus()
            self.check_writable()
            check_input_ready(self.window[1])
            if self.confirm_expected(state, focus_checked=True) != "match":
                raise RuntimeError("置換直前に本文または選択位置が変わりました")
        selected = self.range_for(state, start, end)
        if input_tick() != before_tick:
            raise RuntimeError("文字範囲の準備中に操作がありました")
        with stage("UIA/選択反映待ち"):
            selected.Select()
            deadline = time.monotonic() + self.timeout
            wanted = TextState(state.text, start, end)
            while True:
                if input_tick() != before_tick:
                    raise RuntimeError("選択の反映待ちに操作があったため中止しました")
                # Different provider anchors can represent the same textual
                # boundary. Verify freshly read text and offsets, rather than
                # equating raw range endpoints from separate COM objects.
                actual = self.confirm_expected(wanted)
                if actual == "match":
                    break
                if actual == "text_changed":
                    raise RuntimeError("選択の反映待ちに本文が変更されました")
                if time.monotonic() >= deadline:
                    observed = self.read()
                    raise RuntimeError(f"UIAの選択が反映されませんでした（要求{start}:{end}、実際{observed.start}:{observed.end}）。文字は送信していません")
                poll_sleep(0.01)
        with stage("UIA/送信前確認"):
            self.check_focus()
            if self.get_text(self.pattern.DocumentRange) != state.text:
                raise RuntimeError("入力前に本文が変わりました")
            check_input_ready(self.window[1])
            self.check_focus()
            if input_tick() != before_tick:
                raise RuntimeError("入力直前に操作がありました")
        logger.debug("UIA入力: 選択確認済み=%s:%s / 送信文字数=%s / イベント数=%s",
                     start, end, len(result), len(events))
        # Keep disabled until the engine has verified text/caret. SendInput
        # returning does not mean the application's input queue is drained.
        with stage("UIA/IME状態取得と一時オフ"):
            self.ime_session = self.ime_session_factory(self.window)
            self.ime_session.disable()
        with stage("UIA/IME変更後の再確認"):
            # read() checks focus both before and after the full snapshot.
            if self.confirm_expected(wanted) != "match" or input_tick() != before_tick:
                raise RuntimeError("IME切り替え中に本文・選択または操作状態が変わりました。文字は送信していません")
        with stage("UIA/SendInput送信"):
            send(events)  # never delete first or retry partial input
        logger.debug("UIA入力: SendInput送信済み。本文とカーソルの反映確認へ進みます")

    def restore_caret(self, state, caret, expected_tick=None):
        self.check_focus()
        selected = self.range_for(state, caret, caret)
        self.check_focus()
        if expected_tick is not None and input_tick() != expected_tick:
            raise RuntimeError("カーソル復元前に操作がありました")
        selected.Select()

    def close(self):
        self.finish_edit()

    def finish_edit(self):
        session = getattr(self, "ime_session", None)
        self.ime_session = None
        if session is not None:
            session.close()
