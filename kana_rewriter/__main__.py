import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import ctypes
import sys
import time
import logging
import json
from dataclasses import dataclass, replace

from .core import Config, Converter, apply_result
from .hotkeys import parse_hotkey
from .timing import stage, buffered_logs, flush_logs

logger = logging.getLogger(__name__)


@dataclass
class PendingConversion:
    future: object
    captured: object
    started: float
    capture_seconds: float
    japanese_trigger: bool = False
    commit: object = None
    prepared: object = None


class FixedConverter:
    """Exercise the normal hotkey/edit path without loading or calling AI."""
    def __init__(self, source, result):
        self.source, self.result = source, result

    def convert_selection(self, source, left_context, right_context):
        if source != self.source:
            raise RuntimeError("比較用の変換対象と一致しないため中止しました")
        return self.result


@stage("変換/AI呼び出し")
def timed_conversion(converter, captured):
    started = time.perf_counter()
    result = converter.convert_selection(captured.source, captured.left_context, captured.right_context)
    return result, time.perf_counter() - started


def main():
    parser = argparse.ArgumentParser(description="確定済み日本語をローカルAIでかな漢字変換")
    parser.add_argument("--config", help="TOML設定ファイル（省略時は既定値）")
    parser.add_argument("--text", help="ウィンドウを操作せず、接続・変換を確認")
    parser.add_argument("--preview", action=argparse.BooleanOptionalAction, default=None,
                        help="入力欄の近くの変換プレビューを切り替え")
    parser.add_argument("--left-context", default="", help="--textの左文脈")
    parser.add_argument("--right-context", default="", help="--textの右文脈")
    parser.add_argument("--debug", action="store_true", help="変換対象・文脈・モデル出力・編集方式を端末に表示")
    parser.add_argument("--timings", action="store_true", help="本文を表示せず、編集完了後に工程時間を表示")
    parser.add_argument("--diagnostic-log", nargs="?", const="", metavar="PATH",
                        help="詳細診断と端末出力をUTF-8ログへ保存（省略時logs/へ自動命名）")
    parser.add_argument("--inspect", action="store_true", help="3秒後の入力欄の直接編集能力を表示（文字変更なし）")
    parser.add_argument("--auto-replace-on-ime-commit", action=argparse.BooleanOptionalAction, default=None,
                        help="TSFによるEnter確定後の自動変換を今回だけ切り替え（Python直接編集のみ）")
    parser.add_argument("--benchmark-source", help="AIなし比較用の変換対象（完全一致時のみ置換）")
    parser.add_argument("--benchmark-result", help="AIなし比較用の固定変換結果")
    args = parser.parse_args()
    if args.diagnostic_log is not None:
        args.debug = True
    benchmarking = args.benchmark_source is not None or args.benchmark_result is not None
    if benchmarking and (not args.benchmark_source or not args.benchmark_result):
        parser.error("比較には空でない--benchmark-sourceと--benchmark-resultが必要です")
    if benchmarking and (args.text is not None or args.inspect):
        parser.error("比較モードと--text・--inspectは併用できません")
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="[診断] %(message)s")
    elif args.timings:
        logging.basicConfig(level=logging.WARNING, format="[診断] %(message)s")
    if args.timings:
        logging.getLogger("kana_rewriter.timing").setLevel(logging.DEBUG)
        logger.setLevel(logging.DEBUG)
    if args.diagnostic_log is not None:
        from .diagnostic_log import DiagnosticLog
        try:
            with DiagnosticLog(args.diagnostic_log):
                return _run(args, benchmarking)
        except OSError as exc:
            print(f"診断ログを開けません: {exc}", file=sys.stderr)
            return 1
    return _run(args, benchmarking)


def _run(args, benchmarking):
    try:
        config = Config.load(args.config) if args.config else Config()
        overrides = {}
        if args.auto_replace_on_ime_commit is not None:
            overrides["auto_replace_on_ime_commit"] = args.auto_replace_on_ime_commit
        if overrides:
            config = replace(config, **overrides)
        logger.debug("診断設定: edit_backend=%s / auto_ime=%s / timeout=%s",
                     config.edit_backend,
                     config.auto_replace_on_ime_commit, config.editor_timeout_seconds)
        converter = FixedConverter(args.benchmark_source, args.benchmark_result) if benchmarking else Converter(config)
        if args.inspect:
            if sys.platform != "win32":
                raise RuntimeError("入力欄の診断はWindows専用です")
            from .direct import Desktop
            print("3秒以内に診断する入力欄へフォーカスを移してください", flush=True)
            time.sleep(3)
            desktop = Desktop(config, debug=args.debug, timings=args.timings)
            try:
                print(json.dumps(desktop.inspect(), ensure_ascii=False, indent=2))
            finally:
                desktop.close()
            return 0
        if args.text is not None:
            print(converter.convert_selection(args.text, args.left_context, args.right_context))
            return 0
        if sys.platform != "win32":
            raise RuntimeError("ショートカットでの変換はWindows専用です")
        if benchmarking:
            print("AIなし試験: 一致する対象だけを固定結果へ置換します", flush=True)
        else:
            print("GGUFモデルを読み込んでいます…", flush=True)
            converter.load_model()
        return run_windows(converter, config, debug=args.debug, timings=args.timings,
                           preview=config.preview_enabled if args.preview is None else args.preview)
    except Exception as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


@buffered_logs()
def run_windows(converter, config, debug=False, timings=False, preview=False):
    from .winapi import user, W, MessageWaiter
    bindings = {1: parse_hotkey(config.hotkey_line), 2: parse_hotkey(config.hotkey_selection)}
    if config.hotkey_quit:
        bindings[3] = parse_hotkey(config.hotkey_quit)
    registered = []
    actions = {}
    japanese_release = None
    auto_monitor = None
    tsf_editor = None
    pool = ThreadPoolExecutor(max_workers=1)
    pending = None
    live_preview = None
    from .direct import Desktop
    desktop = Desktop(config, debug=debug, timings=timings)
    try:
        waiter = MessageWaiter()
        if any(binding.key == 0xF3 for binding in bindings.values()):
            from .japanese_hotkey import JapaneseKeyRelease
            japanese_release = JapaneseKeyRelease().start()
        for ident, binding in bindings.items():
            for index, key in enumerate(binding.keys):
                registration = ident + index * 10
                if not user.RegisterHotKey(None, registration, 0x4000 | binding.modifiers, key):
                    raise RuntimeError(f"ショートカット {binding.label} を登録できません。他アプリとの競合やOSの予約キーを確認してください")
                registered.append(registration)
                actions[registration] = ident
        desktop.warmup()
        if preview:
            from .preview import LivePreview
            live_preview = LivePreview(config, converter, pool)
            print("変換プレビュー: 有効（TSF対応入力欄／変換候補を随時更新）", flush=True)
        if config.auto_replace_on_ime_commit:
            from .auto_ime import AutoImeMonitor
            from .tsf_edit import TsfCommitEditor
            tsf_editor = TsfCommitEditor(config.tsf_edit_path)
            auto_monitor = AutoImeMonitor(config, waiter.wake, probe_factory=tsf_editor.probe_factory)
            auto_monitor.start()
        flush_logs()
        quit_label = f" / {bindings[3].label} = 終了" if 3 in bindings else ""
        print(f"起動: {bindings[1].label} = カーソル左の区切りまで / "
              f"{bindings[2].label} = 選択範囲{quit_label} / この端末でCtrl+C = 終了", flush=True)
        if auto_monitor is not None:
            print("IME確定後の自動変換: 有効（TSF直接編集／Enter確定／入力先に追従）", flush=True)
        message = W.MSG()
        while True:
            while user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                action = actions.get(message.wParam) if message.message == 0x0312 else None
                if action == 3:
                    print("終了します", flush=True)
                    return 0
                if action in (1, 2) and pending is None:
                    if live_preview is not None:
                        live_preview.paused.set()
                    if auto_monitor is not None:
                        auto_monitor.begin()
                    try:
                        started = time.perf_counter()
                        japanese_trigger = bindings[action].key == 0xF3
                        # NLS pseudo-key states can remain latched even after
                        # release. The parent checks physical release instead.
                        trigger_keys = () if japanese_trigger else bindings[action].keys
                        captured = desktop.capture("line" if action == 1 else "selection",
                                                   config.max_chars, trigger_keys=trigger_keys)
                        capture_seconds = time.perf_counter() - started
                        future = pool.submit(timed_conversion, converter, captured)
                        pending = PendingConversion(future, captured, started, capture_seconds, japanese_trigger)
                        future.add_done_callback(waiter.wake)
                        print("変換中…", flush=True)
                    except Exception as exc:
                        if live_preview is not None:
                            live_preview.paused.clear()
                        if auto_monitor is not None:
                            auto_monitor.finish()
                        flush_logs()
                        print(f"中止: {exc}", flush=True)
            if auto_monitor is not None and pending is None:
                commit = auto_monitor.take()
                if commit is not None and auto_monitor.begin(commit):
                    if live_preview is not None:
                        live_preview.paused.set()
                    try:
                        started = time.perf_counter()
                        prepared = tsf_editor.prepare(commit)
                        captured = tsf_editor.capture(prepared, config.context_chars,
                                                      config.model_format not in {'jinen_v1', 'jinen_v2'})
                        capture_seconds = time.perf_counter() - started
                        if not auto_monitor.valid(commit):
                            raise RuntimeError("取得中に入力・フォーカスが変わったため中止しました")
                        future = pool.submit(timed_conversion, converter, captured)
                        pending = PendingConversion(future, captured, started, capture_seconds,
                                                    commit=commit, prepared=prepared)
                        future.add_done_callback(waiter.wake)
                        print("IME確定: 変換中…", flush=True)
                    except Exception as exc:
                        if live_preview is not None:
                            live_preview.paused.clear()
                        auto_monitor.finish()
                        flush_logs()
                        print(f"自動変換を中止: {exc}", flush=True)
                        from .tsf_edit import RestartRequired
                        if isinstance(exc, RestartRequired):
                            return 1
            if pending is not None and pending.future.done():
                job = pending
                pending = None
                try:
                    result, inference_seconds = job.future.result()
                    applying = time.perf_counter()
                    if job.japanese_trigger:
                        japanese_release.wait_released()
                    if job.commit is not None and not auto_monitor.valid(job.commit):
                        raise RuntimeError("推論中に入力・フォーカスが変わったため自動変換を中止しました")
                    changed = (tsf_editor.apply(job.prepared, job.captured, result) if job.prepared is not None
                               else apply_result(desktop, job.captured, result))
                    finished = time.perf_counter()
                    logger.debug("時間集計: 取得=%.1fms / AI=%.1fms / 適用=%.1fms / AI以外=%.1fms / 合計=%.1fms",
                                 job.capture_seconds * 1000, inference_seconds * 1000,
                                 (finished - applying) * 1000,
                                 (finished - job.started - inference_seconds) * 1000,
                                 (finished - job.started) * 1000)
                    flush_logs()
                    print("差し替え処理を実行しました" if changed else "変換の必要はありません", flush=True)
                except Exception as exc:
                    flush_logs()
                    print(f"中止: {exc}", flush=True)
                    if job.prepared is not None:
                        from .tsf_edit import RestartRequired
                        if isinstance(exc, RestartRequired):
                            return 1
                finally:
                    if live_preview is not None:
                        live_preview.paused.clear()
                    if auto_monitor is not None:
                        auto_monitor.finish()
            if auto_monitor is not None:
                flush_logs()
            waiter.wait()
    except KeyboardInterrupt:
        print("終了します", flush=True)
    finally:
        # Attempt every cleanup even if an earlier callback fails. ExitStack
        # retains the exception chain while releasing callbacks in this order:
        # monitor, hotkeys, executor, editor, Japanese-key monitor.
        with ExitStack() as cleanup:
            if japanese_release is not None:
                cleanup.callback(japanese_release.close)
            cleanup.callback(desktop.close)
            cleanup.callback(pool.shutdown, wait=True, cancel_futures=True)
            for ident in reversed(registered):
                cleanup.callback(user.UnregisterHotKey, None, ident)
            if auto_monitor is not None:
                cleanup.callback(auto_monitor.close)
            if live_preview is not None:
                cleanup.callback(live_preview.close)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
