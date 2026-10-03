# かな漢字変換モデルの選定（2026-10-03）

第一候補は**jinen-v1-xsmall / Q5_K_M**。左文脈を専用トークンで渡す小型の変換専用モデルです。公開済みGGUFは31,178,432 bytes（約31.2MB）。現在のmainにはGGUFが見当たらないため、GGUF追加コミット`6ec3b6ae261939271c6bd0b9385553ee0ae0a93c`を指定します。

公開資料に基づいて選定し、その後、使用者のWindows環境でGGUFの読み込み・直接推論が動作したと報告されています。左文脈による「歯医者／廃車」の変換も確認されました。実モデルの比較ベンチマークは未実施で、この確認だけで他のモデル版やランタイムとの互換性は保証できません。公開時のGGUFが最新のSafetensorsと同じ更新状態とも限りません。

| モデル | パラメーター／公開ファイル | 文脈 | 判断 |
| --- | --- | --- | --- |
| jinen-v1-xsmall | 約35.8M、Q5_K_Mは31.2MB | 左 | 第一候補。BPE、専用形式。公開リビジョン指定が必要 |
| jinen-v1-small | 約110M、現行BF16重みは221MB | 左 | 次の比較候補。作者の評価ではxsmallより高いAcc@1。現行mainのGGUFは確認できず |
| zenz-v3.2-small | 約95.1M、Q5_K_Mは73.9MB | 左・右 | 用途に適するが、独自トークナイザーを要求 |
| zenz-v3.2-xsmall | 約25.6M、Q5_K_Mは21MB | 左・右 | 小さい代替候補。同じランタイム制約あり |

ファイル容量は常駐メモリではありません。速度はこのPCで計測していません。jinen作者のカードではAJIMEE-BenchのNFKC適用時Acc@1がsmall=0.775、xsmall=0.720と記載されています。今回のGGUF・入力欄での実測ではなく、zenzとの直接比較にも使えません。公開GGUFと現行カードの更新状態の差にも注意が必要です。

## zenzのランタイム

zenz-v3.2-smallの公開GGUFヘッダーで、`gpt2.context_length=1024`、`tokenizer.ggml.model=gpt2`、`tokenizer.ggml.pre=gpt2-small-japanese-char`、`tokenizer.ggml.add_bos_token=false`を確認しました。

調査時の上流llama.cppには`gpt2-small-japanese-char`が見当たらず、AzooKeyKanaKanjiConverterはazooKeyのllama.cppフォークを使用しています。Hugging Faceの自動生成される利用例だけを根拠に、通常のpip版で動くと判断しないでください。

zenzを使う場合、このpre-tokenizerに対応したllama.cppを組み込むPythonランタイムが必要です。メタデータだけを汎用gpt2に書き換える方法は、文字のトークン化が変わるため採用していません。フォーク版Python wheelのビルド・互換性確認は未実装です。

入力形式は`model_format = "zenz_v3_2"`で対応済みです。対応ランタイムを用意した場合のモデル取得:

```powershell
.\.venv\Scripts\hf.exe download Miwa-Keita/zenz-v3.2-small-gguf ggml-model-Q5_K_M.gguf --local-dir models/zenz-v3.2-small
```

```toml
model_path = "models/zenz-v3.2-small/ggml-model-Q5_K_M.gguf"
model_format = "zenz_v3_2"
n_ctx = 0
```

## 入力形式と対象範囲

jinen-v1は`U+EE02`で左文脈、`U+EE00`でカタカナの読み、`U+EE01`で出力開始を区切ります。右文脈の対応は確認できないため送信しません。zenz-v3.2は左文脈の後の`U+EE07`で右文脈を渡せます。

カーソル直前の連続ひらがなを対象とし、漢字・句点・その他の非ひらがなで止めます。前後は同じ表示行から取得します。助詞判定や既存漢字の読みへの戻しはありません。対象の読みだけをカタカナにし、チャットテンプレートなしのcompletion APIで推論します。

## 一次資料

- [jinen-v1-xsmall](https://huggingface.co/togatogah/jinen-v1-xsmall): 形式、BPE、評価、CC-BY-SA 4.0。
- [GGUF追加コミット](https://huggingface.co/togatogah/jinen-v1-xsmall/commit/6ec3b6ae261939271c6bd0b9385553ee0ae0a93c): ファイル名、容量、SHA256。
- [jinen-v1-small](https://huggingface.co/togatogah/jinen-v1-small)、[現行ファイル一覧](https://huggingface.co/togatogah/jinen-v1-small/tree/main)。
- [zenz-v3.2-small](https://huggingface.co/Miwa-Keita/zenz-v3.2-small-gguf)、[zenz-v3.2-xsmall](https://huggingface.co/Miwa-Keita/zenz-v3.2-xsmall-gguf): 容量、パラメーター、Apache-2.0表記。
- [Zenzai公式ドキュメント](https://github.com/azooKey/AzooKeyKanaKanjiConverter/blob/main/Docs/zenzai.md): 専用マーカーと左右文脈。
- [Package.swift](https://github.com/azooKey/AzooKeyKanaKanjiConverter/blob/main/Package.swift)、[上流llama-vocab.cpp](https://github.com/ggml-org/llama.cpp/blob/master/src/llama-vocab.cpp): ランタイムの対応状況。

モデルの再配布時は、プログラムと別に、取得リビジョンのライセンスと表示条件を確認してください。
