"""Start both diagnostic logs together; never reproduce or edit a note."""
import argparse
from datetime import datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]


def evaluate(cli, code):
    result = subprocess.run([cli, "eval", "code=" + code], capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=20)
    marker = result.stdout.rfind("=> ")
    if result.returncode or marker < 0:
        raise RuntimeError((result.stdout + result.stderr).strip() or "Obsidian eval failed")
    return json.loads(result.stdout[marker + 3:].strip())


def main(argv=None):
    parser = argparse.ArgumentParser(description="Obsidian内部監視を確認して変換を起動")
    parser.add_argument("--config", default=str(ROOT / "config.toml"))
    parser.add_argument("--log-dir", type=Path, default=ROOT / "logs")
    args, extra = parser.parse_known_args(argv)
    cli = shutil.which("Obsidian.com")
    if not cli:
        parser.error("Obsidian公式CLI（Obsidian.com）が見つかりません")
    args.log_dir.mkdir(parents=True, exist_ok=True)
    prefix = args.log_dir.resolve() / datetime.now().strftime("obsidian-%Y%m%d-%H%M%S-%f")
    dom_path = str(prefix) + "-dom.jsonl"
    uia_path = str(prefix) + "-uia.log"
    session_path = str(prefix) + "-session.json"
    source = str(ROOT / "scripts" / "diagnostics" / "trace_obsidian_selection.js")
    code = ('(async()=>{if(window.__kanaSelectionTrace)await window.__kanaSelectionTrace.stop();'
            'window.__kanaSelectionTracePath=' + json.dumps(dom_path) + ';'
            'return JSON.stringify(eval(require("fs").readFileSync('
            + json.dumps(source) + ',"utf8")));})()')
    session = {"dom": dom_path, "uia": uia_path, "mode": "current"}
    try:
        start = evaluate(cli, code)
        session["start"] = start
        if start["stopped"] or start["error"] or Path(start["path"]) != Path(dom_path):
            raise RuntimeError("Obsidian内部監視の開始確認に失敗しました: " + str(start))
        print("Obsidian内部監視: 開始確認済み / renderer PID=" + str(start["rendererPid"]), flush=True)
        print("DOMログ: " + dom_path, flush=True)
        print("UIAログ: " + uia_path, flush=True)
        print("Obsidianを再起動・再読み込みせず、同じ箇条書き行で再現してください。", flush=True)
        try:
            module = 'kana_rewriter'
            run_args = list(extra)
            return subprocess.call([sys.executable, "-m", module, "--config", args.config,
                                    *run_args, "--diagnostic-log", uia_path], cwd=ROOT)
        except KeyboardInterrupt:
            return 130
        finally:
            try:
                end = evaluate(cli, '(async()=>JSON.stringify(window.__kanaSelectionTrace?'
                               'await window.__kanaSelectionTrace.stop():null))()')
                session["end"] = end
                session["valid"] = bool(end and end["rendererPid"] == start["rendererPid"]
                    and end["path"] == start["path"] and not end["error"] and not end["dropped"])
                if not session["valid"]:
                    print("内部監視が途切れています。この回のDOMログは完全ではありません。", file=sys.stderr)
                else:
                    print("Obsidian内部監視: 解除・保存完了（監視継続を確認）", flush=True)
            except Exception as exc:
                session["valid"] = False
                session["stop_error"] = str(exc)
                print("内部監視の終了確認に失敗: " + str(exc), file=sys.stderr)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        session["error"] = str(exc)
        print("診断起動失敗: " + str(exc), file=sys.stderr)
        return 1
    finally:
        Path(session_path).write_text(json.dumps(session, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
