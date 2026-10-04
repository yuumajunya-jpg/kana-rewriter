# 初回GitHub公開の手順

pushは利用者自身が実行します。Gitは初期化・コミット済みで、公開準備後のローカル`main`にPython最適化版と整理済み資料を含めています。`git init`や履歴の作り直しは不要です。公開準備時点ではremoteは未登録です。

## 1. GitHubで空のリポジトリを作る

GitHubにログインし、New repositoryから名前（例: `kana-rewriter`）と公開範囲を指定します。README、.gitignore、Licenseの自動追加は選ばず、空のリポジトリとして作成してください。作成後のHTTPS URLをコピーします。

GitHubの[既存コードを追加する公式手順](https://docs.github.com/en/migrations/importing-source-code/using-the-command-line-to-import-source-code/adding-locally-hosted-code-to-github)に従う方法です。

## 2. ローカルの状態を確認する

PowerShellで:

```powershell
Set-Location C:\workfile\kana-rewriter
git switch main
git status --short
git log -1 --oneline
git remote -v
```

`git status --short`が空なら管理対象の未コミット変更はありません。`git remote -v`も最初は空です。すでにoriginを登録している場合は再度追加せずURLを確認してください。

## 3. originを登録し、自分でpushする

`YOUR_ACCOUNT`は自分のGitHubアカウントまたは組織名、リポジトリ名は作成した名前に置き換えます。

```powershell
git remote add origin https://github.com/YOUR_ACCOUNT/kana-rewriter.git
git remote -v
git push -u origin main
```

最後のコマンドが初回pushです。認証を求められたらGit Credential Manager等の案内に従ってGitHubへログインしてください。登録先URLを確認してからpushします。

GitHubのリポジトリページを再読み込みし、`main`のREADME・ソース・最終コミットが表示されることを確認してください。

## 公開されるファイルと履歴

`git push -u origin main`は`main`とその祖先コミットを送ります。Python最適化版、C++比較用ソース、テスト、資料を含みます。過去の開発も履歴に含まれますが、開発ブランチ名を個別に公開する必要はありません。

`.gitignore`で`config.toml`、`.venv`、モデル、`build`、ログ、`.env`を除外しています。公開準備時の全Git履歴でも、モデル・ローカル設定・生成物のパスと代表的な認証トークン／秘密鍵パターンは検出されませんでした。すべての種類の秘密情報が存在しないことを保証する確認ではありません。

ソースコードのライセンスはまだ設定していません。必要なら選定したLICENSEをローカルで追加・コミットしてから公開してください。モデルは別途取得し、その配布元のライセンスに従います。

## 次回以降

`main`で変更をコミットした後は`git push`で更新できます。新しい作業をブランチで行った場合は取り込む変更を確認してから`main`へマージしてpushします。
