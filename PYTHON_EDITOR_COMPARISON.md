# Python編集処理の最適化と比較

C++への移行はここで止め、Pythonの編集ワーカーを最適化した。AIの推論経路は変更していない。Python版はC++編集ワーカーの実行ファイルを必要としない。Windows APIとUIAは従来どおりctypes/comtypesから利用する。

## 変更内容

- `GetText`が正常に返った範囲では、空範囲を判定する追加のCOM呼び出しを省略。null BSTRの場合は従来の確認を残す。
- 選択範囲が空かどうかの判定を再利用。
- 置換前・選択後・IME切り替え後は、既知の期待本文と位置に一致するかを専用処理で確認。全文の前後照合、選択本文、位置、入力先の確認を維持。
- 入力反映待ちではWin32の入力先を毎回確認し、期待本文に一致した時点でUIAの入力先と位置を確認。成功した本文照会を最終確認に再利用。
- Chromiumの通常のEdit入力欄で、文字方向・本文・句読点数が条件に合う場合は、Unicode入力と句読点を越える右矢印を一度のSendInputで送る。絵文字・結合文字・方向不明などはUIAでカーソルを復元。
- 短い待機はスレッド専用の高分解能waitable timerを使用。利用できないOS環境は`time.sleep`へ戻す。OS全体のタイマー設定は変更しない。

## 読み取りの比較結果（2026-10-04）

同じ非表示RichEdit・同じHWNDで各20回測定。UIAは旧Python→最適化Python→C++→C++→最適化Python→旧Pythonの順で前後に分割し、Pythonは各区間で2回ウォームアップした。入力欄のメッセージ処理には試験用の5ms待機がある。フォーカス確認・文字入力・AI・ワーカー通信を含まない。

| 編集経路 | 中央値 | p95（nearest rank） |
|---|---:|---:|
| 旧Python UIA | 226.415ms | 239.314ms |
| 最適化Python UIA | 178.489ms | 191.584ms |
| C++ UIA | 167.487ms | 178.594ms |
| Python Win32 | 64.883ms | 65.315ms |
| C++ Win32 | 64.744ms | 66.182ms |

Python UIAは旧版より約21.2%短縮した。最適化PythonとC++の差は中央値11.0ms。この試験用入力欄の結果から、ブラウザーでの編集全体の速度を推定することはできない。

旧版は`a5897e075d016c321f7b482857f061ce49ae2f96`の`direct_uia.py`。ローカルの測定結果は`build/python-editor-read-comparison.json`、旧版の保存先は`build/python-baseline/direct_uia.py`。`build`はGit管理外。再測定:

```cmd
.\.venv\Scripts\python.exe -B scripts\benchmark_native_reads.py --samples 20 --baseline-python build\python-baseline\direct_uia.py --native-exe build\native\kana-editor-worker-expected.exe --output build\python-editor-read-comparison.json
```

旧版を保存していない環境では、次のコマンドで復元できる。C++実行ファイルの構築は[NATIVE_EDITOR.md](NATIVE_EDITOR.md)を参照。

```cmd
.\.venv\Scripts\python.exe -c "from pathlib import Path; import subprocess; p=Path('build/python-baseline/direct_uia.py'); p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(subprocess.check_output(['git','show','a5897e075d016c321f7b482857f061ce49ae2f96:kana_rewriter/direct_uia.py']))"
```

## 実ブラウザーでのPython手動試行（2026-10-04）

同じ利用者環境でAIあり・`--editor-worker python --timings`を起動し、3回の置換成功ログを確認した。入力欄での見た目・復元位置は利用者が確認する手動試行であり、自動試験とは別。

| 回 | 取得 | AI | 適用 | AI以外 | 合計 |
|---|---:|---:|---:|---:|---:|
| 初回 | 47.1ms | 121.1ms | 96.1ms | 144.5ms | 265.6ms |
| 2回目 | 12.1ms | 69.6ms | 100.1ms | 112.5ms | 182.1ms |
| 3回目 | 13.1ms | 69.0ms | 106.3ms | 119.6ms | 188.7ms |

全試行でキー解放待ちは0ms、句読点後の一括カーソル移動が使われた。入力反映確認は60.2 / 68.7 / 74.3msだった。初回を除くAI以外は平均116.1ms。以前のC++ログと同程度だが、試行数・入力・測定順を揃えた比較ではないため、差の有意性や優劣は判断しない。通常利用はPython版とし、C++移行はいったん終了した。

## 実際のブラウザーでAIなし比較

通常のホットキー・取得・ワーカー通信・置換・反映確認を通す。AIモデルは読み込まず、指定した対象に完全一致した場合だけ固定結果に置換する。空の対象・結果は不可。異なる対象は送信前に中止する。以下はcmd用:

```cmd
.\.venv\Scripts\python.exe -m kana_rewriter --config config.toml --editor-worker python --benchmark-source さんぽ --benchmark-result 散歩 --timings > build\python-edit.log 2>&1
```

いつもの入力欄に`さんぽ。`を入力確定し、句点の後にカーソルを置いてCtrl+Enter。毎回同じ本文に戻して10〜20回実行し、端末のCtrl+Cで終了する。続いて同条件でC++版:

```cmd
.\.venv\Scripts\python.exe -m kana_rewriter --config config.toml --editor-worker native --benchmark-source さんぽ --benchmark-result 散歩 --timings > build\native-edit.log 2>&1
.\.venv\Scripts\python.exe scripts\summarize_timings.py build\python-edit.log build\native-edit.log
```

`--editor-worker`は今回の起動だけ設定を上書きする。2つを同時に起動するとホットキーが競合するため、先に終了して切り替える。初回はウォームアップとして別に見る。順番による影響を減らすにはPython→C++→C++→Pythonの4区間で測る。

`時間集計`の`AI`欄は、このモードでは固定結果を返す処理時間であり推論時間ではない。比較は`AI以外`・`取得`・`適用`の中央値/p95と中止件数を見る。キーを押したままにすると解放待ちも含まれるので、同じように短く押して離す。固定結果はすぐ返るため、通常のAIあり実行より解放待ちの影響が大きくなる。キー解放待ちの工程ログも確認する。ログ出力は測定終了まで本文の反映状況を表示しないため、入力欄を見て成功とカーソル位置を確認する。

通常のAIあり比較は、比較用の2つの引数を外して同じモデル・設定で測定する。ここでの自動試験は非表示の入力欄に限り、ユーザーのブラウザーへ自動入力していない。
