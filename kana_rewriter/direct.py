"""Isolated UIA/Win32 editor worker; no clipboard imports or API calls."""
import logging
import multiprocessing
import time

from .editor import make_capture, replacement_plan

logger = logging.getLogger(__name__)


class EditorEngine:
    def __init__(self, config):
        self.config = config
        self.automation = None
        self.saved = None
        self.counter = 0

    def editor(self):
        from .direct_win32 import NativeEditor, is_standard_edit, window_identity
        hwnd = window_identity()[1]
        if self.config.edit_backend in {"auto", "win32"} and is_standard_edit(hwnd):
            return NativeEditor(hwnd, self.config)
        if self.config.edit_backend == "win32":
            raise RuntimeError("この入力欄はWin32の標準Edit/RichEditではありません")
        if self.automation is None:
            from .direct_uia import Automation
            self.automation = Automation()
        return self.automation.focused_editor(self.config)

    def capture(self, mode, max_chars, trigger_keys=()):
        from .direct_win32 import window_identity, input_tick, check_input_ready
        from .winapi import user
        target = window_identity()
        deadline = time.monotonic() + 2
        keys = (0x10, 0x11, 0x12, 0x5B, 0x5C) + tuple(trigger_keys)
        while any(user.GetAsyncKeyState(key) & 0x8000 for key in keys):
            if time.monotonic() >= deadline:
                raise RuntimeError("ショートカットキーを離してください")
            time.sleep(0.01)
        if window_identity() != target:
            raise RuntimeError("入力先が変わりました")
        check_input_ready(target[1])
        tick = input_tick()
        editor = self.editor()
        state = editor.read()
        if window_identity() != target or input_tick() != tick:
            raise RuntimeError("本文の取得中に操作があったため中止しました")
        self.counter += 1
        capture = make_capture(state, (target, tick), mode, self.config, self.counter)
        self.saved = (capture, editor, state)
        logger.debug("編集方式=%s / 対象=%r / 左文脈末尾=%r / 右文脈先頭=%r",
                     editor.kind, capture.source, capture.left_context[-self.config.context_chars:],
                     capture.right_context[:self.config.context_chars])
        return capture

    def apply(self, capture, result):
        from .direct_win32 import window_identity, input_tick
        if self.saved is None or self.saved[0] != capture:
            raise RuntimeError("取得した編集対象が無効になっています")
        saved, editor, state = self.saved
        self.saved = None  # an edit token is consumed even on failure; never replay
        target, tick = capture.stamp
        if window_identity() != target or input_tick() != tick:
            raise RuntimeError("待機中に操作または入力先変更があったため中止しました")
        if editor.read() != state:
            raise RuntimeError("待機中に本文または選択位置が変わったため中止しました")
        start, end, expected, caret = replacement_plan(capture, result)
        if not result or "\x00" in result or len(expected) > self.config.max_document_chars:
            raise ValueError("差し替え結果が空、不正、または本文上限を超えています")
        logger.debug("部分置換=%s:%s / 結果=%r / 復元位置=%s", start, end, result, caret)
        # Verify again after reading the text, before making any selection.
        if window_identity() != target or input_tick() != tick:
            raise RuntimeError("適用直前に操作があったため中止しました")
        editor.replace(state, start, end, result, expected_tick=tick)
        deadline = time.monotonic() + min(self.config.editor_timeout_seconds, 2)
        while True:
            if window_identity() != target:
                raise RuntimeError("入力後に入力先が変わりました。編集は再送しません")
            updated = editor.read()
            if updated.text == expected:
                break
            if editor.kind != "uia" or time.monotonic() >= deadline:
                raise RuntimeError("差し替え結果を確認できませんでした。本文を確認してください。編集は再送しません")
            time.sleep(0.01)
        insertion_end = start + len(result)
        if updated.start != insertion_end or updated.end != insertion_end:
            raise RuntimeError("入力後の選択位置が想定と異なります。カーソル復元を中止しました")
        after_input_tick = input_tick()
        if caret != insertion_end:
            if input_tick() != after_input_tick or window_identity() != target:
                raise RuntimeError("カーソル復元前に操作がありました")
            editor.restore_caret(updated, caret, expected_tick=after_input_tick)
            deadline = time.monotonic() + min(self.config.editor_timeout_seconds, 2)
            while True:
                if window_identity() != target or input_tick() != after_input_tick:
                    raise RuntimeError("カーソル復元中に操作がありました")
                restored = editor.read()
                if restored.text == expected and restored.start == restored.end == caret:
                    break
                if window_identity() != target or input_tick() != after_input_tick:
                    raise RuntimeError("カーソル復元中に操作がありました")
                if time.monotonic() >= deadline:
                    raise RuntimeError("文字列は置換しましたが、カーソルの復元を確認できませんでした")
                time.sleep(0.01)
        return True

    def inspect(self):
        from .direct_win32 import window_identity, class_name
        identity = window_identity()
        report = {"foreground": identity[0], "focus_hwnd": identity[1], "pid": identity[2],
                  "class": class_name(identity[1]), "clipboard": "unused"}
        try:
            editor = self.editor()
            state = editor.read()
            report.update(backend=editor.kind, document_chars=len(state.text),
                          selection=[state.start, state.end])
        except Exception as exc:
            report["unsupported_reason"] = str(exc)
        return report


def _worker(connection, config, debug):
    if debug:
        logging.basicConfig(level=logging.DEBUG, format="[診断] %(message)s")
        logging.getLogger("comtypes").setLevel(logging.WARNING)
    engine = EditorEngine(config)
    try:
        while True:
            method, args = connection.recv()
            if method == "close":
                break
            try:
                connection.send((True, getattr(engine, method)(*args)))
            except Exception as exc:
                connection.send((False, str(exc)))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        connection.close()


class Desktop:
    def __init__(self, config, debug=False):
        self.config = config
        self.debug = debug
        self.process = None
        self.connection = None
        self.failed = False

    def _start(self):
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=_worker, args=(child, self.config, self.debug), daemon=True)
        self.process.start()
        child.close()

    def _call(self, method, *args):
        if self.failed:
            raise RuntimeError("編集ワーカーが停止しています。プログラムを再起動してください")
        if self.process is None:
            self._start()
        try:
            self.connection.send((method, args))
            if not self.connection.poll(self.config.editor_timeout_seconds + 3):
                raise TimeoutError()
            success, result = self.connection.recv()
        except (EOFError, BrokenPipeError, OSError, TimeoutError) as exc:
            self.failed = True
            self.close()
            raise RuntimeError("編集ワーカーの応答を確認できません。編集は再送しません。本文を確認して再起動してください") from exc
        if not success:
            raise RuntimeError(result)
        return result

    def capture(self, mode, max_chars, trigger_keys=()):
        return self._call("capture", mode, max_chars, trigger_keys)

    def apply(self, capture, result):
        return self._call("apply", capture, result)

    def inspect(self):
        return self._call("inspect")

    def close(self):
        if self.process is not None:
            if self.process.is_alive():
                if not self.failed:
                    try:
                        self.connection.send(("close", ()))
                    except (BrokenPipeError, OSError):
                        pass
                    self.process.join(timeout=0.5)
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(timeout=2)
            self.connection.close()
            self.process = None
            self.connection = None
