# 開発用診断ツール

通常起動には不要です。リポジトリのルートから実行します。

- `python scripts/diagnostics/diagnose_obsidian.py --config config.toml`: Obsidian内部の選択監視とアプリの診断ログを取得します。Obsidian公式CLIが必要です。
- `trace_obsidian_selection.js`: 上のランチャーから読み込むDOM・CodeMirror観測処理です。本文は編集しません。
- `python scripts/diagnostics/stress_obsidian_uia.py --help`: 専用テスト保管庫の`repro.md`を上書きしてUIA選択を繰り返します。`websocket-client`が必要です。実IMEを含む試験ではありません。

通常の診断は`python -m kana_rewriter --config config.toml --diagnostic-log`、AIなしの再現は`--benchmark-source`と`--benchmark-result`を使用してください。
調査の経緯は[過去の記録](../../docs/history/README.md)にあります。
