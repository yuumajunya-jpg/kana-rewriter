# kana-rewriter

Windowsの入力欄で、日本語のかなをローカルAIで漢字かな交じり文へ変換するツールです。
手動ショートカット、EnterによるIME確定後の自動変換、入力中の変換プレビューに対応します。
標準モデルは **zenz-v3.2-xsmall / Q5_K_M** です。左右の文脈を使い、対象以外の本文や句読点を保持します。

## セットアップ

Windows 10/11、64ビットPython 3.11以上が必要です。
ビルド環境のないPCでは、[Windows配布版の手順](docs/WINDOWS_DISTRIBUTION.md)を参照してください。

ソースから使う場合は、リポジトリのルートで次を実行します。

```cmd
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[local,desktop]" huggingface_hub
copy config.example.toml config.toml
.\.venv\Scripts\hf.exe download Miwa-Keita/zenz-v3.2-xsmall-gguf ggml-model-Q5_K_M.gguf --revision 4f5423f0fad41a73b1242eb96fe5c12ae4fdca83 --local-dir models/zenz-v3.2-xsmall
```

既存の`config.toml`は上書きせず、そのまま利用してください。
モデルのSHA256、jinenへの切り替えは[モデル資料](MODELS.md)を参照してください。
zenzは読み込み時にトークナイザーの設定名を上書きします。GGUFファイルは変更しません。
`llama-cpp-python 0.3.36`で動作確認済みです。構築にC++開発環境が必要になる場合があります。

プレビューと自動変換に使うDLLをPowerShellでビルドします。
既存のLLVM/MinGW環境、またはVisual StudioのC++開発環境とCMakeが必要です。

```powershell
.\scripts\build_tsf_edit_probe.ps1
.\scripts\build_preview.ps1
```

## 起動と操作

モデルだけの動作確認と、通常起動:

```cmd
.\.venv\Scripts\python.exe -m kana_rewriter --config config.toml --text "はいしゃ" --left-context "歯が痛いので、"
.\.venv\Scripts\python.exe -m kana_rewriter --config config.toml
```

| 操作 | 設定例のキー |
|---|---|
| カーソル左の区切りまで変換 | Ctrl+Enter |
| 選択範囲内のかなを変換 | Ctrl+Shift+Enter |
| 終了 | 起動端末でCtrl+C |

例: `さんぽ。│`にCtrl+Enter → `散歩。│`（│はカーソル）。
カーソル左の走査は、既定では漢字・カタカナ・「。」で止まります。
手動変換は標準Edit/RichEditをWin32 API、それ以外をUI Automation（UIA）で取得・部分置換します。
クリップボードは使いません。

日本語キーボードでは`hotkey_line = "半角全角"`、`hotkey_selection = "Shift+半角全角"`も指定できます。
半角全角キーはRaw Inputで実際の解放を確認してから適用します。
キーの競合は起動時にエラーになります。

### IME確定後の自動変換

`config.toml`で`auto_replace_on_ime_commit = true`にすると、Enterで確定した範囲だけをTSFで直接置換します。
既定は無効です。`--auto-replace-on-ime-commit` / `--no-auto-replace-on-ime-commit`で起動時だけ切り替えられます。

- 今回の確定文字列に漢字またはカタカナ（半角を含む）が1文字でもあれば、全体を変換済みとしてAIへ渡しません。`コーヒーをのむ`や`今日はいいてんき`はそのまま保持します。
- `こーひー`の長音、ひらがなの分離した濁点・半濁点は変換対象です。周囲の本文は除外判定に含めません。
- Ctrl+Enterによる確定では自動変換しません。この操作で変換を抑制する場合は、手動変換のキーを半角全角などへ変更してください。
- 推論中の入力・文書切り替えで中止します。失敗時の方式切り替えや文字の再送は行いません。

部分確定・マウス確定は動作保証の対象外です。書き込み結果が不明な場合は停止します。本文を確認して再起動してください。

### 入力中のプレビュー

通常起動で有効です。`--no-preview`または`preview_enabled = false`で無効化できます。
IME未確定入力の読みと変換結果を、角丸のない小窓に常に2段で表示します。ひらがなのまま変換不要な場合も、上下に同じ文字を表示します。
読みは推論を待たずに更新し、最初の候補ができるまでは読みだけを表示します。
自動変換が有効なときは、入力中の文字だけを変換・表示し、確定済み本文は前後の文脈として参照します。
最初の有効な入力位置を横方向の基準とし、縦方向の移動に追従します。画面端では作業領域内へ調整します。
候補更新中は直前の候補を維持し、確定・取消・入力先変更で非表示にします。
表示だけでは本文は変わりません。

TSFで未確定文字・位置を公開する64ビットアプリが対象です。
位置と前後の文脈をTSFの同じ読み取りセッションから取得します。
手動変換やIME確定後の自動変換を受け付けると、実行中のプレビュー推論を中断して本文の変換を優先します。
中断した途中結果は表示・適用せず、本文の変換が終わった後に新しい入力のプレビューを再開します。
中断はモデルの生成停止判定で行うため、実行中の入力処理やトークンの計算が終わるまで待つ場合があります。

## 設定と診断

全項目は[設定例](config.example.toml)を参照してください。
`--config`を省略すると設定ファイルを自動読込せず、プログラムの既定値を使います。
その場合の手動キーはCtrl+Alt+K / Ctrl+Alt+Jです。
zenzの左右文脈はそれぞれ`context_chars`文字まで（既定64）、jinenは左文脈だけを使います。

推論はローカルGGUFの直接読み込みに統一しました。旧設定の`backend = "llama_cpp"`は指定不要ですが、そのまま読み込めます。`backend = "http"`を指定した設定はエラーになります。

旧設定の`editor_worker = "python"`と`ime_commit_backend = "tsf"`は読み込み時に受け入れますが、指定は不要です。
C++編集、クリップボード編集、UIA自動変換は廃止しました。旧方式を明示した設定はエラーになります。

```cmd
python -m kana_rewriter --config config.toml --inspect
python -m kana_rewriter --config config.toml --debug
python -m kana_rewriter --config config.toml --timings
python -m kana_rewriter --config config.toml --diagnostic-log
```

`--inspect`は3秒後に入力欄の能力を確認します。本文を変更しません。
`--debug`と`--diagnostic-log`は対象の文章を含む診断を出力します。
AIなしの編集確認には`--benchmark-source "さんぽ" --benchmark-result "散歩"`を使えます。

## 開発・対応範囲

```powershell
python -m unittest discover -s tests -v
node --test tests/test_obsidian_trace.cjs
```

パスワード・読み取り専用欄は対象外です。独自エディターや管理者権限の入力欄など、対応しないアプリがあります。
合成アプリでの回帰試験と、実アプリ・実IMEでの互換性確認は別に行います。

[資料一覧](docs/README.md)から、[アーキテクチャ](docs/ARCHITECTURE.md)、[編集処理](docs/EDITOR_DETAILS.md)、
[TSF範囲検証](docs/TSF_RANGE_VALIDATION.md)、モデルライセンス、過去の調査記録を参照できます。
