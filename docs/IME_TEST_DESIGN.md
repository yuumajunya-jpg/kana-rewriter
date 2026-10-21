# IME相互作用のテスト設計

対象: UIAの`SendInput`置換におけるIMEの一時オフ・復元。実装は`kana_rewriter/direct_uia.py`、試験は`tests/test_direct.py`の`ImeInteractionTests`。

## 見直し前の不足

以前の試験は`ImeSession`を「呼ばれた回数」を記録するモックとして扱い、`send`はモデルの本文を直接書き換えていた。このため、次の既存システムへの影響を検出できなかった。

1. 対象範囲を選択した後のIME制御メッセージが、エディターの選択範囲を解除する。
2. IMEがオンのままUnicodeを送ると、置換結果が確定本文ではなく未確定の変換文字列になる。

実際に、IMEをオフにしない変更では全試験が成功した。また、選択後のIME照会を含む変更も、選択範囲の解除を再現できなかった。どちらも実アプリで初めて表面化した。

## 結合モデル

`FakeImeSession`はIMEとエディターを一つの状態機械として扱う。

- `open`: IMEのオン・オフ。
- `composition`: オンの間に送られたUnicodeを保持する未確定文字列。
- `selection_active`: 対象範囲が選択済みか。
- `sent`: 置換文字を送信済みか。
- `pending`: IME切り替え通知が入力先スレッドに未処理で残っているか。
- `UnselectableRange`: `GetSelection()`由来のRange。本文は読めるが`Select()`が反映されない。
- `AnchorRange`: 繰り返し置換でリスト項目が増えると、文書先頭側から作った左端が前のリスト項目のアンカーを保持し、本文は読めるが`Select()`が反映されなくなるRange。確認済み右端から左端を後方へ動かしたRangeは選択できる。

モデルの規則:

- IMEをオフにすると、入力先スレッドへ選択解除の通知が保留される。
- `flush_target_thread()`（入力先スレッドへの同期メッセージ）が保留中の通知を処理する。
- 保留中に`Select()`すると、通知が選択を`0:0`へ解除する。
- 対象範囲の選択後に`is_open()`（IME照会）が届いても、選択範囲を`0:0`へ解除する。
- `GetSelection()`由来のRangeは、本文が一致していても`Select()`が反映されない場合がある。`DocumentRange`でも、文書先頭側から左端を作ると前のリスト項目などのアンカーを引き継ぎ、同じように反映されない場合がある。置換を繰り返してリスト項目が増えると、この状態が再現しやすい。確認済みの右端から左端を後方へ動かしたRangeでは反映される。ChromiumのUIA実装は、選択範囲由来のアンカーがリストマーカーや別のアクセシビリティツリーにある場合、`Select()`を成功扱いのまま何もしない。
- IMEがオンの間に`send`された文字は本文へ入らず、`composition`へ入る。
- IMEがオフの間だけ、`send`された文字が選択範囲を置換する。

`TrackingRange`は`Select()`と`Clone()`を記録し、対象範囲の選択時刻をイベント列に残す。`send`の偽物は、IMEがオフのときだけ本文を更新する。

## 固定する不変条件

1. `ime_off`と入力先スレッド同期（`ime_flush`）は`select`より前にある。IMEをオフにできない場合は選択も送信もしない。
2. `select`から`input_sent`までの間にIME制御メッセージを送らない。オフ確認と通知の処理は選択前の`disable()`内で完了させる。
3. 対象範囲は`GetSelection()`から作らず、常に`DocumentRange`から構築する。範囲はIME一時オフと入力先スレッド同期の後に、確認済みの右端から左端を後方へ動かして構築する。文書先頭側から左端を作ると、本文が同じでも前のリスト項目などのアンカーを引き継ぎ、`Select()`が実際の選択に反映されない場合がある。切替前に構築した範囲も再利用しない。送信直前にもUIAで本文と選択位置を再確認し、一致しなければ送信しない。
4. 置換後の`composition`は空で、本文は期待する完成文と一致する。
5. 成功・中止のどちらでも、元のIME状態へ戻す。元からオフならオンにしない。
6. 送信失敗・IME切り替え失敗・入力先スレッド同期失敗では文字を再送しない。

## 試験

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_direct.UiaRangeTests tests.test_direct.ImeInteractionTests -v
```

- `test_ime_off_precedes_selection_and_no_ime_message_follows_it`
- `test_target_range_is_built_from_document_not_live_selection`
- `test_repeated_list_end_replacements_keep_selecting`
- `test_already_off_ime_stays_off_and_is_not_queried_after_selection`
- `test_send_failure_restores_original_ime_state_without_composition`
- `test_switch_failure_sends_nothing_and_keeps_original_state`

`tests/test_ime.py`では、入力先スレッド同期の失敗で送信前に中止すること、元からオフなら同期しないことも確認する。

`disable()`を何もしない実装に差し替えると、本文は`さんぽ`のまま、`composition`に`散歩`が残る。旧順序（選択後にIMEを切り替える）では、`select`後の`ime_query`で選択が`0:0`へ解除され、置換前に中止する。選択前に切り替えても入力先スレッド同期を省くと、保留中の通知が`Select()`時に選択を解除する。`GetSelection()`由来のRangeを使う実装では、本文が一致しても`Select()`が反映されず、選択が`500:500`のまま残る。文書先頭側から左端を作る実装でも、リスト項目の末尾で同じ状態になる。置換を繰り返してリスト項目が増えた後のテストでは、旧方式が要求`23:30`に対して実際`30:30`のまま中止し、右端基準の方式は全回成功する。確認済みの右端から左端を後方へ動かすことで、この状態を再現せずに選択できる。

## 制限

このモデルは、利用者環境で観測した「選択後のIME通知で選択が解除される」「IME有効中のUnicodeが未確定になる」という挙動を固定したもの。すべてのIME・ブラウザーの実装を再現するものではない。実IME・実ブラウザーでの確認は別途必要で、`--debug`の`時間:`と`UIA入力:`ログで、IME一時オフと状態確認、範囲構築、選択、送信、復元の順序を確認する。

