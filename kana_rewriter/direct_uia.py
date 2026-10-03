"""UIA ranges and Unicode input. Used only inside the editor worker process."""
import time

from .editor import TextState
from .direct_win32 import check_input_ready, unicode_events, window_identity, input_tick
from .winapi import send


def normalize(text):
    return text.replace("\r\n", "\n").replace("\r", "\n")


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

    def pattern(self, element, name):
        types = self.types
        try:
            return element.GetCurrentPattern(getattr(types, "UIA_" + name + "Id")).QueryInterface(
                getattr(types, "IUIAutomation" + name))
        except Exception:
            return None

    def focused_editor(self, config):
        focused = self.client.GetFocusedElement()
        if not focused or focused.CurrentIsPassword or not focused.CurrentIsEnabled:
            raise RuntimeError("入力先を取得できないか、パスワード・無効な入力欄です")
        element = focused
        # A focused text child can expose TextPattern on its editable ancestor.
        for _ in range(8):
            pattern = self.pattern(element, "TextPattern")
            if pattern is not None:
                return AutomationEditor(self, focused, element, pattern, config)
            element = self.client.ControlViewWalker.GetParentElement(element)
            if not element:
                break
        raise RuntimeError("この入力欄はUIAのTextPatternによる取得・選択に対応していません")


class AutomationEditor:
    kind = "uia"

    def __init__(self, automation, focused, element, pattern, config):
        self.automation = automation
        self.focused = focused
        self.element = element
        self.pattern = pattern
        self.runtime_id = tuple(focused.GetRuntimeId())
        self.limit = config.max_document_chars
        self.timeout = min(config.editor_timeout_seconds, 2)
        self.window = window_identity()
        self.check_writable()

    def check_focus(self):
        current = self.automation.client.GetFocusedElement()
        if window_identity() != self.window or tuple(current.GetRuntimeId()) != self.runtime_id:
            raise RuntimeError("入力先が変わったため中止しました")

    def check_writable(self):
        if self.focused.CurrentIsPassword or not self.focused.CurrentIsEnabled:
            raise RuntimeError("パスワード・無効な入力欄は対象外です")
        if not self.focused.CurrentHasKeyboardFocus:
            raise RuntimeError("入力欄にキーボードフォーカスがありません")
        value = self.automation.pattern(self.element, "ValuePattern")
        if value is not None and value.CurrentIsReadOnly:
            raise RuntimeError("読み取り専用の入力欄です")
        readonly = self.pattern.DocumentRange.GetAttributeValue(self.automation.types.UIA_IsReadOnlyAttributeId)
        if isinstance(readonly, (bool, int)) and readonly:
            raise RuntimeError("読み取り専用の入力欄です")
        composing = self.automation.pattern(self.element, "TextEditPattern")
        if composing is not None:
            active = composing.GetActiveComposition()
            if active and active.GetText(1):
                raise RuntimeError("IMEの入力を確定してから変換してください")

    def get_text(self, range_):
        text = normalize(range_.GetText(self.limit * 2 + 1))
        if len(text) > self.limit:
            raise RuntimeError("入力欄の本文がmax_document_charsを超えています")
        return text

    def selection(self):
        ranges = self.pattern.GetSelection()
        if ranges and ranges.Length == 1:
            return ranges.GetElement(0)
        if ranges and ranges.Length > 1:
            raise RuntimeError("複数の選択範囲は未対応です")
        pattern2 = self.automation.pattern(self.element, "TextPattern2")
        if pattern2 is not None:
            active, range_ = pattern2.GetCaretRange()
            if active and range_:
                return range_
        raise RuntimeError("この入力欄の選択範囲・カーソルを取得できません")

    def read(self):
        self.check_focus()
        document = self.pattern.DocumentRange
        text = self.get_text(document)
        selection = self.selection()
        prefix = document.Clone()
        prefix.MoveEndpointByRange(1, selection, 0)
        left = self.get_text(prefix)
        prefix.MoveEndpointByRange(1, selection, 1)
        through_selection = self.get_text(prefix)
        if not text.startswith(left) or not text.startswith(through_selection):
            raise RuntimeError("本文とUIA文字範囲を対応づけられません")
        selected = self.get_text(selection)
        start, end = len(left), len(through_selection)
        if text[start:end] != selected:
            raise RuntimeError("UIA選択範囲の本文が一致しません")
        self.check_focus()
        return TextState(text, start, end)

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

    def range_for(self, state, start, end):
        document = self.pattern.DocumentRange
        if self.get_text(document) != state.text:
            raise RuntimeError("文字範囲の準備中に本文が変更されました")
        begin = self.point(document, state.text, start)
        finish = self.point(document, state.text, end)
        begin.MoveEndpointByRange(1, finish, 1)
        if self.get_text(begin) != state.text[start:end]:
            raise RuntimeError("UIAの置換対象範囲を確認できませんでした")
        return begin

    def replace(self, state, start, end, result, expected_tick=None):
        before_tick = input_tick() if expected_tick is None else expected_tick
        events = unicode_events(result)  # validate before changing selection
        self.check_focus()
        self.check_writable()
        check_input_ready(self.window[1])
        selected = self.range_for(state, start, end)
        if input_tick() != before_tick:
            raise RuntimeError("文字範囲の準備中に操作がありました")
        selected.Select()
        deadline = time.monotonic() + self.timeout
        while True:
            self.check_focus()
            if input_tick() != before_tick:
                raise RuntimeError("選択の反映待ちに操作があったため中止しました")
            current = self.selection()
            if (selected.CompareEndpoints(0, current, 0) == 0
                    and selected.CompareEndpoints(1, current, 1) == 0):
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("UIAの選択が反映されませんでした")
            time.sleep(0.01)
        self.check_focus()
        if self.get_text(self.pattern.DocumentRange) != state.text:
            raise RuntimeError("入力前に本文が変わりました")
        check_input_ready(self.window[1])
        self.check_focus()
        if input_tick() != before_tick:
            raise RuntimeError("入力直前に操作がありました")
        send(events)  # never delete first or retry partial input

    def restore_caret(self, state, caret, expected_tick=None):
        self.check_focus()
        selected = self.range_for(state, caret, caret)
        self.check_focus()
        if expected_tick is not None and input_tick() != expected_tick:
            raise RuntimeError("カーソル復元前に操作がありました")
        selected.Select()

    def close(self):
        pass
