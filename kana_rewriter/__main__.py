import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
import sys
import time
import logging
import json

from .core import Config, Converter, apply_result
from .hotkeys import parse_hotkey


def main():
    parser = argparse.ArgumentParser(description="確定済み日本語をローカルAIでかな漢字変換")
    parser.add_argument("--config", help="TOML設定ファイル（省略時は既定値）")
    parser.add_argument("--text", help="ウィンドウを操作せず、接続・変換を確認")
    parser.add_argument("--left-context", default="", help="--textの左文脈")
    parser.add_argument("--right-context", default="", help="--textの右文脈")
    parser.add_argument("--debug", action="store_true", help="変換対象・文脈・モデル出力・編集方式を端末に表示")
    parser.add_argument("--inspect", action="store_true", help="3秒後の入力欄の直接編集能力を表示（文字変更なし）")
    args = parser.parse_args()
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="[診断] %(message)s")
    try:
        config = Config.load(args.config) if args.config else Config()
        converter = Converter(config)
        if args.inspect:
            if sys.platform != "win32":
                raise RuntimeError("入力欄の診断はWindows専用です")
            from .direct import Desktop
            print("3秒以内に診断する入力欄へフォーカスを移してください", flush=True)
            time.sleep(3)
            desktop = Desktop(config, debug=args.debug)
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
        if config.backend == "llama_cpp":
            print("GGUFモデルを読み込んでいます…", flush=True)
            converter.load_model()
        return run_windows(converter, config, debug=args.debug)
    except Exception as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


def run_windows(converter, config, debug=False):
    from .winapi import user, W
    bindings = {1: parse_hotkey(config.hotkey_line), 2: parse_hotkey(config.hotkey_selection)}
    if config.hotkey_quit:
        bindings[3] = parse_hotkey(config.hotkey_quit)
    registered = []
    pool = ThreadPoolExecutor(max_workers=1)
    pending = None
    if config.edit_backend == "clipboard":
        from .windows import Desktop
        desktop = Desktop(paste_wait_seconds=config.paste_wait_seconds,
                          conversion_delimiters=config.conversion_delimiters,
                          stop_at_kanji=config.stop_at_kanji,
                          trailing_punctuation=config.trailing_punctuation)
    else:
        from .direct import Desktop
        desktop = Desktop(config, debug=debug)
    try:
        for ident, binding in bindings.items():
            if not user.RegisterHotKey(None, ident, 0x4000 | binding.modifiers, binding.key):
                raise RuntimeError(f"ショートカット {binding.label} を登録できません。他アプリとの競合やOSの予約キーを確認してください")
            registered.append(ident)
        quit_label = f" / {bindings[3].label} = 終了" if 3 in bindings else ""
        print(f"起動: {bindings[1].label} = カーソル左の区切りまで / "
              f"{bindings[2].label} = 選択範囲{quit_label} / この端末でCtrl+C = 終了", flush=True)
        message = W.MSG()
        while True:
            while user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                if message.message == 0x0312 and message.wParam == 3 and 3 in bindings:
                    print("終了します", flush=True)
                    return 0
                if message.message == 0x0312 and message.wParam in (1, 2) and pending is None:
                    try:
                        captured = desktop.capture("line" if message.wParam == 1 else "selection",
                                                   config.max_chars, trigger_keys=(bindings[message.wParam].key,))
                        pending = (pool.submit(converter.convert_selection, captured.source,
                                               captured.left_context, captured.right_context), captured)
                        print("変換中（操作すると差し替えを中止します）…", flush=True)
                    except Exception as exc:
                        print(f"中止: {exc}", flush=True)
            if pending is not None and pending[0].done():
                future, captured = pending
                pending = None
                try:
                    changed = apply_result(desktop, captured, future.result())
                    print("差し替え処理を実行しました" if changed else "変換の必要はありません", flush=True)
                except Exception as exc:
                    print(f"中止: {exc}", flush=True)
            time.sleep(0.02)
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
