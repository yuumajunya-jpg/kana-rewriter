# クリップボードを使わない版の検討

調査日: 2026-10-03。以下は調査時の設計資料。`feature/no-clipboard`で併用方式を実装した。使用方法と現在の対応範囲はREADMEを参照。

実装は`edit_backend = "auto"`を既定とし、標準Edit/RichEditではWin32、それ以外ではUIAの取得・SelectとSendInputを使う。UIA/Win32の編集ワーカーは別プロセスに分離し、タイムアウト時に編集を再送しない。`--inspect`でモデルを読み込まずに入力欄の能力を確認できる。旧方式は`clipboard`の明示指定で利用できる。

追加検証では別プロセスの非表示Edit/RichEditで改行・絵文字を含む部分置換、カーソル復元、Undoを確認した。実際のCOMバインディングとRichEditのTextRange取得・選択も確認した。UIAのSendInputはモックで順序と中止を確認しており、実ブラウザーでの一連の編集は未検証。

実アプリ試験では、Unicode入力がIME有効時に文字順の崩れと未確定文字の残存を生み、手動でIMEをオフにすると解消するとの報告を受けた。利用者の選択により、UIA入力ではIMEの元状態を保存し、入力直前に一時オフ、本文・カーソルの反映確認後に復元する方式へ変更した。以下の調査時の「IMEを切り替えない」方針は、この変更以前の記述である。現在の動作と制約はREADMEを参照。IME切り替え失敗時の送信中止、元がオフなら維持、例外時の復元、反映前に復元しない順序をモックで検証した。APIによる実ブラウザーのIME切り替えは未検証。

## 結論

UI Automation（UIA）で本文と対象範囲を取得し、TextRange.Selectで選択してSendInputで置換する方式は実現可能な候補。TextPatternに書き込みAPIがないことと、UIAを使った部分置換が不可能であることを混同しない。ブラウザーなど標準Editメッセージを使えない入力欄を含めるには、この組み合わせを評価する価値がある。

推奨する構成は併用方式。標準Edit / RichEditとして確認できた入力欄ではWin32の直接置換を優先し、それ以外はUIAで取得・選択してSendInputで変換結果だけを送る。実際のWebメモでUIAの取得・選択・文字入力が成功するかを最初に検証する。UIAが対応しているだけで全アプリを編集できるとは保証しない。

UIAのTextPatternに対応していることだけを根拠に、編集可能とは判断しない。Chromeや独自エディターで読み取りができても、Win32編集メッセージで編集できるとは限らない。UIAのValuePatternまたはLegacyIAccessible.SetValueを使える入力欄では値全体の更新を検討できるが、対応アプリごとにカーソル・Undo・保存通知を確認する。

クリップボード不使用モードでは未対応のアプリを明示して中止し、コピー・貼り付けへ自動で戻さない。モデル、区切り設定、ショートカット設定は既存実装を再利用する。

## Win32編集メッセージ

対象は実際に標準編集コントロールとして動作するHWND。ブラウザーのウィンドウハンドルは、DOM上のtextareaのハンドルではない。

| 操作 | メッセージ／方法 |
| --- | --- |
| 本文長・本文取得 | WM_GETTEXTLENGTH / WM_GETTEXT |
| 選択範囲・カーソル取得 | EM_GETSEL。DWORDの出力引数を使う |
| 対象範囲の選択 | EM_SETSEL |
| 対象だけの置換 | EM_REPLACESEL、wParam=TRUEでUndoを許可 |
| カーソル復元 | EM_SETSELの始点・終点を同じ位置にする |
| 処理完了・タイムアウト | SendMessageTimeoutWと、置換後の再読み取り |

取得時にShift+Home/EndやCtrl+Cを送る必要はない。本文とカーソルを読み、設定された漢字・区切り文字まで左へ走査する。推論中は選択状態を変更せず、適用直前だけ対象を選択して置換する。行全体を再入力する必要はなく、折り返しに左右される矢印キーによるカーソル復元も不要になる。

別プロセスの入力欄にはGetWindowTextWではなくWM_GETTEXTを送る。標準のEM_GETSELは返り値に16ビットずつ位置を詰めるが、出力引数では32ビットの位置を取得できる。長文でも返り値の上位・下位ワードだけに頼らない。[GetWindowTextW](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowtextw)、[EM_GETSEL](https://learn.microsoft.com/en-us/windows/win32/controls/em-getsel)

Win32のUnicodeコントロールの位置はUTF-16単位として扱う。Pythonのlenとは補助平面の漢字・絵文字で異なる。改行も編集コントロールが扱う単位と照合し、取得本文を無条件にLFへ正規化してから位置を計算しない。

RichEditではさらに、取得APIが返すCR/CRLFや、埋め込みオブジェクト・表の構造文字が位置に影響する。EM_GETTEXTEXのGT_RAWTEXTは内部文字位置との対応を保つための指定だが、この専用メッセージを外部プロセスに使うには別の問題がある。[GETTEXTEX](https://learn.microsoft.com/en-us/windows/win32/api/richedit/ns-richedit-gettextex)

Windowsが自動的に別プロセス向けのポインター引数をマーシャリングするのはWM_USER未満のシステムメッセージ。EM_GETSEL、EM_SETSEL、EM_REPLACESELはこの範囲で利用できる。一方、EM_EXGETSELやEM_GETTEXTEXなどのRichEdit専用メッセージに、自プロセスの構造体のアドレスをそのまま渡す設計は採用できない。必要になれば対応する別の取得方法やCOM・アプリ側の協力を検討する。リモートメモリ操作を初期版の標準手段にはしない。[SendMessageのプロセス間通信仕様](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendmessage)

EM_REPLACESELは成功を表す値を返すAPIではない。SendMessageTimeoutの正常終了は「処理から戻った」ことを意味し、期待した文字列になったかは再取得で確認する。読み取り専用、入力上限、保護範囲、アプリ独自処理により期待どおりにならない可能性がある。タイムアウトや結果不明の場合に置換を再送しない。RichEditの新しい文字列の書式は選択範囲先頭などの書式を継承するため、範囲内の混在書式までそのまま保つとは限らない。[EM_REPLACESEL](https://learn.microsoft.com/en-us/windows/win32/controls/em-replacesel)

UIPIにより、通常権限のプロセスから高い整合性レベルのアプリへのメッセージ送信には制限がある。初期版では同等権限の入力欄を対象にし、権限が合わない場合は失敗理由を表示する。

### 実験で確認した範囲

Pythonから別のPythonプロセスを起動し、非表示のEDITとRICHEDIT50Wを作成した。WM_GETTEXT、EM_GETSEL、EM_SETSEL、EM_REPLACESELをプロセス間で送信し、以下を確認した。ユーザーの入力欄とクリップボードは操作していない。

- `前😀。さんぽ?後ろ`の対象範囲を取得する。
- `さんぽ`だけを`散歩`へ置換して前後の文字を保持する。
- 絵文字をUTF-16の2単位として数え、カーソルを`散歩`の直後へ設定する。
- WM_UNDOで元の文章へ戻す。

両コントロールで成功した。これは用意した単一行の試験文字列に対する結果であり、メモ帳の全バージョン、Webメモ、長文・複数行・リッチ文書の動作確認ではない。

## UI Automation

UIAはCOMインターフェース。Pythonから利用する場合はCOMバインディングを用いる構成が候補になる。依存ライブラリの採用は実装前に決める。

### 読み取りとカーソル

1. IUIAutomation.GetFocusedElementでフォーカス中の要素を取得する。
2. TextPatternのGetSelectionで選択範囲、DocumentRangeやTextRange.GetTextで本文を取得する。
3. TextPattern2.GetCaretRangeに対応していれば、カーソル位置の空範囲を取得する。
4. カーソルの範囲をCloneして必要な前後文脈まで広げ、まとまった単位で取得する。
5. 対象区切りを計算し、置換する範囲と前後文脈を保持する。

GetCaretRange非対応では、GetSelectionが返す空範囲をカーソルとして使えるか確認する。選択なしで空のコレクションしか返さない実装なども区別し、画面上の座標だけで文字位置を推定しない。[GetCaretRange](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationtextpattern2-getcaretrange)、[TextRange](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-usingtextrangeobjects)

TextRangeの文字単位移動とPythonの文字インデックスが同じとは仮定しない。絵文字、結合文字、埋め込み要素やプロバイダーの単位に合わせて範囲を対応づける。全文置換後は以前の範囲が無効になりうるため、カーソル復元には新しい範囲を取得する。

### 書き込みの制約

TextPattern / TextRangeには任意の範囲へ文字列を設定する共通メソッドがない。TextEditPatternという名前のAPIも、GetActiveComposition / GetConversionTargetによって入力中の状態を取得するもので、汎用の部分置換APIではない。[UIAテキストモデル](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-understandingtheuiautomationtextobjectmodel)、[TextEditPattern](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nn-uiautomationclient-iuiautomationtexteditpattern)

ValuePattern.SetValueを公開する入力欄なら、元本文の対象部分を変換結果へ置換した完成文字列を、入力欄全体の新しい値として設定できる。これは表示行だけでなく入力欄全体の更新になり、長文の転送コスト、Undo、書式、カーソル復元、アプリの保存・変更通知を確認する必要がある。ValuePatternしかない入力欄では、正確なカーソル位置を取得できるとも限らない。[SetValue](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationvaluepattern-setvalue)

LegacyIAccessiblePattern.SetValueも候補だが、対応するMSAA実装次第であり、やはり値全体の設定。選択したテキスト範囲だけを置換するメソッドとは扱わない。[LegacyIAccessiblePattern](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nn-uiautomationclient-iuiautomationlegacyiaccessiblepattern)

### TextRange.Select + SendInputの再評価

この組み合わせなら、ValuePatternによる全文設定を使わずに選択した部分を入力で置き換えられる。Selectは現在の選択を置き換え、空範囲へのSelectはカーソルをその位置に移動する。Microsoftのサンプルでも、UIA経由の値設定が使えない入力欄にフォーカスを与えてキー入力を送る構成が示されている。ただしサンプルはSendKeysを使う例であり、今回の日本語・対象アプリでSendInputが動くことを検証した例ではない。[Select](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationtextrange-select)、[UIAとキー入力の例](https://learn.microsoft.com/en-us/dotnet/framework/ui-automation/add-content-to-a-text-box-using-ui-automation)

SendInputのKEYEVENTF_UNICODEは、ローマ字キーの再現ではなくUnicode文字をVK_PACKETとして送り、通常はTranslateMessageを経てWM_CHARになる。入力配列は順に挿入され、同一のSendInput呼び出し内のイベント間に他のキー入力が混ざらない。これはエディターの変更全体が単一トランザクションになるという意味ではない。返り値は入力イベントを挿入した数であり、エディターが最終的な文章へ反映したことの確認は別途必要。[KEYBDINPUT](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-keybdinput)、[SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput)

過去の文字崩れについて、SendInputが原因だと確定した実験結果はない。旧実装も文字イベントをまとめて1回のSendInputで送っており、バッチ化だけを新たな解決策とは扱わない。UIAで取得と選択を直接行うこと、行全体ではなく変換対象だけを入力することは旧方式との差になる。ただし文字入力の受け手側に原因がある場合は残る可能性がある。

新方式での適用手順案:

1. カーソル・選択範囲と必要な前後本文をUIAから取得する。取得中に選択を動かさない。
2. モデルで対象を変換する。待機中のユーザー操作や入力先変更を検知する。
3. 対象要素が引き続き前面の入力先であり、本文・選択位置が変わっていないことを確認する。
4. ショートカットの主キーと修飾キーの解放を確認する。検出可能な入力中のIME変換があれば中止する。IMEを勝手に切り替える設計にはしない。
5. 対象TextRange.Selectを実行し、GetSelectionで同じ範囲になったことを確認する。非同期の選択反映には上限付きで待つ。別ウィンドウへ移った場合に入力先を強制的に戻さない。
6. 変換結果だけをUTF-16単位のUnicode入力としてまとめて送る。先にDeleteを送って原文を消す方式は避ける。
7. UIAで本文・カーソルを再取得し、対象の置換と前後文字の保持を確認する。入力イベントを全部送れなかった場合や、結果不明の場合は再送しない。
8. 必要なら新たに取得した空のTextRange.Selectでカーソルを復元する。以前の範囲が編集後も有効とは仮定しない。

Selectは文字範囲を選ぶ操作であり、SendInputをそのUIA要素宛てに送る操作ではない。SendInputは前面の入力先へ届くため、選択後にもフォーカスを確認する必要がある。API間のフォーカス変更の競合を外部から完全に排除することはできない。

最初の検証は単一行の日本語と記号を対象にする。改行、絵文字のサロゲートペア、結合文字、入力中のIME、Undoのまとまり、サイト独自のinputイベント処理を追加で確認する。SendInputにはCtrl+V用のクリップボード復元待機は不要だが、選択反映と文字入力完了の確認は残る。UIAの通信時間も含め、速さは対象アプリで計測する。

### 2方式の判断基準

| 項目 | UIA + Select + SendInput | Win32編集メッセージ |
| --- | --- | --- |
| クリップボード | 不使用 | 不使用 |
| 対応範囲 | UIAで本文と選択を公開し、Unicode入力で編集できる入力欄 | 標準メッセージに対応するEdit / RichEdit |
| ブラウザーのWeb入力欄 | 候補。実際のブラウザー・入力欄で確認が必要 | 通常はDOM入力欄へ直接使えない |
| 置換処理 | 対象を選択し、入力イベントで置換 | EM_REPLACESELで直接置換 |
| フォーカス | 実際の前面入力先を維持する必要あり | メッセージはHWND宛て。製品としてはフォーカス変更時に中止 |
| 完了判定 | UIAから再取得して確認 | メッセージ応答後に再取得して確認 |
| Undo | アプリの入力イベント処理による | Undoを許可する指定が可能。まとまりはアプリでも確認 |
| 実装の中心 | COM、範囲の対応づけ、選択・入力の同期 | HWND、UTF-16位置、改行、メッセージ通信 |

ブラウザーを含む複数アプリへの対応を優先して一方だけ試すならUIA + SendInputを先に評価する。標準編集コントロールだけを対象に安定した部分置換を作るならWin32が優先。最終構成では入力欄の能力に応じた併用が合理的で、UIAの全文SetValueは既定の置換手段にしない。

### 性能とスレッド

UIAの呼び出しはプロセスをまたぐ。1文字ずつGetTextする設計を避け、必要な範囲をまとめて取得する。UIAだから一定時間内に必ず応答する、とは判断しない。

Windowsのメッセージループと別に、COMをMTAとして初期化したUIA専用ワーカーを持つ設計を検討する。COMの要素・範囲を推論ワーカーに渡さず、推論へ渡すのは文字列データだけにする。イベント登録と解除も同じワーカーで扱う。応答停止への対策を必要とする場合は、専用プロセスへ分離する。ただし書き込み中のタイムアウトは編集結果が不明になるため、再試行はしない。[UIAのスレッド仕様](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading)

## 対応を決めるための診断

アプリ名だけでバックエンドを決めず、実際にフォーカスされた入力欄を検査する。

| 診断項目 | 確認する理由 |
| --- | --- |
| プロセス、HWND、クラス名 | 標準Edit / RichEditか、ブラウザー等の独自コントロールか |
| UIAのRuntimeId、ControlType、FrameworkId | 同じ入力要素を再確認できるか |
| TextPattern / TextPattern2 | 本文、選択範囲、カーソルが取得できるか |
| ValuePattern / LegacyIAccessiblePattern | 値を直接設定できる経路があるか |
| IsReadOnly / IsEnabled / IsPassword | 編集に適した入力欄か |
| 実際の本文と選択位置 | UIAの値が本文なのか、ラベルやページ全体なのか |

最初は能力を表示するだけの診断を用意し、実際のWebメモと普段使うアプリで記録する。UIA対応というだけで、ブラウザーのtextareaを安全に編集できるとは結論づけない。今回の調査では実際のWebメモのUIA能力は確認していない。

## 既存実装からの変更案

モデル、かな部分の変換、区切り設定、ショートカット設定は共有する。次の責務をバックエンドごとに分ける。

- capture: 本文・対象範囲・カーソル・入力要素の識別情報を取得する。
- validate: 推論後に同じ入力要素・本文・選択位置か再確認する。
- replace_range: 対象範囲だけを直接置換し、変更結果を確認する。
- restore_caret: 変換による文字数の差と保持した末尾記号を考慮して位置を設定する。

現在のapply_resultはcopy_selectionで再確認し、行全体をreplaceへ渡す。クリップボード不使用版ではこの経路も置き換える必要がある。stampからクリップボード番号を除くだけでは不十分。

Win32の識別情報はHWNDとプロセスIDを含め、UIAでは入力要素の識別と範囲を扱う。本文・選択位置の再確認は編集直前にも実施する。複数のAPI呼び出し全体を外部から完全な単一トランザクションにはできないため、途中でユーザーが操作した場合や内容が変わった場合は中止する。結果不明の編集は再送しない。

## 実装順序と確認項目

1. 読み取り専用の能力診断で、実際のWebメモと普段使うアプリの本文・カーソル・選択機能を確認する。
2. UIA + Select + SendInputの単一行の試作を対象アプリで比較する。旧実装の文字崩れが再発するかも検証する。
3. 標準Edit / RichEditではWin32の直接置換経路を優先して追加する。
4. 両方式で改行・長文・絵文字・Undo・タイムアウトを確認する。取得、選択、入力完了を別々に計測する。
5. UIAの取得・選択やUnicode入力にも対応しないアプリは未対応とするか、ブラウザー拡張・アプリ専用APIを検討する。

試験項目はK/Jの範囲、区切り設定、カーソル途中・末尾・記号直後、複数行、折り返し、絵文字・補助漢字・結合文字、Undo、推論中の本文・選択・フォーカス変更、読み取り専用、入力上限、対象アプリ終了、タイムアウト。性能は取得・推論・再確認・置換・カーソル復元を別々に計測する。推論時間はどの方式でも残る。
