# ビルド環境なしで使う Windows 配布版

配布ZIPにはアプリとビルド済みのTSF監視DLL・TSF編集DLLを含めます。Python本体と依存パッケージは同梱せず、利用先の64ビットPython 3.11以上と、インストール済みの`llama-cpp-python`・`comtypes`を使います。利用先にC++コンパイラーやCMakeは不要です。Windows x64向けです。ARM64ネイティブ版は作成しません。

## 利用するPC

1. ZIPをすべて展開します。ZIP内から直接起動しないでください。
2. `models`にGGUFモデルを置き、`config.toml`の`model_path`と`model_format`を合わせます。モデル同梱版なら設定済みです。
3. IME確定後の自動変換を使う場合、`config.toml`の`auto_replace_on_ime_commit`を`true`、`ime_commit_backend`を`"tsf"`にします。設定例にはTSF方式を指定済みです。
4. `start.cmd`をダブルクリックします。終了は開いた端末でCtrl+Cです。

公開ReleaseのZIPはモデルを含みません。既定のモデルは展開先で次のように取得できます。

```cmd
curl.exe -L --fail -o models\jinen-v2-xsmall-Q5_K_M.gguf https://huggingface.co/togatogah/jinen-v2-xsmall.gguf/resolve/b91eac974998a37423ca8a1198fd7c5631b06e57/jinen-v2-xsmall-Q5_K_M.gguf
certutil -hashfile models\jinen-v2-xsmall-Q5_K_M.gguf SHA256
```

確認済みSHA256は`24ff3af5db712fbbb4aa9254ee28ec4d731207134471ab68b06c1828726284c2`です。既定の設定例はこのモデル用です。

`start.cmd`は`KANA_REWRITER_PYTHON`で指定した実行ファイル、展開先の`.venv\Scripts\python.exe`、`py -3`の順にPythonを選びます。既存の仮想環境を使う場合はcmdで次を実行します。

```cmd
set "KANA_REWRITER_PYTHON=C:\path\to\.venv\Scripts\python.exe"
start.cmd
```

依存パッケージがまだない場合は、使用するPythonで以下を実行します。`--only-binary=:all:`により、対応するビルド済みパッケージがない場合はコンパイルせずエラーにします。

```cmd
python -m pip install --only-binary=:all: "llama-cpp-python>=0.3,<0.4" "comtypes>=1.4,<2" --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
```

追加の引数も渡せます。

```cmd
start.cmd --auto-replace-on-ime-commit
start.cmd --text "はいしゃ" --left-context "歯が痛いので、"
```

設定ファイルには各項目の説明があります。配布版の既定はPythonの編集ワーカーです。比較用C++編集ワーカーは同梱しません。GPU推論のランタイムも同梱しません。実際のIME操作はアプリごとに動作確認してください。

## 配布物を作るPC

この操作だけはC++ビルド環境と、ローカル推論が動く64ビットCPython環境が必要です。まずTSF DLLをビルドし、通常のPython版が動くことを確認します。

```powershell
.\scripts\build_tsf_probe.ps1
.\scripts\build_tsf_edit_probe.ps1
.\.venv\Scripts\python.exe scripts/package_windows.py
```

出力は`dist/kana-rewriter-windows-x64.zip`です。Pythonソース、設定例、起動ファイル、ビルド済みTSF DLLをコピーします。Python環境自体のコピーは行いません。

モデルなしが既定です。再配布条件を確認したモデルを同梱する場合:

```powershell
.\.venv\Scripts\python.exe scripts/package_windows.py --model models/jinen-v2-xsmall-Q5_K_M.gguf
```

`jinen-v2-`で始まるモデル名は`model_format = "jinen_v2"`に設定します。他のモデルは手動で設定を確認してください。既存の個人用`config.toml`、ログ、APIキー、`.venv`そのものはコピーしません。

TSFソースとDLLのビルド時のハッシュが一致しない場合は作成を中止するので、ソース変更後はDLLを再ビルドしてください。生成前に作成用Pythonから同梱TSF DLLを読み込み、アプリの`--help`を確認します。`bundle-manifest.json`にPythonの必要バージョン、各ファイルのSHA256を記録します。別PCでは展開後のモデル推論とIME確定・置換を確認してください。ZIPは自動で公開しません。
