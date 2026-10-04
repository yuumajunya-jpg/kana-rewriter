# C++編集ワーカー

推論のPython実装を維持し、入力欄の取得、Win32/UIA編集、入力監視、IME制御、反映確認をC++の別プロセスへ移す。移行用ブランチは`codex/native-editor-worker`。既存のPython編集ワーカーは比較・切り戻し用に残す。

現在は明示的に選択する実験用バックエンド。非表示Edit/RichEdit、実COMによるUIA取得・位置復元、IPCの失敗処理は検証済み。実ブラウザーのSendInput、IME切り替え、イベント通知を含む変換全体の性能・互換性は未検証なので、既定値は`python`を維持する。

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

## 検証と性能比較

```powershell
.\build\native\kana-editor-worker.exe --self-test
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
.\.venv\Scripts\python.exe -B scripts\benchmark_native_reads.py --output build/native/read-benchmark.json
```

自己テストは専用の非表示Edit/RichEditでUTF-16位置、改行・空行、置換、Undo、古い状態の拒否、UIA取得・カーソル位置、読み取り専用判定を確認する。実アプリへのキー送信やフォーカス変更はしない。Pythonテストは実ワーカーの初期化・終了と、不正応答、適用タイムアウト、大きなパイプ書き込みのタイムアウト、トークン再利用禁止を確認する。

非表示RichEditを別スレッドから更新し、`event`設定の反映待ちが最終的に全文・選択位置を確認することも検証する。イベント未対応時のポーリング代替も含むため、このテストだけではイベント通知による短縮量を示さない。

取得比較は同じ非表示RichEditの同じHWNDでPython/C++をABBA順、各40回測る。両者ともフォーカス照会を試験用に省く。フィクスチャの5msポーリングとWindowsのスケジューリングを含むため、ブラウザーの1変換時間や短縮量を示す測定ではない。起動・初期化・IPC・文字送信は含めない。

2026-10-04、他のテストを同時実行しない最終比較:

| 取得経路 | Python中央値 / p95 | C++中央値 / p95 |
| --- | ---: | ---: |
| Win32 | 66.486 / 67.941ms | 66.450 / 67.473ms |
| UIA | 232.996 / 248.406ms | 199.044 / 216.213ms |

UIAの中央値は約14.6%短縮。事前の照会削減を入れない移植では232.628ms対229.699msで、大きな差はなかった。最終比較は言語移植とUIA照会削減を合わせた差であり、C++の計算速度だけの効果ではない。別実行の初期比較と最終比較から個々の寄与を厳密には分離できない。

実ブラウザーでは試験専用入力欄で同じ本文・選択位置を復元し、固定の変換結果を渡してAIを測定から除外する。Python、C++/poll、C++/eventの取得・選択・SendInput・反映待ち・カーソル復元・IME復元・全体について、中央値・p95・成功率を比較する。通知準備・解除も全体に含める。推論中の入力・フォーカス変更、絵文字、空行、末尾記号、Undo、IMEオン/オフも検証する。

実ブラウザーで改善と互換性を確認するまではネイティブ版を既定にしない。

## 追加最適化（2026-10-04）

ブラウザメモ帳での利用者の5回のログでは、AI以外の平均は157.7ms。入力反映確認49.1〜62.9ms、カーソル復元10.1〜41.7ms、キー解放待ち0〜53.2msだった。IPCの追加時間は約0.5msで、言語移植より入力経路と待ち方の改善を優先する。

- UIAの反映待ちで、直前に取得したDocumentRange・全文を最終確認の開始状態に再利用する。最後の全文再取得とフォーカス確認は維持する。成功時の全文取得は3回から2回になる。
- 選択取得時に確認済みの「選択範囲か、カーソルか」を再利用し、同じ範囲のCompareEndpointsを重ねない。同一要素のメタデータ再取得と、不要なTreeWalker取得も省く。
- ポーリングの2/5/10ms、キー解放の5ms、カーソル復元の10msは設定値を変えず、スレッド専用の高精度待機タイマーで待つ。通知モードでは通知とタイマーを同時に待つ。タイマー未対応・失敗時は従来の待機に戻る。システム全体のタイマー精度は変更しない。[MicrosoftのAPI仕様](https://learn.microsoft.com/en-us/windows/win32/api/synchapi/nf-synchapi-createwaitabletimerexw)で高精度タイマーはWindows 10 version 1803以降に対応する。
- ChromiumのEdit入力欄で、全文と変換結果が単純な日本語・ASCIIであり、進行方向属性が左から右、未編集の末尾が16文字以内の単純な句読点の場合、文字入力と必要な右移動を同じSendInputにまとめる。最終全文・カーソル位置を確認し、後続のUIAカーソル復元を省く。絵文字、結合文字、方向属性不明、Monaco等は従来の経路を使う。診断の`C++/uia_caret_batch`がこの経路を使った目印。不一致時は編集も移動も再送しない。

同一環境で各30回の短時間待機の平均実測（ブラウザ変換全体の実測ではない）:

| 指定時間 | Sleep | 高精度タイマー |
| --- | ---: | ---: |
| 2ms | 13.108ms | 2.649ms |
| 5ms | 16.773ms | 5.442ms |
| 10ms | 16.723ms | 10.486ms |

非表示RichEdit取得のPython/C++ ABBA比較を各20回実施したところ、UIA中央値は232.753/174.375ms、p95は249.654/183.892msだった。前回と標本数・実行時点が異なるため、前回からの改善幅を厳密には示さない。今回も実ブラウザの一変換時間や、句読点移動の互換性の検証とは別の測定である。

実行中のexeはWindowsが上書きを拒否する。別名でビルドして切り替える場合:

```powershell
.\scripts\build_native.ps1 -Compiler .\build\toolchain\llvm-mingw-20260922-ucrt-x86_64\bin\x86_64-w64-mingw32-clang++.exe -OutputName kana-editor-worker-optimized.exe
```

```toml
native_worker_path = "build/native/kana-editor-worker-optimized.exe"
```

ワーカーは起動時に選ばれるため、設定変更後は変換ツールを終了して再起動する。別名のexeを回帰テストや取得比較に使う場合:

```powershell
$env:KANA_NATIVE_TEST_EXE = "$PWD/build/native/kana-editor-worker-optimized.exe"
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
.\build\native\kana-editor-worker-optimized.exe --benchmark-wait
.\.venv\Scripts\python.exe -B scripts\benchmark_native_reads.py --native-exe build/native/kana-editor-worker-optimized.exe --samples 20
```

ネイティブ自己テストは句読点移動の上限・順序と、絵文字・結合文字・双方向文字・改行移動の除外、高精度タイマーの待機と通知も検証する。実サイトでの変換速度とUndoの確認は引き続き必要。
