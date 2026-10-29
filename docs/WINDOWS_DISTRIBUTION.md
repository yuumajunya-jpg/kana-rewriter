# kana-rewriter Windows配布版（ZIP）の使い方

このZIPは、C++のビルド環境がないWindows PCで使うための配布版です。アプリ本体（Pythonソース）と、ビルド済みのTSF監視DLL・TSF編集DLLを同梱しています。Python本体と依存パッケージは含みません。

モデル同梱版では、**zenz-v3.2-xsmall**と、その出典・ライセンスを記載した`MODEL_LICENSES.md`が入っています。`config.toml`も設定済みなので、**手順4を飛ばして手順5以降へ進んでください**。zenzの直接読み込みには`kv_overrides`に対応した`llama-cpp-python`が必要です（0.3.36で確認）。

対象は **Windows 10/11 の64ビット版** です。ARM64ネイティブ版はありません。

## 1. ZIPの中身

```
kana-rewriter\
  start.cmd                 起動用
  launch.py                 Pythonの入口
  config.toml               設定ファイル（最初から入っています）
  README.md                 このファイル
  MODEL_LICENSES.md         同梱モデルの出典とライセンス（モデル同梱版のみ）
  bundle-manifest.json      同梱ファイルのSHA256一覧
  kana_rewriter\            アプリ本体
  build\tsf-probe\          監視用DLL
  build\tsf-edit-probe\     編集用DLL
  models\                   GGUFモデル（同梱版では設定済みのモデルが入っています）
```

同梱しないもの: Python本体、`llama-cpp-python`、`comtypes`、GPU推論用ランタイム。モデル同梱版以外はGGUFモデルも含みません。

## 2. 必要なもの

- Windows 10/11（64ビット）
- 64ビットのPython 3.11以上
- GGUFモデル（モデル同梱版には入っています。それ以外は手順4で取得します）

## 3. セットアップ手順

### 手順1: 展開する

ZIPを任意のフォルダーへ**すべて展開**します。ZIPの中から直接`start.cmd`を実行しないでください。

展開先は、あとから動かさない場所にしてください。設定の相対パスは`config.toml`の位置を基準に解決されます。

### 手順2: Pythonを確認する

コマンドプロンプトを開き、展開先のフォルダーへ移動してから確認します。

```cmd
py -3 -c "import struct, sys; print(sys.version); print('64-bit' if struct.calcsize('P') == 8 else '32-bit')"
```

`3.11`以上かつ`64-bit`と表示されれば進めます。`32-bit`や「Pythonが見つかりません」と出る場合は、64ビット版Python 3.11以上を入れるか、手順6で`KANA_REWRITER_PYTHON`を指定します。

`py`が使えない環境では、`py -3`を`python`に読み替えてください。

### 手順3: 依存パッケージを入れる

使用するPythonに、ビルド済みパッケージだけを入れます。`--only-binary=:all:`により、対応するビルド済みパッケージがない場合はコンパイルせずエラーになります。

```cmd
py -3 -m pip install --only-binary=:all: "llama-cpp-python>=0.3,<0.4" "comtypes>=1.4,<2" --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
```

入ったことを確認します。

```cmd
py -3 -c "import llama_cpp, comtypes; print('依存パッケージOK')"
```

`llama_cpp`の読み込みでエラーになる場合は、64ビットPythonかどうかと、上のコマンドが最後まで成功したかを確認してください。

### 手順4: モデルを置く

**モデル同梱版ではこの手順は不要です。** `models\zenz-v3.2-xsmall`にモデルが入り、`config.toml`も`model_format = "zenz_v3_2"`で設定済みです。手順5の確認だけ行ってください。

モデルなし版では、`models`フォルダーへGGUFを置きます。**既定は jinen-v2-xsmall** です。取得するファイル名・固定リビジョン・SHA256は、リポジトリ直下のREADMEと同じです。ただしZIPには`huggingface_hub`を同梱していないため、ここではWindows標準の`curl.exe`を使います。

```cmd
curl.exe -L --fail -o models\jinen-v2-xsmall-Q5_K_M.gguf https://huggingface.co/togatogah/jinen-v2-xsmall.gguf/resolve/b91eac974998a37423ca8a1198fd7c5631b06e57/jinen-v2-xsmall-Q5_K_M.gguf
certutil -hashfile models\jinen-v2-xsmall-Q5_K_M.gguf SHA256
```

SHA256が次と一致することを確認します。

```
24ff3af5db712fbbb4aa9254ee28ec4d731207134471ab68b06c1828726284c2
```

`certutil`の表示は大文字・空白区切りですが、同じ値です。`huggingface_hub`を別途入れている場合は、READMEと同じ`hf.exe`のコマンドでも同じファイルを取得できます。

#### zenz-v3.2-xsmallを使う場合

左右の文脈を使う代替モデルです。**保存先のフォルダーとファイル名はREADMEと同じ**`models\zenz-v3.2-xsmall\ggml-model-Q5_K_M.gguf`にします。

```cmd
mkdir models\zenz-v3.2-xsmall 2>nul
curl.exe -L --fail -o models\zenz-v3.2-xsmall\ggml-model-Q5_K_M.gguf https://huggingface.co/Miwa-Keita/zenz-v3.2-xsmall-gguf/resolve/4f5423f0fad41a73b1242eb96fe5c12ae4fdca83/ggml-model-Q5_K_M.gguf
certutil -hashfile models\zenz-v3.2-xsmall\ggml-model-Q5_K_M.gguf SHA256
```

SHA256が次と一致することを確認します。

```
00c64b3d318045a708d0cad5434faccab10f5481a49e6362864551fd0995fa58
```

zenzのGGUFは、azooKeyのllama.cppフォーク固有のpre-tokenizer名を持ちます。アプリは`model_format = "zenz_v3_2"`のとき、読み込み時だけこの名前を`gpt-2`へ上書きします。GGUFファイルは変更せず、別サーバーも起動しません。

この上書きには`kv_overrides`に対応した`llama-cpp-python`が必要です（0.3.36で確認）。古い版ではモデル読込に失敗するので、手順3のコマンドで更新してください。

### 手順5: `config.toml`を設定する

最低限、`model_path`と`model_format`の2行をモデルに合わせます。`model_path`は`config.toml`からの相対パス、または絶対パスです。**モデル同梱版は設定済みなので、この手順は確認だけで構いません。**

zenz-v3.2-xsmall（同梱版）:

```toml
model_path = "models/zenz-v3.2-xsmall/ggml-model-Q5_K_M.gguf"
model_format = "zenz_v3_2"
```

jinen-v2-xsmall（モデルなし版の既定）:

```toml
model_path = "models/jinen-v2-xsmall-Q5_K_M.gguf"
model_format = "jinen_v2"
```

`model_format`を間違えると、読み込みは成功しても変換結果が崩れます。モデルと形式は必ず組み合わせてください。

| モデル | `model_format` |
|---|---|
| jinen v2（xsmall / small） | `jinen_v2` |
| jinen v1 | `jinen_v1` |
| zenz-v3.2（xsmall / small） | `zenz_v3_2` |
| 汎用チャットモデル | `chat` |

設定ファイルには他の項目の説明もコメントで入っています。最初は変更せず、変換結果を見ながら調整してください。

### 手順6: 起動する

`start.cmd`をダブルクリックします。コマンドプロンプトの画面が開き、常駐します。終了はその画面で`Ctrl+C`を押します。

Pythonの場所を自動で見つけられない場合は、使う実行ファイルを指定して起動します。

```cmd
set "KANA_REWRITER_PYTHON=C:\path\to\python.exe"
start.cmd
```

`start.cmd`は`KANA_REWRITER_PYTHON`、展開先の`.venv\Scripts\python.exe`、`py -3`の順にPythonを選びます。

### 手順7: 動作を確認する

アプリを止めた状態で、モデルと設定だけを確認できます。読みと文脈を渡すと、変換結果だけを表示して終了します。

```cmd
start.cmd --text "はいしゃ" --left-context "歯が痛いので、"
```

同梱版（zenz-v3.2-xsmall）なら`歯医者`と表示されます。jinenでも同じ読みなら通常は`歯医者`になります。モデル読込エラーになる場合は、手順4と5のパス・形式・SHA256を確認してください。

常駐起動後は、入力欄でひらがなを確定し、カーソルを読みの直後に置いて`Ctrl+Enter`を押すと、カーソル左の区切りまでを変換します。選択範囲は`Ctrl+Shift+Enter`です。日本語キーボードでは`半角/全角`と`Shift+半角/全角`も使えます（`config.toml`で変更）。

## 4. IME確定後の自動変換を使う

`Enter`でIME確定した直後に、確定した読みを自動で変換できます。`config.toml`を次の2行にします。

```toml
auto_replace_on_ime_commit = true
ime_commit_backend = "tsf"
```

- 同梱のTSF編集DLLを使うため、追加のビルドは不要です。
- `Ctrl+Enter`で確定した場合は置換しません（読みのまま確定します）。
- 自動変換は、確定した範囲の原文・前後の文脈・選択・入力先を書き込み直前に確認します。変化していれば中止します。
- `tsf_edit_path`や`tsf_monitor_path`を明示している場合は、同梱DLLのパスへ変更するか、その行をコメントアウトしてください。古いDLLを指していると動作しません。
- アプリごとにIMEの通知が異なります。自動変換が働かないアプリでは、手動の`Ctrl+Enter`を使ってください。

## 5. うまく動かないとき

| 症状 | 確認すること |
|---|---|
| 画面が一瞬で閉じる | `start.cmd`をコマンドプロンプトから実行し、表示されるエラーを確認する |
| `Pythonが見つかりません` | 手順2で64ビット3.11以上を確認し、必要なら`KANA_REWRITER_PYTHON`を指定する |
| `pip install`がコンパイルを始める／失敗する | 64ビットPythonか確認する。手順3の`--only-binary=:all:`と`--extra-index-url`を付けて実行する |
| `Failed to load model` | `model_path`が`config.toml`からの相対パスとして正しいか、`models`にファイルがあるか確認する |
| zenzで`unknown pre-tokenizer type`が出る | `llama-cpp-python`が古い。手順3のコマンドで更新する |
| 変換結果が崩れる | `model_format`がモデルと一致しているか確認する（手順5の表） |
| `Ctrl+Enter`で何も起きない | カーソル位置、読みがひらがな・全角カタカナか、IMEや他アプリとキーが競合していないかを確認する |
| 自動変換だけ働かない | 手順4の2行、`tsf_edit_path`の指定、対象アプリの対応を確認する。まず手動の`Ctrl+Enter`で変換できるか切り分ける |
| `0x8007051a`で置換に失敗する | アプリを終了して起動し直す。TSF側が編集を拒否しているため、`ime_commit_backend`は`tsf`のまま、古いDLLを指定していないか確認する |

ログは、`--diagnostic-log`を付けて起動すると`logs`フォルダーへ保存されます。

## 6. 更新するとき

1. アプリを`Ctrl+C`で終了します。
2. 新しいZIPを展開し、`config.toml`と`models`を除いて上書きします。
3. `config.toml`は自分の設定を残し、必要なら新しい項目だけ追記します。

差し替え用ZIPには`config.toml`を含めていません。既存の設定をそのまま使えます。

## 7. 配布物を作る場合（開発者向け）

この操作だけはC++ビルド環境と、ローカル推論が動く64ビットCPython環境が必要です。

```powershell
.\scripts\build_tsf_probe.ps1
.\scripts\build_tsf_edit_probe.ps1
.\.venv\Scripts\python.exe scripts/package_windows.py
```

出力は`dist/kana-rewriter-windows-x64.zip`です。Pythonソース、設定例（`config.toml`として同梱）、起動ファイル、ビルド済みTSF DLLをコピーします。Python環境自体はコピーしません。

モデルなしが既定です。再配布条件を確認したモデルを同梱する場合:

```powershell
.\.venv\Scripts\python.exe scripts/package_windows.py --model models/jinen-v2-xsmall-Q5_K_M.gguf
```

`jinen-v2-`で始まるモデル名は`model_format = "jinen_v2"`に設定します。それ以外のモデルは手動で確認してください。既存の個人用`config.toml`、ログ、APIキー、`.venv`はコピーしません。

TSFソースとDLLのビルド時のハッシュが一致しない場合は作成を中止するので、ソース変更後はDLLを再ビルドしてください。生成前に作成用Pythonから同梱TSF DLLを読み込み、アプリの`--help`を確認します。`bundle-manifest.json`にPythonの必要バージョンと各ファイルのSHA256を記録します。別PCでは、展開後のモデル推論とIME確定・置換を確認してください。ZIPは自動で公開しません。
