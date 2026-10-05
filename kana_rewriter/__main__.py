import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
import sys
import time
import logging
import json
from dataclasses import replace

from .core import Config, Converter, apply_result
from .hotkeys import parse_hotkey
from .timing import stage, buffered_logs, flush_logs

logger = logging.getLogger(__name__)


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
    parser.add_argument("--left-context", default="", help="--textの左文脈")
    parser.add_argument("--right-context", default="", help="--textの右文脈")
    parser.add_argument("--debug", action="store_true", help="変換対象・文脈・モデル出力・編集方式を端末に表示")
    parser.add_argument("--timings", action="store_true", help="本文を表示せず、編集完了後に工程時間を表示")
    parser.add_argument("--inspect", action="store_true", help="3秒後の入力欄の直接編集能力を表示（文字変更なし）")
    parser.add_argument("--editor-worker", choices=("python", "native"), help="設定ファイルの編集ワーカーを今回だけ上書き")
    parser.add_argument("--benchmark-source", help="AIなし比較用の変換対象（完全一致時のみ置換）")
    parser.add_argument("--benchmark-result", help="AIなし比較用の固定変換結果")
    args = parser.parse_args()
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
    try:
        config = Config.load(args.config) if args.config else Config()
        if args.editor_worker:
            config = replace(config, editor_worker=args.editor_worker)
        if benchmarking and config.edit_backend == "clipboard":
            raise RuntimeError("編集ワーカー比較には直接編集の設定を使用してください")
        converter = FixedConverter(args.benchmark_source, args.benchmark_result) if benchmarking else Converter(config)
        if args.inspect:
            if sys.platform != "win32":
                raise RuntimeError("入力欄の診断はWindows専用です")
            if config.editor_worker == "native":
                from .native import Desktop
            else:
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
            print(f"AIなし比較: editor_worker={config.editor_worker} / 一致する対象だけを固定結果へ置換します", flush=True)
        elif config.backend == "llama_cpp":
            print("GGUFモデルを読み込んでいます…", flush=True)
            converter.load_model()
        return run_windows(converter, config, debug=args.debug, timings=args.timings)
    except Exception as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


@buffered_logs()
def run_windows(converter, config, debug=False, timings=False):
    from .winapi import user, W, MessageWaiter
    bindings = {1: parse_hotkey(config.hotkey_line), 2: parse_hotkey(config.hotkey_selection)}
    if config.hotkey_quit:
        bindings[3] = parse_hotkey(config.hotkey_quit)
    registered = []
    actions = {}
    pool = ThreadPoolExecutor(max_workers=1)
    pending = None
    if config.edit_backend == "clipboard":
        from .windows import Desktop
        desktop = Desktop(paste_wait_seconds=config.paste_wait_seconds,
                          conversion_delimiters=config.conversion_delimiters,
                          stop_at_kanji=config.stop_at_kanji,
                          trailing_punctuation=config.trailing_punctuation)
    else:
        if config.editor_worker == "native":
            from .native import Desktop
        else:
            from .direct import Desktop
        desktop = Desktop(config, debug=debug, timings=timings)
    try:
        waiter = MessageWaiter()
        for ident, binding in bindings.items():
            for index, key in enumerate(binding.keys):
                registration = ident + index * 10
                if not user.RegisterHotKey(None, registration, 0x4000 | binding.modifiers, key):
                    raise RuntimeError(f"ショートカット {binding.label} を登録できません。他アプリとの競合やOSの予約キーを確認してください")
                registered.append(registration)
                actions[registration] = ident
        if config.edit_backend != "clipboard":
            desktop.warmup()
        flush_logs()
        quit_label = f" / {bindings[3].label} = 終了" if 3 in bindings else ""
        print(f"起動: {bindings[1].label} = カーソル左の区切りまで / "
              f"{bindings[2].label} = 選択範囲{quit_label} / この端末でCtrl+C = 終了", flush=True)
        message = W.MSG()
        while True:
            while user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                action = actions.get(message.wParam) if message.message == 0x0312 else None
                if action == 3:
                    print("終了します", flush=True)
                    return 0
                if action in (1, 2) and pending is None:
                    try:
                        started = time.perf_counter()
                        captured = desktop.capture("line" if action == 1 else "selection",
                                                   config.max_chars, trigger_keys=bindings[action].keys)
                        capture_seconds = time.perf_counter() - started
                        future = pool.submit(timed_conversion, converter, captured)
                        pending = (future, captured, started, capture_seconds)
                        future.add_done_callback(waiter.wake)
                        print("変換中…", flush=True)
                    except Exception as exc:
                        flush_logs()
                        print(f"中止: {exc}", flush=True)
            if pending is not None and pending[0].done():
                future, captured, started, capture_seconds = pending
                pending = None
                try:
                    result, inference_seconds = future.result()
                    applying = time.perf_counter()
                    changed = apply_result(desktop, captured, result)
                    finished = time.perf_counter()
                    logger.debug("時間集計: 取得=%.1fms / AI=%.1fms / 適用=%.1fms / AI以外=%.1fms / 合計=%.1fms",
                                 capture_seconds * 1000, inference_seconds * 1000,
                                 (finished - applying) * 1000,
                                 (finished - started - inference_seconds) * 1000,
                                 (finished - started) * 1000)
                    flush_logs()
                    print("差し替え処理を実行しました" if changed else "変換の必要はありません", flush=True)
                except Exception as exc:
                    flush_logs()
                    print(f"中止: {exc}", flush=True)
            waiter.wait()
    except KeyboardInterrupt:
        print("終了します", flush=True)
    finally:
        for ident in registered:
            user.UnregisterHotKey(None, ident)
        pool.shutdown(wait=True, cancel_futures=True)
        desktop.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
