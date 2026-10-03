# kana-rewriter

Windowsで入力確定済みの日本語をローカルAIで漢字かな交じり文に差し替えます。`llama-cpp-python`からGGUFを直接読み込みます。既定ではクリップボードを使わず、標準Edit/RichEditはWin32の編集メッセージ、その他の入力欄はUI Automationによる範囲選択とUnicode入力で置換します。

例: `歯が痛いので、はいしゃ│に行く`（│はカーソル） → `歯が痛いので、歯医者に行く`

## モデルとセットアップ

第一候補は**jinen-v1-xsmallのQ5_K_M版（31.2MB）**。左文脈を専用マーカーで渡す変換専用モデルです。現行mainにはGGUFが見当たらないため、公開時のリビジョンを指定します。詳細と代替候補は[MODELS.md](MODELS.md)に記載しました。

使用者のWindows環境で`--text`による推論が動作し、左文脈「歯が痛いので、」で「歯医者」、「車が壊れたので、」で「廃車」となることが確認されました。広い範囲での精度や各入力アプリの動作は未評価なので、最初に`--text`で確認してください。

Windows 10/11、Python 3.11以上を用意し、cmdで:

```cmd
cd C:\workfile\kana-rewriter
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[local,desktop]" huggingface_hub
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

既存環境の更新では `.\.venv\Scripts\python.exe -m pip install -e ".[desktop]"` を実行してください。設定を省略した場合も新しい`auto`方式を使います。既存の区切り・モデル・ショートカット設定はそのまま利用できます。

```toml
edit_backend = "auto" # 標準Edit/RichEditはWin32、それ以外はUIA
editor_timeout_seconds = 5
max_document_chars = 200000
```

`edit_backend`は`auto`、`win32`、`uia`、`clipboard`を指定できます。`win32`・`uia`は検証用の固定方式です。未対応の場合にクリップボードへ自動で戻りません。旧方式を使う場合は明示的に`clipboard`を指定してください。

| キー | 対象 |
| --- | --- |
| Ctrl + Alt + K | カーソル左から区切りまでの範囲（既定は漢字・「。」） |
| Ctrl + Alt + J | 選択範囲内のひらがな・全角カタカナ。漢字・句読点などは保持 |
| 起動端末でCtrl + C | 終了。推論中は完了を待つ場合がある |

変換ショートカットは`config.toml`で変更できます。以下は既定値で、`hotkey_quit`を指定すると端末以外からも終了できます。

```toml
hotkey_line = "Ctrl+Alt+K"
hotkey_selection = "Ctrl+Alt+J"
hotkey_quit = "" # 例: "Ctrl+Alt+Q"。空文字列では終了キーを登録しない
```

修飾キーは`Ctrl`・`Alt`・`Shift`・`Win`、主キーは英字・数字・`F1`〜`F24`（F12を除く）、`Space`・`Enter`・`Tab`・`Escape`・`Backspace`・`Insert`・`Delete`・`Home`・`End`・`PageUp`・`PageDown`・`Left`・`Up`・`Right`・`Down`に対応します。例: `Ctrl+Shift+K`、`Win+Space`、`F8`。大文字・小文字は問いません。ファンクションキー以外は修飾キーを付けてください。設定内の重複はエラーにし、他アプリとの競合やOSの予約によって登録できない場合は起動を中止します。終了キーも含め、推論中の終了は完了を待つ場合があります。端末のCtrl+CはOSの割り込みとして常に使えます。変更後は再起動してください。

Kは未選択の状態で使います。既定では、カーソルから左へ漢字または句点「。」まで走査します。「、」「?」「!」や空白・英数字・カタカナは区切りにしません。本文の先頭と改行でも止めます。画面上の折り返しには左右されません。範囲内にかながなければ中止します。助詞の判定はないため、`私はきょう│`では`はきょう`が対象です。

カーソル直前が `。 、 ？ ? ！ ! , . ， ．` の場合は、連続するこれらの記号を飛ばして、その左から同じ走査を行います。これは区切り文字の設定とは独立しています。記号は保持します。例: `今日はいい天気だ。さんぽでもしようか。│` → `今日はいい天気だ。散歩でもしようか。`。

変換による文字数の変化を考慮してカーソルを復元します。記号の直後から実行した場合は記号の直後に戻します。Win32はUTF-16の選択位置、UIAは新しく取得した空のTextRange.Selectを使います。Jでは変換範囲の末尾に置きます。UIAの文字単位と本文を対応づけられない位置では中止します。

K・J・`--text`では、専用モデルに渡せる連続したかなの部分を順に変換します。漢字・句読点・空白・改行・英数字はそのまま保持します。新方式のJでは選択範囲外の文脈も取得します。単語や助詞の判定は行いません。

入力欄の本文から対象前後を取得します。既定のモデル入力は最大64文字の左文脈のみです。`zenz_v3_2`では左右両方を渡します。置換対象以外の文字はモデルに生成させず、そのまま保持します。

推論中は入力やマウス操作をせずに待ってください。取得時は選択を動かしません。ユーザー操作・入力先変更・本文や選択位置の変更を検出すると適用を中止します。UIAでは選択反映を確認してから変換結果を一度だけ送信し、本文を再取得して結果を確認します。確認に失敗した場合は再送しません。中止後に選択範囲が残った場合は解除するか、選び直して再実行してください。

## 入力形式と設定

専用モデルはチャットではなく`create_completion`を使います。ひらがなを全角カタカナにし、以下のマーカーを挿入します。表記は説明用で、実際はそのUnicode文字を送ります。

```text
jinen_v1:   \uEE02左文脈\uEE00カタカナの読み\uEE01
jinen_v2:   \uEE02左文脈\uEE00カタカナの読み\uEE01（全体をNFKC正規化）
zenz_v3_2:  \uEE02左文脈\uEE07右文脈\uEE00カタカナの読み\uEE01
```

空文脈の扱いも形式に合わせます。余分なBOS/EOSを追加せず、temperature=0、反復ペナルティなしで生成します。EOS/`</s>`で停止し、打ち切り・空出力・制御マーカー・空白や改行を含む結果は拒否します。

jinen v2を使う場合は、ダウンロード後に`config.toml`の以下の2項目を変更して再起動します。smallとxsmall、各GGUF量子化版で同じ形式を使えます。

```toml
model_path = "models/jinen-v2-small-Q5_K_M.gguf"
model_format = "jinen_v2"
```

v2は左文脈が空なら`\uEE02`を省略し、組み立てたプロンプト全体をNFKC正規化して、temperature=0・top_k=1で生成します。正規化するのはモデル入力だけで、元の本文の英数字・記号・対象外の文字は保持します。v1の入力形式は変更しません。仕様は[作者のモデルカード](https://huggingface.co/togatogah/jinen-v2-small.gguf)に従っています。

`config.example.toml`が設定例です。`--config`を省略すると既定値を使用し、`config.toml`は自動読み込みしません。

- `model_format`: `jinen_v1`、`jinen_v2`、`zenz_v3_2`、汎用モデル用の`chat`。
- `model_path`: 設定ファイルからの相対パス、または絶対パス。
- `n_ctx = 0`: GGUFの学習時コンテキスト長を使用。
- `context_chars`: 文脈の最大文字数。0で文脈を無効化。
- `conversion_delimiters`: Kで左へ走査するときの区切り文字。既定は`"。"`。例: `"。、?!？！"`なら読点や疑問符・感嘆符でも止めます。
- `stop_at_kanji`: Kの走査を漢字で止めるか。既定`true`。`false`でも専用モデルにはかな部分だけを渡し、漢字は保持します。
- `trailing_punctuation`: カーソル直前で飛ばして保持する記号。既定`"。、？！?!,.，．"`。空文字列で無効化します。範囲内の区切りには使いません。
- `max_chars`: 対象の文字数上限。`max_tokens`: 出力上限。
- `edit_backend`: 編集方式。既定`auto`。モデル接続方式の`backend`とは別設定です。
- `uia_ime_check`: UIAの未確定範囲の扱い。既定`auto`はChrome/EdgeなどChromium系の入力欄では参考情報として扱い、それ以外では中止判定に使います。`strict`は全UIA入力欄で範囲があれば中止、`off`は未確定範囲を照会しません。この設定は未確定入力の判定用で、送信時のIME一時オフとは別です。
- `editor_timeout_seconds`: 編集APIの応答待機。既定5秒、1〜30秒。応答不明時は編集を再送せず、ワーカーを停止します。再起動前に本文を確認してください。
- `max_document_chars`: 取得する本文の上限。既定200000文字、1000〜1000000文字。
- `paste_wait_seconds`: `clipboard`方式だけで使うクリップボード復元待機。既定0.1秒、0.1〜5秒。

`backend = "http"`も使用できます。専用モデルは`/v1/completions`、chatは`/v1/chat/completions`を指定します。接続はローカルに限定し、プロキシとリダイレクトを無効化します。`timeout_seconds`はHTTP専用で、直接推論の時間制限はありません。

既存の`config.toml`でも、新しい項目を省略した場合は漢字・「。」が区切りになります。明示する場合は次を追加し、待ち受けプロセスを再起動してください。

```toml
conversion_delimiters = "。"
stop_at_kanji = true
trailing_punctuation = "。、？！?!,.，．"
```

## 対応範囲と検証

- 新方式ではコピー・切り取り・貼り付けを行わず、クリップボード履歴に書き込みません。本文全体を再取得して確認しますが、書き込みは対象範囲だけです。
- Win32はUnicode標準Edit/RichEditのWM_GETTEXT・EM_GETSEL・EM_SETSEL・EM_REPLACESELを使います。別プロセスに用意した非表示Edit/RichEditで、改行と絵文字を含む本文の部分置換・カーソル復元・Undoを確認しました。
- UIAはTextPatternによる本文・選択位置の取得が必要です。TextRangeの文字単位は前後の本文との一致で確認します。実際のRichEdit上でCOMの本文取得・範囲選択を検証し、Unicode入力の順序と中止条件はモックで検証しています。Webメモなど実ブラウザーでの一連の編集は未検証です。
- UIAの改行はEnter入力として扱うため、単一行入力欄や独自エディターでは挙動が異なることがあります。タブを含むUIAの置換結果は選択変更前に拒否します。Undoのまとまり、リッチテキストの書式、サイト独自の変更通知はアプリ依存です。
- 読み取り専用・パスワード欄は対象外です。端末や独自入力コントロール、管理者権限の入力欄には対応しない場合があります。元のIME入力はEnterで確定してから実行してください。UIAの未確定範囲は対応する入力欄で照会しますが、既定ではChromium系の入力欄で中止の根拠にしません。確定後にも範囲が返るという利用者の報告があり、確認した[Chromiumの公開実装](https://chromium.googlesource.com/chromium/src/+/d6d648b6dc54c1c78936dc506e5ccebe23cee7a3/ui/accessibility/platform/ax_platform_node_win.cc)も変換範囲をキャッシュして確定通知を別に扱っています。ブラウザーの全バージョンで同じ挙動とは断定していません。非対応の入力欄（Win32経路を含む）や`off`では未確定状態を検出できません。別スレッドの入力欄に対してIMMの文字列長だけで未確定と断定しません。[TextEditPatternの未確定範囲](https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationtexteditpattern-getactivecomposition)
- UIAで文字を送る直前に対象アプリのIME状態を保存し、オンなら一時的にオフにします。IMEウィンドウへの制御メッセージで切り替えを確認し、確認できなければ文字を送りません。切り替え後にも本文と選択範囲を確認します。本文とカーソルの反映確認後に元のオン状態へ戻し、元からオフならそのままにします。EnterやEscapeは自動送信しません。Win32の直接置換ではIME切り替えは不要です。
- 通常の例外時にもIMEの復元を試みます。入力先が変わった場合は別の入力先のIMEを変更しないよう復元を省略し、警告を表示します。ワーカーの強制終了・応答停止では復元できない場合があります。中止時は本文とIME状態を確認してください。実ブラウザーでAPI切り替えが効くかは未検証ですが、利用者による手動IMEオフでは文字順の崩れと未確定文字の残存が解消したことが確認されています。[IMEウィンドウへの制御](https://learn.microsoft.com/en-us/windows/win32/api/immdev/nf-immdev-immgetdefaultimewnd)
- `clipboard`方式では従来どおり表示行全体（Jは選択範囲全体）の完成文を一度貼り付けます。Jの外側文脈は取得せず、ブラウザーのカーソル復元はEndと左矢印に依存します。クリップボードの通常形式は復元しますが、履歴には対象文字が残る場合があります。
- 読みの逸脱や同音異義語の誤選択は起こりえます。辞書による読み検証、候補選択、トレイUI、自動起動は未実装です。

```powershell
py -3 -m unittest discover -s tests -v
```

切り出し、文脈保持、専用マーカー、API呼び出し、応答検証、中止処理を確認します。モデル呼び出し・ウィンドウ操作のモックを含み、実モデルの精度や実アプリでの動作を保証するものではありません。

編集方式の調査資料は[NO_CLIPBOARD_DESIGN.md](NO_CLIPBOARD_DESIGN.md)にあります。入力欄の能力だけを確認する場合は、以下を実行して3秒以内に対象の入力欄へフォーカスを移します。モデルを読み込まず、本文を変更せず、本文の内容も表示しません。`backend`、`document_chars`、`selection`、または`unsupported_reason`を表示します。

```cmd
.\.venv\Scripts\python.exe -m kana_rewriter --config config.toml --inspect
```

文字が崩れる場合は、次の起動方法で変換対象・文脈・モデル出力・編集方式を確認できます。診断モードでは対象の文章が端末に表示されます。

```cmd
.\.venv\Scripts\python.exe -m kana_rewriter --config config.toml --debug
```

VS Codeで実際のカーソルと異なる`0:0`が取得される場合は、診断の`UIA位置`と`取得`の行を確認してください。空の選択範囲ではTextPattern2.GetCaretRangeを優先し、非対応・非アクティブならGetSelectionを使います。両方が先頭を返す場合は、この経路では実際のカーソルを特定できていません。VS Codeはスクリーンリーダー向けの本文をページ単位で公開するため、UIAの本文がファイル全体と一致するとは限りません。[VS Codeのアクセシビリティ仕様](https://code.visualstudio.com/docs/configure/accessibility/accessibility)

利用者の診断ログでは、`native-edit-context`が本文ではなく「この時点では、エディターにアクセスできません」という案内68文字を公開し、選択位置を`0:0`として返していました。この場合は案内を本文として扱わず、最適化モードの有効化を案内して中止します。VS Codeのエディターにフォーカスを置き、案内にあるShift+Alt+F1でスクリーンリーダー最適化モードを有効にするか、設定で`"editor.accessibilitySupport": "on"`を指定して再確認してください。この設定は本プログラムから自動変更しません。有効化後の部分置換成功はまだ未検証です。カーソル位置を本文の末尾と推定して置換する処理は行いません。

Web入力欄の選択反映は、本文を再取得した文字位置と対象文字列で確認します。中止時には要求した位置と実際の位置を表示し、確認できなかった場合は文字を送信しません。

本文と範囲の取得中にアプリ側が更新すると、一時的に古い本文と新しい選択位置が混ざることがあります。UIAではこの不一致だけを最大0.5秒、読み取り直して確認します。本文取得の前後でも内容が一致することを確認し、入力後は本文とカーソルの両方が期待値になるまで最大2秒待ちます。文字入力は再送しません。診断の`UIA入力: SendInput送信済み`以降に中止した場合は、すでに文字が変更されている可能性があります。

104文字の本文の`92:104`を12文字の読みから8文字の結果に置換し、本文100文字・カーソル`100:100`を確認する正常系テストを追加しています。範囲を選択する処理と非同期で変化するUIA本文・選択を模擬し、一連の取得・置換・確認を実行します。実ブラウザーへのSendInputの配送と描画は模擬しており、実ブラウザーでの成功を確認したテストではありません。
