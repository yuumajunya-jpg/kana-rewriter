# C++編集ワーカー

> 現在の方針: 言語移行はいったん終了し、通常利用は最適化したPython編集ワーカーとする。C++版は任意の比較用実装として残す。Webメモでの手動置換ログは確認済みだが、全アプリ・IME・ブラウザーバージョンの互換性は未検証。以下には実装・調査時点の記録を含む。

推論のPython実装を維持し、入力欄の取得、Win32/UIA編集、入力監視、IME制御、反映確認をC++の別プロセスへ移す。移行用ブランチは`codex/native-editor-worker`。既存のPython編集ワーカーは比較・切り戻し用に残す。

現在は明示的に選択する実験用バックエンド。非表示Edit/RichEdit、実COMによるUIA取得・位置復元、IPCの失敗処理は検証済み。WebメモでのSendInput・IME制御を含む手動置換ログを確認した。全アプリに対する互換性は未検証。既定値は最適化した`python`を維持する。

## ビルドと起動

Visual StudioのC++ツール・Windows SDK・CMakeがあるDeveloper PowerShellで:

```powershell
.\scripts\build_native.ps1
```

またはllvm-mingwのポータブルツールチェーンで:

```powershell
.\scripts\build_native.ps1 -Compiler C:\tools\llvm-mingw\bin\x86_64-w64-mingw32-clang++.exe
```

この環境では[llvm-mingw 20260922](https://github.com/mstorsjo/llvm-mingw/releases/tag/20260922)の`ucrt-x86_64`版を`build/toolchain/`へ展開した。OSへのインストールやPATH変更はない。ツールチェーンと生成バイナリーはGit管理外。再ビルドには次の指定を使える。

```powershell
.\scripts\build_native.ps1 -Compiler .\build\toolchain\llvm-mingw-20260922-ucrt-x86_64\bin\x86_64-w64-mingw32-clang++.exe
```

生成先は`build/native/kana-editor-worker.exe`。ビルド後は`--self-test`を自動実行する（`-SkipTests`で省略可能）。VS/MSVCの経路はこの環境では未検証。

`config.toml`へ以下を追加して起動する。

```toml
editor_worker = "native"
native_worker_path = "build/native/kana-editor-worker.exe"
native_uia_wait = "poll"
```

```powershell
.\.venv\Scripts\python.exe -m kana_rewriter --config config.toml --timings
```

`native_worker_path`の相対パスは設定ファイルから解決する。未指定時はパッケージ所在から`build/native/kana-editor-worker.exe`を探す。wheelへのバイナリー同梱は未対応。

切り戻しは`editor_worker = "python"`に変更して再起動する。モデル・プロンプト・推論設定は変更不要。`--text`は推論だけを実行し、C++ワーカーを起動しない。`clipboard`とC++版の組み合わせは設定エラーにする。

## 設計

| ファイル | 担当 |
| --- | --- |
| `native/worker.cpp` | 取得・適用トランザクション、IPC、非表示フィクスチャ |
| `native/platform.hpp` | Win32編集、入力フック専用スレッド、IME制御 |
| `native/uia.hpp` | ネイティブCOM、属性一括取得、範囲構築、反映待ち |
| `native/protocol.hpp` | フレーム制限、Unicode位置変換、工程時間 |
| `kana_rewriter/native.py` | ワーカー起動、期限付きIPC、既存Captureとの接続 |

ホットキー登録、かな対象の抽出、推論はPythonに残る。C++は全文・選択のスナップショットと一回限りの編集トークンを保持し、Pythonからの置換計画を適用する直前に状態を再確認する。

IPCは`KRN1`、32-bit little-endianフレーム長、64-bit little-endian整数、長さ付きUTF-8文字列。16MiBのフレーム制限と本文上限を設ける。stdoutはIPC専用。工程時間は応答に付け、既存の遅延ログ出力へ流す。`--timings`では本文を出さない。

パイプの書き込みと読み取りは専用スレッドで行い、書き込みが詰まった場合も親の期限で停止する。破損応答・応答不明・タイムアウトではワーカーを停止して編集を再送しない。Python版への自動切り替えもしない。アプリの再起動が必要。

UIAはウィンドウを持たないMTAスレッドで使い、入力フックは別スレッドのメッセージループで動かす。`native_uia_wait = "event"`はTextChanged通知で再確認を起こす比較用モード。通知が未対応ならポーリングを続ける。通知を成功の証拠にせず、期待全文・選択位置・フォーカスを最終確認する。

通常の`GetText`では事前の`CompareEndpoints`を省く。null BSTRの場合だけ端点を照会し、空範囲なら空文字列、非空範囲なら更新中として再確認する。本文と選択範囲の整合・読み取り前後の全文一致は維持する。これは言語移植とは別のUIA呼び出し削減で、性能比較では区別する。

Pythonのコードポイント位置、WindowsのUTF-16位置、UIAのCharacter単位を区別する。近傍の範囲構築が一致しなければ本文からの構築と末尾側からの構築を試し、前後の本文と対象範囲が一致しなければ文字を送信しない。以前取り消したカーソル復元のバックオフ変更は再導入しない。

## 検証

`build/native/kana-editor-worker.exe --self-test` と `python -m unittest tests.test_native` で検証します。単発の旧版比較スクリプトは削除済みです。通常起動の `--benchmark-source` / `--benchmark-result` でAIを使わず編集経路を確認できます。
