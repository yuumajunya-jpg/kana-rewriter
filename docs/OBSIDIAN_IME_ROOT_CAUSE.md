# ObsidianのIME確定後にUIA選択が失われる原因

2026-10-22の実操作ログで、選択解除の呼び出し元まで特定した。
今回の失敗は、UIA範囲の所属要素の違いではなく、Obsidianの確定後処理とDOM選択取り込みの実行順の競合だった。

## 証拠の組

接頭辞 `logs/obsidian-20261022-000055-069862` の次のファイルを照合した。

- `-session.json`：renderer PID 21152が継続、valid=true、dropped=0、error=null。
- `-uia.log`：要求253:260、実選択260:260でタイムアウト。
- `-dom.jsonl`：DOM選択・CodeMirror状態・選択変更の呼び出し元。
- `-evidence.json`：成功例・失敗例の該当32イベントを抽出。
- `-source.json`：起動中のObsidianに同梱されたapp.jsの該当実装とSHA256。

ソースSHA256は `958ce57fe22f87fdf121546e46dfccd9c8ee971ce3fd8f801d60d4ece39a2df9`。
UAに記録された基盤はChromium 142.0.7444.265 / Electron 39.8.3。

## 最後の失敗の時系列

以下の時刻はDOMログのローカル時刻（日本時間）。UIAログとの壁時計の数msの差を、内部処理の順序の証拠には使わない。
同じDOMログ内のbefore/after記録で順序を確定する。

| 時刻 | 観測 |
| --- | --- |
| 00:01:30.417 | compositionend。DOMもCodeMirrorもカーソル位置 |
| 00:01:30.521 | DOMには対象「へんかんてすと」が42:49で選択済み。一方CodeMirrorは3776:3776のカーソルのまま |
| 00:01:30.521 | app.js:1:2558641からdispatchTransactionsが呼ばれる |
| 00:01:30.522 | CodeMirrorのupdateSelectionが呼ばれる。DOMはまだ42:49の選択 |
| 00:01:30.522 | app.js:1:369476のSelection.collapseが49を指定して実行される |
| 00:01:30.522 | collapseのafter記録でDOM選択が49:49に消える |
| 00:01:30.523 | selectionchangeが配送された時点では、すでに空選択 |
| 00:01:32.518（UIAログ） | 選択確認471回後もカーソルのままで中止 |

DOMの42:49は行内のTextノードの位置、3776はCodeMirror文書全体の位置、
UIAの253:260はUIAが公開した本文内の位置。異なる座標系を直接比較して位置ずれとは扱わない。
3つの記録の対象文字列と行末文脈を照合した。

## 選択を解除した処理

起動中のObsidianのapp.jsを読み取り確認した結果、呼び出し元2558641は次のハンドラーだった。
整形のみ行った抜粋：

```javascript
compositionend: function(event, view) {
  setTimeout(function() {
    return view.dispatch({});
  }, 100);
}
```

つまり、IME確定から約100ms後に空のトランザクションを発行するObsidian独自の処理である。
今回、UIAがDOMに選択を設定した直後、selectionchangeをCodeMirrorが処理する前にこのタイマーが実行された。
CodeMirror内の選択は古いカーソル位置のままで、更新処理がそれをDOMに書き戻した。
その書き戻しが実際のSelection.collapseとして記録されている。

「Selectが無効だった」ではなく、「Selectは届いたが、CodeMirrorに取り込まれる前に解除された」と判別できた。
範囲構築失敗・AI変換失敗・TSF確定通知の欠落・物理操作による中止では説明できない。

## 成功例との差

直前の00:01:28の成功では、DOM選択37:44に対するselectionchangeが先に処理されている。
CodeMirrorのonSelectionChange→flush→dispatch経路が、内部の選択を3764:3771に更新した。
その後、同じapp.js:1:2558641から空のトランザクションが発行されても、
内部状態とDOMの選択が一致しているため解除されず、置換文字を入力できた。

00:01:16の失敗も、DOMの20:27の選択に対し同じ確定後処理がcollapse(27)を実行していた。
この回はUIAの最初の選択確認を通過し、最終確認で選択消失を検知して中止した。
選択反映待ち中の失敗と送信直前の失敗を、同じ競合で説明できる。

ただし、前日の別セッションで記録した「送信後に本文が増えた」事象はDOMログを伴わない。
その事象も同じ原因だったとまでは断定しない。

## 修正の対象と制約

根本的に直す対象は、確定後の空トランザクションを実行する前のDOM選択の取り込みである。
実行順を「最新DOM選択をCodeMirrorへ取り込む→確定後更新」にすれば、
古い内部選択でDOMを上書きする競合を除去できる。

候補は確定後ハンドラー側で `view.observer.flush()` を先に実行する修正。
起動中のCodeMirrorのflush実装でも、readSelectionRange→readChange→状態更新の順であることを確認した。
公開実装の参照： https://raw.githubusercontent.com/codemirror/view/main/src/domobserver.ts

これはObsidian内部の変更案であり、現時点で適用・実動検証はしていない。
Obsidianの内部APIへ依存するため、Python側の一般UIA処理へ黙って組み込む変更にはしない。
外部アプリ内に適用する修正と、かな変換プログラム側の処理を明確に区別する。
固定待ち時間の追加、タイムアウト延長、Selectの再送、Shift+左矢印への変更は解決策として採用しない。

今回の調査で本体の選択方式は変更していない。実操作の再現は利用者が行い、解析は保存ログと読み取りだけで実施した。

## 対策の比較（2026-10-22追検討）

前提はUIAのSelectによる選択を維持すること。固定待ち・再選択・キー選択への切り替えは使わない。
実装・アプリへの適用はまだ行わず、候補の成立条件と問題点を比較した。

| 対策 | 競合への作用 | 速度・依存 | 判断 |
| --- | --- | --- | --- |
| Obsidianの確定後コールバック内で選択を取り込み、その後に空dispatch | 問題の書き戻しを行う前に内部選択を最新化 | 新たなタイマー待ちは不要。内部API依存 | 原因箇所への最小修正。開発元修正として第一候補 |
| Obsidian拡張で、空トランザクションに未反映のDOM選択を反映 | 古い選択を保った空更新が作られることを防ぐ | 公開拡張API中心に構成できる可能性。DOM位置変換・他拡張との整合が必要 | 手元で継続利用する対策の検証候補 |
| 変換前後にCLI/IPCからCodeMirror同期を要求 | 同期要求自体とタイマーの間に競合が残る | 変換ごとのIPCが増える | 単発の同期呼び出しだけでは解決を保証できない |
| PythonのUIA確認を増やす・安定した選択を数回観測する | 選択が今あることは分かるが、後続の解除を防げない | UIA往復が増える | 根本対策にならない |
| UIAのTextSelectionChanged等を待つ | DOM側の変化通知であり、CodeMirror取り込み完了と同じとは限らない | 完了条件の証明が必要 | 現時点で同期障壁として採用できない |
| selectionchangeのcaptureリスナーで同期する | 今回はイベント配送自体より先にタイマーが選択を消した | リスナーの順番だけでは間に合わない | 単独では不十分 |
| 選択後に無害そうなキーを送ってkeydown経由で同期 | CodeMirrorのkeydown経路にはforceFlushがあるが、そのキーより先にタイマーが走り得る | ショートカットや他プラグインへの影響 | 採用しない |
| 空dispatchの削除・100msタイマーの短縮 | 当該競合の発生箇所を変える | 元処理の目的・IME確定後の描画への影響が未確認 | 安易に削除・短縮しない |
| CodeMirror APIで直接置換 | DOM選択を経ず内部トランザクションで編集できる | Obsidian専用の別編集方式になる | 利用者のUIA方式維持の指示に反するため採用しない |

### 最小修正の正しい位置

同期を入れるなら、空の `view.dispatch({})` を呼ぶ前、つまりトランザクション生成前に置く必要がある。
CodeMirrorのdispatchは現在のstateからトランザクションを作ってからdispatchTransactionsへ渡す。
生成後のdispatchTransactionsやupdate内部でflushしてstateが変わると、元トランザクションのstartStateが古くなり、
状態不一致や更新中の再入を招く。起動中Obsidianのupdateにも、その不一致を拒否する検査が存在した。

また、単純なflushはdelayedFlush等の状態によって実行を保留する場合がある。
従って「flushを1行足せば必ず直る」とは扱わず、実際に選択が取り込まれたことと
元処理の描画更新が保たれることを検証する。forceFlushの無条件実行も、IME中の保留を無視するため避ける。

### 手元での対策候補

Obsidianの公開 `registerEditorExtension` とCodeMirrorのトランザクションフィルターを使い、
本文変更・明示的選択変更・effectsを含まない空更新について、同じ入力欄内の未反映DOM選択を
更新のselectionへ組み込む案を先に小さく検証する余地がある。
DOMの端点から文書位置への変換には公開 `posAtDOM` を使える。
ただし、公開APIがあることと、その組み合わせで安全に解決できることは別であり、現時点では未実装・未検証。

この案では再入dispatchを行わず、対象トランザクションの返却仕様だけを変える設計にする。
IME中、別入力欄の選択、文書が更新中のDOM、複数選択、逆向き選択、折り畳み、ライブプレビュー装飾、
他のトランザクションフィルターとの関係を検証する必要がある。
公開APIだけで整合を保証できない場合は、広範囲の内部メソッド差し替えへ拡大せず、
原因コールバックに限定した修正案を開発元へ提示する。

公式参照：

- https://github.com/obsidianmd/obsidian-api/blob/master/obsidian.d.ts （registerEditorExtension）
- https://github.com/codemirror/view/blob/main/src/editorview.ts （dispatch生成順・posAtDOM・更新中検査）
- https://github.com/codemirror/view/blob/main/src/domobserver.ts （flush・forceFlush・選択取り込み）

### 検証の合格条件

単に成功回数を見るのではなく、内部監視付きの利用者による再現で
「確定後タイマーがUIA選択の取り込み前に走る順序」を再度採り、その順序でも選択が保持されることを確認する。
逆の順序でも成功し、選択操作はSelect1回、置換送信は1回、通常経路に追加の待機がないことを条件とする。
元のIME確定・Undo/Redo・日本語入力・他エディターの動作も確認する。

## Obsidianを書き換えない案の追加調査

「アプリのコード・ハンドラー・プラグインを変更しない」と「現在のUIA選択＋キー送信方式を維持する」は別の条件。
前者だけなら、既存の編集APIを外部から呼ぶ方法も検討できる。
後者も維持する場合、CodeMirrorがDOM選択を取り込んだことを外部から保証する同期手段がまだ見つかっていない。

### Windows側の同期手段

- WM_NULLのSendMessage往復は受信ウィンドウの応答を確認するが、別レンダラーのJavaScriptタイマーや選択イベントを処理済みとは保証しない。
- WaitForInputIdleは継続的な編集完了の同期用ではない。一度idleになった後の呼び出しは、処理中でも直ちに返るという仕様がある。
- SynchronizedInputPatternは入力が対象要素へ届いたかを通知する。CodeMirror内部への選択取り込み完了を通知する仕様ではない。
- UIAからMSAA/IAccessible2の選択APIへ変更しても、今回の「DOM選択が成立してから解除される」原因を除去したとは言えない。
- UIA照会回数の削減などで選択から入力までを短縮する案は、競合確率の低減に留まる。これだけで修正済みとは扱わない。

### 既存のObsidian編集APIを外部から利用する案

公式CLIのevalから既存Editor APIを呼べば、コード改変やプラグイン導入はせず、
エディターの内部状態を通して編集できる可能性がある。DOMだけを選択してから文字を送る経路とは異なる。
ただし、単純なCLIプロセス起動を毎回行うと遅延が増える。
この環境で本文を操作しないevalの往復を5回測定したところ124.7/92.7/95.7/93.7/92.0ms、中央値93.7msだった。
これは接続・読み取りの時間であり、置換の速度・正確性を検証した数値ではない。
高速化には接続の維持などを別途検討する必要があるが、常設プラグインや内部関数の差し替えを必須とする案ではない。
採用する場合はObsidian専用の別編集経路と明示し、UIA失敗後のフォールバックとしては使わない。

### TSFのテキスト編集APIを利用する案

ITfContext::RequestEditSessionとITfRange::SetTextには、書き込み可能な編集セッションで範囲本文を変更する正式APIがある。
既存のTSF確定監視を拡張し、確定した範囲の本文確認と置換をTSF経由で行うことが技術的な検討候補になる。
アプリのJSやプラグインを変更する案ではないが、現行の監視専用DLLへ編集責務を追加する大きめの変更になる。
Read/writeロックの取得、確定範囲の寿命、新しい入力による中止、通知の自己再帰、
ChromiumとCodeMirrorへの編集伝達、UndoとIME状態を実際に検証するまで解決策とは断定しない。
TF_ES_SYNCは任意のタイミングで必ず成功するものではなく、同期モードの強制は採用理由にならない。
この案もUIA選択＋文字送信からは編集方式が変わる。利用者の既存方針を無視して導入しない。

参照：

- https://learn.microsoft.com/en-us/windows/win32/winmsg/about-messages-and-message-queues
- https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-waitforinputidle
- https://learn.microsoft.com/en-us/windows/win32/api/uiautomationclient/nf-uiautomationclient-iuiautomationsynchronizedinputpattern-startlistening
- https://learn.microsoft.com/en-us/windows/win32/api/msctf/nf-msctf-itfcontext-requesteditsession
- https://learn.microsoft.com/en-us/windows/win32/api/msctf/nf-msctf-itfrange-settext
- https://obsidian.md/help/cli

追加調査は読み取り・時間計測のみ。本体やObsidianの動作変更は行っていない。
