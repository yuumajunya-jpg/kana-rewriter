# 対象アプリ内でのTSF監視の調査

調査日: 2026-10-14。目的は、既存の日本語IMEを維持したまま、Windowsの複数アプリで確定を検知すること。自動置換機能の実装・復活は行っていない。

この資料は試作前の調査記録。後続の監視DLL・自動置換試験と実機結果は[TSF検証モード](TSF_PROBE.md)を参照。

## 判断

対象アプリの入力スレッド内でTSFの編集通知を観測する案には、Microsoftの公開資料と、実行したAPI検証による根拠がある。監視だけの試作へ進む価値がある。

ただし、Chrome・メモ帳・VS Codeへの監視DLL接続、実IMEによる確定・取消・部分確定の識別、既存置換処理への接続は未検証。全Windowsアプリへの対応や、確実な確定検知が実現できると結論づける段階ではない。

## 確認できたこと

1. Microsoftは、支援技術が対象アプリ内で`ITfThreadMgrEventSink`と`ITfTextEditSink`を登録し、`ITfContextComposition::EnumCompositions`で未確定範囲を読み取る手順とReadCompサンプルを公開している。2002年の資料なので、Windows 11でのアプリ互換性の証明とは区別する。[Microsoftの資料](https://learn.microsoft.com/en-us/previous-versions/windows/desktop/dnacc/input-method-editor-and-text-services-framework-accessibility)
2. `TF_GetThreadMgr`は呼び出しスレッドの既存マネージャーを取得する。Python側で新しくTSFオブジェクトを作るだけでは、別アプリの入力コンテキストは取得できない。[取得API](https://learn.microsoft.com/en-us/windows/win32/api/msctf/nf-msctf-tf_getthreadmgr)
3. `OnEndEdit`は読み書き編集セッションの終了通知であり、IME確定専用通知ではない。コールバック内の編集Cookieは読み取り専用で、その呼び出し内で読む。[編集通知](https://learn.microsoft.com/en-us/windows/win32/api/msctf/nf-msctf-itftexteditsink-onendedit)
4. `GUID_PROP_COMPOSING`は範囲が未確定かを表す。`ITfEditRecord::GetTextAndPropertyUpdates`で本文・指定属性の更新範囲を取得できる。[属性](https://learn.microsoft.com/en-us/windows/win32/tsf/predefined-properties)、[更新範囲](https://learn.microsoft.com/en-us/windows/win32/api/msctf/nf-msctf-itfeditrecord-gettextandpropertyupdates)
5. Chromiumは入力コンテキストを作成し、そこに自分の`ITfTextEditSink`を登録している。現行公開実装の`OnEndEdit`は`GUID_PROP_COMPOSING`等から状態を取得する。UIAの確定通知を抑制する経路とは別に、TSF内部に観測対象がある。[コンテキスト生成](https://github.com/chromium/chromium/blob/main/ui/base/ime/win/tsf_bridge.cc)、[TSFTextStore](https://chromium.googlesource.com/chromium/src/+/HEAD/ui/base/ime/win/tsf_text_store.cc)

## このPCで実行したAPI検証

64ビットC++コンソールプログラムをコンパイルし、検証プログラム自身のSTAスレッドで次を行った。

- TSFマネージャーを生成・Activateする。IME/TIPとしてのインストール・登録・選択はしていない。
- 空のDocumentMgrと、テキストストアを持たないContextを作成・Pushする。
- 同じContextに異なる2つの`ITfTextEditSink`を登録する。
- `ITfContextComposition`を取得し、`EnumCompositions`を呼ぶ。
- 2つの監視を解除し、ContextをPop、マネージャーをDeactivateする。

全呼び出しが`S_OK`（0x00000000）。IME/TIPとして登録していないプログラムでも、合成コンテキストへの監視登録・複数監視の併用・未確定範囲列挙APIの取得・解除が可能だった。

これは登録APIの検証である。空のコンテキストなので、実際の文字入力、`OnEndEdit`の受信、属性値の読み取り、確定・取消の識別は試験していない。別プロセスへのフックも設置していない。Chrome上で複数監視が共存できることを直接検証した結果ではない。

検証用ファイル（Git管理外の`build`内）:

- `build/tsf-research/api_probe.cpp`
- `build/tsf-research/api_probe.exe`
- `build/tsf-research/api_probe.log`

再実行例（PowerShell、既存のローカルツールチェーンを使用）:

```powershell
& '.\build\toolchain\llvm-mingw-20260922-ucrt-x86_64\bin\x86_64-w64-mingw32-clang++.exe' -std=c++17 -O2 -Wall -Wextra build/tsf-research/api_probe.cpp -o build/tsf-research/api_probe.exe -lole32 -luuid -luser32 -static
& '.\build\tsf-research\api_probe.exe'
```

## 監視だけの試作案

1. 対象を明示的に選択し、入力を処理するGUIスレッドを特定する。
2. 対象と同じビット数のC++ DLLを、対象スレッド限定のWindowsフックで動かす。フック方式と初期化タイミングは試作で検証する。DllMain内でCOM/TSF初期化やIPCを行わない。
3. 対象スレッド上で`TF_GetThreadMgr`を呼ぶ。存在しなければ「既存マネージャーなし」と記録する。新しいマネージャーを作って検知成功と扱わない。
4. `ITfThreadMgrEventSink`を登録し、既存のフォーカス中DocumentMgrとContextも取得する。すでに起動しているアプリへの後付け接続を試験する。
5. Contextの`ITfSource`へ`ITfTextEditSink`を登録する。アプリの`ITextStoreACPSink`を差し替えない。ChromiumのTextStoreには単一ACP sinkの制限があるが、今回使うContext側のTextEdit sinkとは別インターフェースである。
6. 編集通知で、未確定範囲一覧・COMPOSING属性の更新・その範囲に残る本文を読み、プロセスID、スレッドID、コンテキストの世代とともに記録する。CookieやCOMポインターをPython側へ送らない。
7. 終了時に対象スレッド上でUnadviseし、COM参照を解放したことを確認してからフックを解除する。コールバックが残っている状態でDLLをアンロードしない。

[フックのビット数・配置の仕様](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowshookexw)、[解除時の並行呼び出し](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-unhookwindowshookex)

DLLでは推論せず、読み取りと通知に限定する。入力スレッドをモデル処理や同期IPCで待たせない。観測不可能・解除不能等を診断結果として扱う。

## 確定判定で未解決の点

- 「未確定範囲が消えた」だけでは、確定・取消・フォーカス移動を区別できない。
- 消える前の範囲を追跡し、属性の変化と残った本文を照合する必要がある。取消で元の選択文字列が復元される場合も試験する。
- 文節単位の部分確定では、未確定範囲が残ったまま一部だけ確定する。未確定範囲の有無だけを見てはいけない。
- 1回の編集で旧範囲の確定と新しい未確定範囲の開始が起きる場合がある。単純な状態のON/OFFで扱わない。
- TSFの通知時点で、ChromeのレンダラーやUIA本文への反映が完了しているとは限らない。既存の編集処理へ接続する場合、通知された文字列が実際の本文に反映されたことを読み取りで確認する必要がある。時間が経過したことを確定の根拠にしない。
- Chromeは入力タイプ別のTSFコンテキストを再利用する構成なので、Contextの同一性をDOM入力欄の同一性として扱わない。TSF内の文字位置を、そのままUIA全文の位置と扱うことも避ける。
- TSF通知の観測が可能でも、現在のUIA/Win32編集方式が使えないアプリでは置換は別途未対応となる。確定検知の互換性と置換の互換性を分けて評価する。

## 次の試作の合格条件

ChromeのWebメモ、メモ帳、VS Codeの編集欄で、既存Microsoft IMEを使って確認する。最初は通常権限の64ビットアプリを対象とする。結果をアプリ・IME・バージョン別に記録する。

| 操作 | 必要な観測 |
|---|---|
| かな入力途中で数秒止める | 確定として扱わない |
| Enterで確定 | 終了した範囲と確定文字列を識別 |
| 候補を選択して確定 | 候補選択途中と確定を区別 |
| Escapeで取消 | 変換対象を生成しない |
| 選択文字列を置き換える入力を取消 | 復元された元文字列を新しい確定と誤認しない |
| 文節の部分確定 | 確定部分と継続中の部分を区別 |
| マウスで候補を確定 | Enterに依存せず識別 |
| フォーカスを別欄・別アプリへ移動 | 旧通知を新しい欄へ適用しない |
| 同じアプリ内で入力欄を切り替える | コンテキスト再利用による混同がない |
| 監視停止・対象終了 | 監視が解除され、入力が継続できる |

パスワード欄・読み取り専用欄は対象外。TSFを持たない入力欄、管理者権限・保護されたプロセス・アプリコンテナー、32ビット/ARM64等は後続の互換性調査とする。これらを64ビット通常アプリでの成功から推定しない。

まずこの観測試作で確定の情報が取れるかを判断し、成功した場合に限って既存のPython変換・本文再検証へ接続する。
