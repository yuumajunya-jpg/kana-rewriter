# kana-rewriter

Windowsの入力欄で、確定済みのひらがな・全角カタカナをローカルAIで漢字かな交じり文に置き換えるツールです。左文脈を利用し、対象以外の本文や句読点を保持します。

例: `歯が痛いので、はいしゃ│に行く` → `歯が痛いので、歯医者│に行く`（│はカーソル）。

通常はPython単体で動作します。標準Edit/RichEditはWin32 API、それ以外はUI Automation（UIA）で取得・選択し、Unicode入力で部分置換します。既定の直接編集方式ではクリップボードを使いません。AIは`llama-cpp-python`からGGUFを直接読み込みます。

C++編集ワーカーは比較用の実験実装として残しています。Python版の最適化後、手動試行ではC++版との明確な速度差が確認できなかったため、通常利用はPython版としています。

## セットアップ

Windows 10/11、Python 3.11以上が必要です。以下はリポジトリのルートで実行するcmd用の手順です。

```cmd
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[local,desktop]" huggingface_hub
copy config.example.toml config.toml
```

既存の設定ファイルは上書きせず、そのまま利用してください。`llama-cpp-python`の構築にはVisual StudioのC++ツールが必要になる場合があります。[公式インストール手順](https://github.com/abetlen/llama-cpp-python#installation)を参照してください。

現在の利用・速度計測に使っているモデルは**jinen-v2-xsmall / Q5_K_M（約28.3MB）**です。以下の固定リビジョンから、利用者環境と同じGGUFを取得できます。

```cmd
.\.venv\Scripts\hf.exe download togatogah/jinen-v2-xsmall.gguf jinen-v2-xsmall-Q5_K_M.gguf --revision b91eac974998a37423ca8a1198fd7c5631b06e57 --local-dir models
certutil -hashfile models\jinen-v2-xsmall-Q5_K_M.gguf SHA256
```

確認済みSHA256: `24ff3af5db712fbbb4aa9254ee28ec4d731207134471ab68b06c1828726284c2`。[配布元の固定コミット](https://huggingface.co/togatogah/jinen-v2-xsmall.gguf/commit/b91eac974998a37423ca8a1198fd7c5631b06e57)とローカルファイルのハッシュが一致しています。`config.toml`は次の組み合わせで設定してください。更新前の設定がある場合はこの2項目を変更します。

```toml
model_path = "models/jinen-v2-xsmall-Q5_K_M.gguf"
model_format = "jinen_v2"
```

v2の入力はプロンプト全体をNFKC正規化します。`model_format`も必ずv2に合わせてください。モデルの比較・入力形式は[MODELS.md](MODELS.md)を参照してください。モデルファイルはこのリポジトリに含めません。

### jinen-v2-smallを使う場合

small版のQ5_K_M（約81.1MB）も同じPython環境で利用できます。[配布元](https://huggingface.co/togatogah/jinen-v2-small.gguf)から次のコマンドで取得します。

```cmd
.\.venv\Scripts\hf.exe download togatogah/jinen-v2-small.gguf jinen-v2-small-Q5_K_M.gguf --revision 3461d0573ab447985badde3174165b967d06076c --local-dir models
certutil -hashfile models\jinen-v2-small-Q5_K_M.gguf SHA256
```

ローカルに取得済みのファイルで確認したSHA256: `80482707513d6b67dafc31774371cf95d765542abf8d74eebf5f32f92d788bd3`。`config.toml`を次の組み合わせに変更して再起動します。

```toml
model_path = "models/jinen-v2-small-Q5_K_M.gguf"
model_format = "jinen_v2"
```

xsmallとsmallで入力形式は同じです。これまでの速度計測はxsmallで行っているため、smallの速度・精度は同じ結果とは限りません。

## 動作確認と起動

入力欄を操作せずに変換を確認します。以下は文脈による変換を確認するための入力例です。出力はモデル・設定により変わります。

```cmd
.\.venv\Scripts\python.exe -m kana_rewriter --config config.toml --text "はいしゃ" --left-context "歯が痛いので、"
.\.venv\Scripts\python.exe -m kana_rewriter --config config.toml --text "はいしゃ" --left-context "車が壊れたので、"
```

通常の起動:

```cmd
.\.venv\Scripts\python.exe -m kana_rewriter --config config.toml --editor-worker python
```

「起動」と表示されたら、入力欄にフォーカスを置き、IME入力を確定してからショートカットを押します。

| 操作 | `config.example.toml`のキー |
|---|---|
| カーソル左の区切りまで変換 | Ctrl+Enter |
| 選択範囲内のかなを変換 | Ctrl+Shift+Enter |
| 終了 | 起動端末でCtrl+C |

例: `さんぽ。│`にCtrl+Enter → `散歩。│`。句読点は保持し、文字数の変化を考慮してカーソルを戻します。

`--config`を省略すると`config.toml`は読み込まず、プログラム内の既定値を使います。その場合の変換キーはCtrl+Alt+K / Ctrl+Alt+Jです。キーは設定で変更できます。他アプリと競合すると起動を中止します。

日本語キーボードの半角／全角キーも指定できます。`config.toml`で次のように設定し、再起動してください。

```toml
hotkey_line = "半角全角"
hotkey_selection = "Shift+半角全角"
```

`半角/全角`、`半角／全角`、`HankakuZenkaku`、`ZenkakuHankaku`も同じキー名として扱います。単独でもCtrl・Alt・Shift・Winとの組み合わせでも指定できます。IME状態によって変わる2種類のキーコードを登録し、どちらも同じ変換操作へ割り当てます。登録競合時は起動を中止します。実際のキー入力とIMEの挙動は日本語キーボードの環境で確認してください。

半角全角は仮想キーの状態が解放後も残る場合があるため、物理キーの押下・解放を監視して適用前に確認します。`--debug` / `--timings`には`適用/半角全角キー解放待ち`を表示します。キーを押したままの場合は最大2秒で中止します。

## 設定・対応範囲

全項目は[config.example.toml](config.example.toml)を参照してください。`editor_worker`はPython/C++の切り替え、`edit_backend`は入力欄の編集方式、`backend`はAIへの接続方式です。`--editor-worker python`または`native`で、その起動だけ設定を上書きできます。

CPU推論が既定です。GPU対応ランタイムでは`n_gpu_layers = -1`で全層をGPUへ渡せます。`backend = "http"`ではローカルのAPIサーバーも利用できます。設定内のモデル・C++実行ファイルの相対パスは設定ファイルの所在が基準です。

- 標準Edit/RichEditは非表示入力欄で部分置換・改行・絵文字・カーソル復元・Undoを検証しています。
- UIAはTextPatternによる本文と位置の取得が必要です。Webメモでの手動試行では置換成功を確認しています。他のアプリの互換性は個別に確認してください。
- 読み取り専用・パスワード欄は対象外です。管理者権限のアプリや端末では利用できない場合があります。
- UIA入力ではIMEを一時オフにし、本文・位置の反映確認後に元の状態へ戻します。状態が変わるか確認に失敗した場合は中止し、文字を再送しません。
- 候補選択、読みの辞書検証、トレイUI、自動起動は未実装です。変換結果の誤りは起こりえます。

変換範囲・IME・中止条件・VS Code/Monacoの設定は[編集処理の詳細](docs/EDITOR_DETAILS.md)を参照してください。

## 診断・速度比較

入力欄の能力を確認するには、次を実行して3秒以内に対象へフォーカスを移します。モデルを読み込まず、文字を変更しません。

```cmd
.\.venv\Scripts\python.exe -m kana_rewriter --config config.toml --inspect
```

処理時間の計測:

```cmd
.\.venv\Scripts\python.exe -m kana_rewriter --config config.toml --editor-worker python --timings
```

`時間集計`の`AI以外`は、取得から適用完了までの合計からモデル変換呼び出し時間を引いた値です。内側の工程と通信時間は重複するため、全行を足さないでください。`--debug`では対象本文・文脈・モデル出力も表示します。

AIなしでPython版とC++版を比較する手順と実測値は[PYTHON_EDITOR_COMPARISON.md](PYTHON_EDITOR_COMPARISON.md)に記載しています。

## 開発・資料

```cmd
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
```

最終確認ではWindowsで186テスト成功。未ビルドのC++実行ファイルやWindowsに依存するテストは環境によってスキップされます。自動試験はモックを含み、全アプリや実モデルの精度を保証するものではありません。

資料一覧は[docs/README.md](docs/README.md)、初回GitHub公開の手順は[docs/GITHUB_PUBLISH.md](docs/GITHUB_PUBLISH.md)を参照してください。
