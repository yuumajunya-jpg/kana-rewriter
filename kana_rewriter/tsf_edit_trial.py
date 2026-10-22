"""Explicit feasibility trial; no UIA selection, SendInput, or fallback writes."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import logging
import sys
import time

from .auto_ime import AutoImeMonitor
from .core import Config, Converter
from .diagnostic_log import DiagnosticLog
from .tsf_edit import (EDIT_PROTOCOL_VERSION, NativeEditProbe, RestartRequired,
                       apply_commit, capture_commit_context, edit_probe_path, normalize_newlines)
from .timing import flush_logs

logger = logging.getLogger(__name__)



def run(args):
    from .winapi import MessageWaiter, user, W
    import ctypes as C
    config = Config.load(args.config)
    if config.edit_backend == 'clipboard':
        raise ValueError('TSF実験は直接編集用の設定を使用してください')
    path = edit_probe_path()  # fail before loading AI or attaching
    logger.debug('TSF編集実験DLL=%s / protocol=%s', path, EDIT_PROTOCOL_VERSION)
    if (args.fixed_source is None) != (args.fixed_result is None):
        raise ValueError('--fixed-sourceと--fixed-resultは両方指定してください')
    if args.fixed_source is not None and not args.fixed_source:
        raise ValueError('--fixed-sourceは空にできません')
    if args.fixed_source is None:
        converter = Converter(config)
        if config.backend == 'llama_cpp':
            print('GGUFモデルを読み込んでいます…', flush=True)
            converter.load_model()
    else:
        from .__main__ import FixedConverter
        converter = FixedConverter(args.fixed_source, args.fixed_result)
    waiter = MessageWaiter()
    current = {}
    def factory(tid, ignored_path=None):
        probe = NativeEditProbe(tid, path)
        current['probe'] = probe
        return probe
    monitor = AutoImeMonitor(config, waiter.wake, probe_factory=factory)
    pool = ThreadPoolExecutor(max_workers=1)
    pending = None
    try:
        monitor.start()
        print('TSF直接編集実験: Enter確定後の対象を直接置換 / 終了はこの端末でCtrl+C', flush=True)
        print('通常版の変換プログラムは同時に起動しないでください。', flush=True)
        print('本文取得と置換はTSFを使用。UIA選択・キー送信・IME切り替え・フォールバックは行いません。', flush=True)
        while True:
            message = W.MSG()
            while user.PeekMessageW(C.byref(message), None, 0, 0, 1):
                user.TranslateMessage(C.byref(message)); user.DispatchMessageW(C.byref(message))
            if pending is None:
                commit = monitor.take()
                if commit is not None and monitor.begin(commit):
                    try:
                        probe = current['probe']
                        event = dict(probe.last_candidate or {})
                        if event.get('tick') != commit.tick or normalize_newlines(event.get('text', '')) != commit.text:
                            raise RuntimeError('確定通知の識別が変わったため中止しました')
                        captured = capture_commit_context(probe, event, config.context_chars,
                                                          config.model_format not in {'jinen_v1', 'jinen_v2'})
                        if not monitor.valid(commit):
                            raise RuntimeError('本文取得中に入力先または入力状態が変わりました')
                        future = pool.submit(converter.convert_selection, captured.source,
                                             captured.left_context, captured.right_context)
                        future.add_done_callback(waiter.wake)
                        pending = (future, commit, captured, probe, event)
                        logger.debug('TSF直接編集予定: commit=%s revision=%s / source=%r',
                                     event['commit_id'], event['revision'], captured.source)
                    except RestartRequired:
                        raise
                    except Exception as exc:
                        monitor.finish()
                        print('中止: ' + str(exc), flush=True)
            if pending is not None and pending[0].done():
                future, commit, captured, probe, event = pending
                pending = None
                try:
                    result = future.result()
                    if not monitor.valid(commit):
                        raise RuntimeError('推論中に入力先または入力状態が変わりました')
                    reply = apply_commit(probe, event, captured, result, target=commit.focus.target)
                    print('TSF内の本文・カーソル確認完了（画面側は内部ログで検証）' if reply else '変換の必要はありません', flush=True)
                except RestartRequired:
                    raise
                except Exception as exc:
                    logger.debug('TSF実験中止', exc_info=True)
                    print('中止: ' + str(exc), flush=True)
                finally:
                    monitor.finish()
            flush_logs()
            waiter.wait()
    except KeyboardInterrupt:
        print('終了します', flush=True)
    finally:
        try:
            monitor.close()
        finally:
            pool.shutdown(wait=True, cancel_futures=True)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='TSF範囲直接編集の実験専用起動')
    parser.add_argument('--config', default='config.toml')
    parser.add_argument('--diagnostic-log', nargs='?', const='', default='')
    parser.add_argument('--fixed-source')
    parser.add_argument('--fixed-result')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG, format='[診断] %(message)s')
    logging.getLogger('comtypes').setLevel(logging.WARNING)
    with DiagnosticLog(args.diagnostic_log):
        try:
            return run(args)
        except Exception as exc:
            logger.exception('TSF直接編集実験を停止しました')
            print('エラー: ' + str(exc), file=sys.stderr)
            return 1


if __name__ == '__main__':
    raise SystemExit(main())
