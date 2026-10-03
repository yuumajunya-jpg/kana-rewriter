import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
import sys
import time
import logging

from .core import Config, Converter, apply_result


def main():
    parser = argparse.ArgumentParser(description="確定済み日本語をローカルAIでかな漢字変換")
    parser.add_argument("--config", help="TOML設定ファイル（省略時は既定値）")
    parser.add_argument("--text", help="ウィンドウを操作せず、接続・変換を確認")
    parser.add_argument("--left-context", default="", help="--textの左文脈")
    parser.add_argument("--right-context", default="", help="--textの右文脈")
    parser.add_argument("--debug", action="store_true", help="取得文字列・モデル出力・完成文・貼り付け方式を端末に表示")
    args = parser.parse_args()
    if args.debug:
        logging.basicConfig(level=logging.DEBUG, format="[診断] %(message)s")
    try:
        config = Config.load(args.config) if args.config else Config()
        converter = Converter(config)
        if args.text is not None:
            print(converter.convert_selection(args.text, args.left_context, args.right_context))
            return 0
        if sys.platform != "win32":
            raise RuntimeError("ショートカットでの変換はWindows専用です")
        if config.backend == "llama_cpp":
            print("GGUFモデルを読み込んでいます…", flush=True)
            converter.load_model()
        return run_windows(converter, config)
    except Exception as exc:
        print(f"エラー: {exc}", file=sys.stderr)
        return 1


def run_windows(converter, config):
    from .windows import Desktop, user, W
    registered = []
    pool = ThreadPoolExecutor(max_workers=1)
    pending = None
    desktop = Desktop(paste_wait_seconds=config.paste_wait_seconds)
    try:
        for ident, key in ((1, 0x4B), (2, 0x4A)):
            if not user.RegisterHotKey(None, ident, 0x4003, key):  # Ctrl+Alt+NOREPEAT
                raise RuntimeError("ショートカットが使用中です。他の起動済みプロセスを確認してください")
            registered.append(ident)
        print("起動: Ctrl+Alt+K = カーソル直前のひらがな / Ctrl+Alt+J = 選択範囲 / この端末でCtrl+C = 終了", flush=True)
        message = W.MSG()
        while True:
            while user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                if message.message == 0x0312 and pending is None:
                    try:
                        captured = desktop.capture("line" if message.wParam == 1 else "selection",
                                                   config.max_chars)
                        convert = converter.convert if captured.region is not None else converter.convert_selection
                        pending = (pool.submit(convert, captured.source,
                                               captured.left_context, captured.right_context), captured)
                        print("変換中（操作すると差し替えを中止します）…", flush=True)
                    except Exception as exc:
                        print(f"中止: {exc}", flush=True)
            if pending is not None and pending[0].done():
                future, captured = pending
                pending = None
                try:
                    changed = apply_result(desktop, captured, future.result())
                    print("貼り付け処理を実行しました" if changed else "変換の必要はありません", flush=True)
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
