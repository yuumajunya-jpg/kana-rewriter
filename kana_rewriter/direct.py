"""Isolated UIA/Win32 editor worker; no clipboard imports or API calls."""
import logging
import multiprocessing
import time
import sys

from .editor import TextState, make_capture, replacement_plan
from .timing import stage, DeferredHandler

logger = logging.getLogger(__name__)


class EditorEngine:
    def __init__(self, config):
        self.config = config
        self.automation = None
        self.saved = None
        self.counter = 0
        self.trigger_keys = ()

    @stage("起動/編集環境初期化")
    def warmup(self):
        from .direct_win32 import input_tick
        input_tick()
        if self.automation is None and self.config.edit_backend in {"auto", "uia"}:
            try:
                from .direct_uia import Automation
                self.automation = Automation()
            except Exception:
                if self.config.edit_backend == "uia":
                    raise
                logger.debug("UIA事前初期化を省略", exc_info=True)
        return True

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

    @stage("取得/全体")
    def capture(self, mode, max_chars, trigger_keys=()):
        from .direct_win32 import window_identity, input_tick, check_input_ready
        target = window_identity()
        # Reading does not synthesize input: inference can begin on key-down.
        check_input_ready(target[1], allow_modifiers=True)
        tick = input_tick()
        with stage("取得/入力欄の特定"):
            editor = self.editor()
        with stage("取得/本文と位置"):
            state = editor.read()
        logger.debug("取得: 方式=%s / 本文文字数=%s / 選択=%s:%s / 左側=%r / 選択文字=%r / 右側=%r",
                     editor.kind, len(state.text), state.start, state.end,
                     state.text[max(0, state.start - 80):state.start],
                     state.text[state.start:min(state.end, state.start + 80)],
                     state.text[state.end:state.end + 80])
        # Pointer motion and releases are excluded by the activity monitor.
        # Re-read on editing input to verify the captured state.
        if input_tick() != tick and editor.read() != state:
            raise RuntimeError("本文の取得中に本文または選択位置が変わったため中止しました")
        if window_identity() != target:
            raise RuntimeError("本文の取得中に入力先が変わったため中止しました")
        self.counter += 1
        try:
            capture = make_capture(state, (target, tick), mode, self.config, self.counter)
        except ValueError as exc:
            raise ValueError(f"{exc}（{editor.kind}: 取得本文{len(state.text)}文字、"
                             f"カーソル/選択{state.start}:{state.end}。--debugで取得内容を確認）") from exc
        self.saved = (capture, editor, state)
        self.trigger_keys = tuple(trigger_keys)
        logger.debug("編集方式=%s / 対象=%r / 左文脈末尾=%r / 右文脈先頭=%r",
                     editor.kind, capture.source, capture.left_context[-self.config.context_chars:],
                     capture.right_context[:self.config.context_chars])
        return capture

    @stage("適用/全体（IME復元を含む）")
    def apply(self, capture, result):
        editor = self.saved[1] if self.saved is not None and self.saved[0] == capture else None
        try:
            return self.apply_once(capture, result)
        finally:
            finish = getattr(editor, "finish_edit", None)
            if finish is not None:
                failing = sys.exc_info()[0] is not None
                try:
                    with stage("適用/IME復元"):
                        finish()
                except Exception as exc:
                    if not failing:
                        raise RuntimeError(f"編集後のIME復元に失敗しました。本文とIME状態を確認してください: {exc}") from exc
                    logger.warning("中止後のIME復元に失敗しました: %s", exc)

    def apply_once(self, capture, result):
        from .direct_win32 import window_identity, input_tick, wait_input_release
        if self.saved is None or self.saved[0] != capture:
            raise RuntimeError("取得した編集対象が無効になっています")
        saved, editor, state = self.saved
        self.saved = None  # an edit token is consumed even on failure; never replay
        target, _ = capture.stamp
        wait_input_release(target, self.trigger_keys)
        if window_identity() != target:
            raise RuntimeError("待機中に入力先が変わったため中止しました")
        # Built-in backends validate the snapshot immediately before selecting.
        # Other backends keep the engine-side check unless they opt in explicitly.
        if getattr(editor, "validates_before_replace", False) is not True:
            with stage("適用/推論後の本文・位置再確認"):
                if editor.read() != state:
                    raise RuntimeError("待機中に本文または選択位置が変わったため中止しました")
        # Activity during inference is harmless when the editor state is still
        # identical. Keep a fresh activity guard for the short mutation phase.
        tick = input_tick()
        start, end, expected, caret = replacement_plan(capture, result)
        if not result or "\x00" in result or len(expected) > self.config.max_document_chars:
            raise ValueError("差し替え結果が空、不正、または本文上限を超えています")
        logger.debug("部分置換=%s:%s / 結果=%r / 復元位置=%s", start, end, result, caret)
        # Verify again after reading the text, before making any selection.
        if window_identity() != target or input_tick() != tick:
            raise RuntimeError("適用直前に操作があったため中止しました")
        with stage("適用/選択と文字送信"):
            editor.replace(state, start, end, result, expected_tick=tick)
        with stage("適用/入力反映確認"):
            deadline = time.monotonic() + min(self.config.editor_timeout_seconds, 2)
            insertion_end = start + len(result)
            last_status = None
            wait_for_state = getattr(editor, "wait_for_state", None)
            while True:
                if window_identity() != target:
                    raise RuntimeError("入力後に入力先が変わりました。編集は再送しません")
                try:
                    updated = (wait_for_state(TextState(expected, insertion_end, insertion_end), deadline)
                               if wait_for_state is not None else editor.read())
                except Exception as exc:
                    raise RuntimeError(f"入力後の確認に失敗しました。編集は再送しません。本文を確認してください: {exc}") from exc
                status = (updated.text, updated.start, updated.end)
                if status != last_status:
                    logger.debug("反映確認: 本文一致=%s / 本文文字数=%s（期待%s） / 選択=%s:%s（期待%s:%s）",
                                 updated.text == expected, len(updated.text), len(expected),
                                 updated.start, updated.end, insertion_end, insertion_end)
                    last_status = status
                if updated.text == expected and updated.start == updated.end == insertion_end:
                    break
                if editor.kind != "uia" or time.monotonic() >= deadline:
                    raise RuntimeError("差し替え結果を確認できませんでした。本文を確認してください。編集は再送しません")
                time.sleep(0.01)
        after_input_tick = input_tick()
        with stage("適用/カーソル復元と確認"):
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


def _worker(connection, config, debug, timings=False):
    diagnostics = DeferredHandler()
    logging.basicConfig(level=logging.DEBUG if debug else logging.WARNING,
                        handlers=[diagnostics], force=True)
    logging.getLogger("comtypes").setLevel(logging.WARNING)
    if timings:
        logging.getLogger("kana_rewriter.timing").setLevel(logging.DEBUG)
    engine = EditorEngine(config)
    try:
        while True:
            method, args = connection.recv()
            if method == "close":
                break
            try:
                result = (True, getattr(engine, method)(*args))
            except Exception as exc:
                result = (False, str(exc))
            connection.send((*result, diagnostics.export()))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        from .input_activity import close_monitor
        close_monitor()
        connection.close()


class Desktop:
    def __init__(self, config, debug=False, timings=False):
        self.config = config
        self.debug = debug
        self.timings = timings
        self.process = None
        self.connection = None
        self.failed = False

    def _start(self):
        context = multiprocessing.get_context("spawn")
        self.connection, child = context.Pipe()
        self.process = context.Process(target=_worker, args=(child, self.config, self.debug, self.timings), daemon=True)
        self.process.start()
        child.close()

    @stage("通信/編集ワーカー往復")
    def _call(self, method, *args):
        if self.failed:
            raise RuntimeError("編集ワーカーが停止しています。プログラムを再起動してください")
        if self.process is None:
            self._start()
        try:
            self.connection.send((method, args))
            if not self.connection.poll(self.config.editor_timeout_seconds + 3):
                raise TimeoutError()
            success, result, diagnostics = self.connection.recv()
        except (EOFError, BrokenPipeError, OSError, TimeoutError) as exc:
            self.failed = True
            self.close()
            raise RuntimeError("編集ワーカーの応答を確認できません。編集は再送しません。本文を確認して再起動してください") from exc
        for packet in diagnostics:
            logging.getLogger(packet["name"]).handle(logging.makeLogRecord(packet))
        if not success:
            raise RuntimeError(result)
        return result

    def capture(self, mode, max_chars, trigger_keys=()):
        return self._call("capture", mode, max_chars, trigger_keys)

    def warmup(self):
        return self._call("warmup")

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
