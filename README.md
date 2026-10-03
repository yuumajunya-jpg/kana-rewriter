# kana-rewriter

Windowsで入力確定済みの日本語をローカルAIで漢字かな交じり文に差し替えます。`llama-cpp-python`からGGUFを直接読み込む、通常のテキスト入力欄向けの初期実装です。

例: `歯が痛いので、はいしゃ│に行く`（│はカーソル） → `歯が痛いので、歯医者に行く`

## モデルとセットアップ

第一候補は**jinen-v1-xsmallのQ5_K_M版（31.2MB）**。左文脈を専用マーカーで渡す変換専用モデルです。現行mainにはGGUFが見当たらないため、公開時のリビジョンを指定します。詳細と代替候補は[MODELS.md](MODELS.md)に記載しました。

使用者のWindows環境で`--text`による推論が動作し、左文脈「歯が痛いので、」で「歯医者」、「車が壊れたので、」で「廃車」となることが確認されました。広い範囲での精度や各入力アプリの動作は未評価なので、最初に`--text`で確認してください。

Windows 10/11、Python 3.11以上を用意し、cmdで:

```cmd
cd C:\workfile\kana-rewriter
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[local]" huggingface_hub
.\.venv\Scripts\hf.exe download togatogah/jinen-v1-xsmall jinen-v1-xsmall-Q5_K_M.gguf --revision 6ec3b6ae261939271c6bd0b9385553ee0ae0a93c --local-dir models
copy config.example.toml config.toml
```

古いhuggingface_hubで`hf`がインストールされない場合は更新してください。ブラウザーで取得する場合は[公開コミット](https://huggingface.co/togatogah/jinen-v1-xsmall/commit/6ec3b6ae261939271c6bd0b9385553ee0ae0a93c)の「Browse files」からGGUFを選び、`models/jinen-v1-xsmall-Q5_K_M.gguf`として保存します。

公開コミットのSHA256は`bb3110f06e539bf8596756df85a48b3946f1378e6cb912322b9c368be06d79aa`です。`certutil -hashfile models\jinen-v1-xsmall-Q5_K_M.gguf SHA256`で確認できます。この固定リビジョンからも取得できない場合は、現行SafetensorsからのGGUF変換または配布モデルの再確認が必要です。

CPU実行が既定です。llama-cpp-pythonのビルドにはVisual StudioのC++環境が必要になる場合があります。[公式インストール手順](https://github.com/abetlen/llama-cpp-python#installation)を参照してください。GPU対応版をインストールした場合、`n_gpu_layers = -1`で全層をGPUに渡せます。

## 動作確認と起動

まずウィンドウを操作せずに確認します:

```powershell
.\.venv\Scripts\python.exe -m kana_rewriter --config config.toml --text "はいしゃ" --left-context "歯が痛いので、"
.\.venv\Scripts\python.exe -m kana_rewriter --config config.toml --text "はいしゃ" --left-context "車が壊れたので、"
```

使用者環境で、それぞれ「歯医者」「廃車」という結果が報告されています。モデルの更新や設定によって結果は変わる可能性があります。

ショートカット待ち受け:

```powershell
.\.venv\Scripts\python.exe -m kana_rewriter --config config.toml
```

モデル読み込み後に「起動」と表示されたら、メモ帳などで使えます。

| キー | 対象 |
| --- | --- |
| Ctrl + Alt + K | カーソル直前から左へ続くひらがな（長音符を含む） |
| Ctrl + Alt + J | 自分で選択したかなの読み。前後文脈は取得しない |
| 起動端末でCtrl + C | 終了。推論中は完了を待つ場合がある |

Kは未選択の状態で使います。漢字・句読点・空白・英数字・カタカナなどで走査を止めます。対象がなければ中止します。助詞の判定はないため、`私はきょう│`では`はきょう`が対象です。

同じ**表示行**の対象前後を文脈として取得します。既定のモデル入力は最大64文字の左文脈のみです。`zenz_v3_2`では左右両方を渡します。差し替え用の前後文字は取得した元の値を保存し、モデルに生成させません。

推論中は選択された状態で待ってください。入力やマウス操作、フォーカス・クリップボードの変更を検出すると適用を中止します。中止後に選択範囲が残った場合は解除するか、選び直して再実行してください。

## 入力形式と設定

専用モデルはチャットではなく`create_completion`を使います。ひらがなを全角カタカナにし、以下のマーカーを挿入します。表記は説明用で、実際はそのUnicode文字を送ります。

```text
jinen_v1:   \uEE02左文脈\uEE00カタカナの読み\uEE01
zenz_v3_2:  \uEE02左文脈\uEE07右文脈\uEE00カタカナの読み\uEE01
```

空文脈の扱いも形式に合わせます。余分なBOS/EOSを追加せず、temperature=0、反復ペナルティなしで生成します。EOS/`</s>`で停止し、打ち切り・空出力・制御マーカー・空白や改行を含む結果は拒否します。

`config.example.toml`が設定例です。`--config`を省略すると既定値を使用し、`config.toml`は自動読み込みしません。

- `model_format`: `jinen_v1`、`zenz_v3_2`、汎用モデル用の`chat`。
- `model_path`: 設定ファイルからの相対パス、または絶対パス。
- `n_ctx = 0`: GGUFの学習時コンテキスト長を使用。
- `context_chars`: 文脈の最大文字数。0で文脈を無効化。
- `max_chars`: 対象の文字数上限。`max_tokens`: 出力上限。

`backend = "http"`も使用できます。専用モデルは`/v1/completions`、chatは`/v1/chat/completions`を指定します。接続はローカルに限定し、プロキシとリダイレクトを無効化します。`timeout_seconds`はHTTP専用で、直接推論の時間制限はありません。

## 対応範囲と検証

- 取得はShift+Home/Shift+End/Ctrl+Cによるため、折り返しやSmart Homeなどアプリのキー挙動に依存します。別行の文脈は取得しません。
- Kは表示行を選択して取得し、元の前後文字＋変換結果で**表示行全体をUnicodeキー入力で差し替えます**。文字値は保持しますが、リッチテキストの書式は保持しません。
- Jは必ずかなを選択してください。選択なしのCtrl+Cで行全体をコピーする独自エディターの選択状態は、この方式では判別できません。
- 通常のテキスト入力欄が対象です。読み取り専用領域・パスワード欄・端末・独自入力コントロールは対象外です。Undoの単位はアプリ依存です。
- クリップボードはコピー前に実データを複製し、Win32 APIで復元します。文字・HTML・RTF・画像などの通常形式を扱います。OLE内部の追跡情報やアプリ独自の所有権付き形式を完全には保持しません。複製できない形式はコピー前に中止します。コピー履歴には対象文字が残る場合があります。管理者権限のアプリへの送信や他アプリの自動編集にも制限があります。
- 読みの逸脱や同音異義語の誤選択は起こりえます。辞書による読み検証、候補選択、トレイUI、自動起動は未実装です。

```powershell
py -3 -m unittest discover -s tests -v
```

切り出し、文脈保持、専用マーカー、API呼び出し、応答検証、中止処理を確認します。モデル呼び出し・ウィンドウ操作のモックを含み、実モデルの精度や実アプリでの動作を保証するものではありません。
