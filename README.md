# aws_s3_file_transfer

S3 上の結果フォルダ（PDF, CSV）を、あるユーザーID / Identity ID から別のユーザーID / Identity ID の
同じ `checked` / `unchecked` フォルダへ **確認しながら** 移動するツールです。

S3 の構成:

```
{バケット}/private/{Identity ID}/{ユーザーID}/{checked|unchecked}/{結果フォルダ}/*.pdf, *.csv

例: private/ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da/000002/checked/SAT0202301/
```

## 機能

| コマンド | 内容 |
| --- | --- |
| `search` | Identity ID またはユーザーID で対象フォルダを検索する |
| `list` | 移動対象となる結果フォルダ（PDF / CSV を含むもの）の一覧を、ファイル数・サイズ・アップロード日付きで表示する。アップロード日の期間で絞り込み、CSV に保存できる |
| `move` | 結果フォルダを移動する。一覧から番号で選ぶ・期間で絞り込む・`list` で保存した CSV で一括移動する、のいずれかで対象を決める |

基本の流れ:

```
search で ID を確認 → list で一覧 CSV を作成 → CSV から不要な行を削除 → move --list --dry-run → move --list --execute
```

## 誤移動を防ぐ仕組み

| 仕組み | 内容 |
| --- | --- |
| ドライラン | `--dry-run`（または `--execute` を付けない場合）は S3 を変更せず、移動されるファイルを 1 件ずつ「移動元 → 移動先」で表示し、レポート `logs/dryrun_*.json` を保存します |
| ID の特定と存在チェック | 指定した ID が S3 に無い・一意に決まらない場合は中止します |
| 移動先フォルダが無い場合の確認 | 移動先の `checked` / `unchecked` フォルダがどちらも無い場合は警告を表示し、その Identity ID 配下の既存ユーザーID（打ち間違いの確認用）を示したうえで、新規作成するか `yes` / `no` で確認します。作成は最終確認がすべて済んでから行います（ドライランでは作成しません） |
| Identity ID の形式チェック | `ap-northeast-1:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` の形式でなければ中止します |
| 同一 ID の拒否 | 移動元と移動先が同じなら中止します |
| 上書き禁止 | 移動先に同名の結果フォルダがあるものは対象外にします。ファイル単位でも既存ファイルがあれば中止します |
| 一覧 CSV の鮮度チェック | 一覧作成後にファイル数が変わったフォルダ・無くなったフォルダは対象外にします。1 つの CSV に複数の移動元が混在していたら中止します |
| checked / unchecked は維持 | `checked` は `checked` へ、`unchecked` は `unchecked` へのみ移動します |
| 段階的な確認 | ① 移動するフォルダを番号で選択（一覧 CSV 指定時は CSV の内容が対象）→ ② 移動先のユーザーID を再入力 → ③ 移動先の Identity ID を全体で入力（どちらも一致しなければ中止）→ ④ `yes` と入力 |
| コピー → 検証 → 削除 | コピー後にサイズを照合し、フォルダ内の全ファイルが検証できてから移動元を削除します。エラー時はその時点で停止し、失敗したフォルダの移動元は残ります |
| 操作ログ | コピー・削除した全ファイルを `logs/move_YYYYmmdd_HHMMSS.json` に記録します |
| 想定外ファイルの警告 | PDF / CSV 以外のファイルを含むフォルダには注意を表示します（PDF / CSV を 1 つも含まないフォルダは移動対象外） |

## 初回セットアップ手順

初めて使う PC で 1 回だけ行います。

### 1. 必要なソフトをインストール

| ソフト | 確認コマンド | 入手先 |
| --- | --- | --- |
| Python 3.9 以上 | `python --version`（Mac / Linux は `python3 --version`） | https://www.python.org/downloads/ （Windows はインストール時に「Add python.exe to PATH」にチェック） |
| Git | `git --version` | https://git-scm.com/downloads |
| AWS CLI v2 | `aws --version` | https://docs.aws.amazon.com/ja_jp/cli/latest/userguide/getting-started-install.html |

### 2. ツールを取得して Python の仮想環境を作る

> 使っている端末に合わせてどちらか一方を実行します。
> プロンプトが `ユーザー名@PC名:~$` の形なら **WSL / Linux / Mac**、`PS C:\...>` の形なら **Windows PowerShell** です。

WSL / Linux / Mac（bash）:

```bash
git clone https://github.com/dnachip-lab/aws_s3_file_transfer.git
cd aws_s3_file_transfer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> WSL / Ubuntu で `python3 -m venv` が失敗する場合は、先に `sudo apt install python3-venv` を実行してください。

Windows（PowerShell）:

```powershell
git clone https://github.com/dnachip-lab/aws_s3_file_transfer.git
cd aws_s3_file_transfer
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> `Activate.ps1` 実行時に「スクリプトの実行が無効」と出る場合は、一度だけ
> `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` を実行してください。

### 3. AWS の認証情報を設定する

管理者から発行されたアクセスキー（または SSO の情報）を使って、プロファイルを作成します。
テスト環境用のプロファイル名は `dcr_iijima_dev` です。

```bash
# アクセスキーの場合
aws configure --profile dcr_iijima_dev
#   AWS Access Key ID     : （発行されたキー）
#   AWS Secret Access Key : （発行されたシークレット）
#   Default region name   : ap-northeast-1
#   Default output format : json

# SSO の場合
aws configure sso --profile dcr_iijima_dev
```

設定できたか確認します（アカウント ID と利用者名が表示されれば OK）:

```bash
aws sts get-caller-identity --profile dcr_iijima_dev
```

使うユーザー / ロールには、対象バケットに対する次の権限が必要です（IAM ポリシーの例）:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::cp-reportsys-620665995161-test150400-test",
      "Condition": {"StringLike": {"s3:prefix": ["private/*", "private/"]}}
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
      "Resource": "arn:aws:s3:::cp-reportsys-620665995161-test150400-test/private/*"
    }
  ]
}
```

### 4. 設定ファイルを作る

```bash
cp config.example.json config.json        # Windows PowerShell の場合: copy config.example.json config.json
```

`config.json` を開き、バケット名を対象環境（テスト / 本番）に合わせて編集します。

```json
{
  "bucket": "cp-reportsys-620665995161-test150400-test",
  "region": "ap-northeast-1",
  "profile": "dcr_iijima_dev",
  "root_prefix": "private/",
  "expected_extensions": [".pdf", ".csv"],
  "utc_offset_hours": 9,
  "log_dir": "logs"
}
```

- `profile`: 使用する AWS プロファイル名（テスト環境: `dcr_iijima_dev`）
- `utc_offset_hours`: アップロード日の表示・期間判定に使うタイムゾーン（既定: 9 = 日本時間）
- テスト用と本番用で設定を分けたい場合は `config.test.json` / `config.prod.json` のように作り、各コマンドの `--config config.prod.json` で切り替えます。

### 5. 動作確認

既存のユーザーID で検索し、結果が表示されればセットアップ完了です（S3 は変更しません）。

```bash
python s3_move.py search --config config.json --user-id 000002
```

> `--config` で使用する設定ファイルを指定します。`--config` はサブコマンド（`search` / `list` / `move`）の **後ろ** に書きます。
> 本手順書のコマンド例ではすべて `--config config.json` を明記しています（省略した場合も `config.json` が使われます）。
> 本番用など別の設定ファイルを使うときは `--config config.prod.json` のように指定してください。
>
> `s3_move.py` は設定ファイルの `profile`（`dcr_iijima_dev`）を使うので、`--profile` を付ける必要はありません。
> 一時的に別のプロファイルを使う場合だけ `--profile プロファイル名` を付けます。

## 2回目以降の手順

### 1. 準備

```bash
cd ~/aws_s3_file_transfer
source .venv/bin/activate                   # Windows PowerShell の場合: .venv\Scripts\Activate.ps1
git pull                                    # ツールを最新にする
pip install -r requirements.txt             # 更新があった場合に依存関係を反映
aws sts get-caller-identity --profile dcr_iijima_dev   # 認証が有効か確認 (SSO は期限切れなら aws sso login --profile dcr_iijima_dev)
```

`config.json` のバケット名が今回の作業対象（テスト / 本番）になっているか確認してください。

### 2. 移動作業

各コマンドの `--config` には今回の作業対象（テスト / 本番）の設定ファイルを指定します。`s3_move.py` は設定ファイルの `profile` を使うので、`--profile` は不要です。

```bash
# ① 移動元・移動先の ID を確認
python s3_move.py search --config config.json --user-id 000002
python s3_move.py search --config config.json --user-id 000003

# ② 移動元の結果フォルダを全件表示し、アップロード日を確認 (S3 は変更しない)
#    一覧の下に「最終アップロード日の範囲」と「月別フォルダ数」が出るので、移動する期間を決める
python s3_move.py list --config config.json --user-id 000002 --sort last

# ③ 移動するフォルダを絞り込み、一覧を CSV に保存 (S3 は変更しない)
#    絞り込み条件は組み合わせ可能 (下の「絞り込み条件」を参照)
#    例1: 期間で絞り込む
python s3_move.py list --config config.json --user-id 000002 --since 2026-10-01 --until 2026-10-31 -o list_000002.csv
#    例2: フォルダ名を指定する
python s3_move.py list --config config.json --user-id 000002 --folder SAT0202301,SAT0202303 -o list_000002.csv
#    例3: フォルダ名のパターン + checked のみ
python s3_move.py list --config config.json --user-id 000002 --name 'SAT2026*' --status checked -o list_000002.csv

# ④ list_000002.csv を Excel 等で開き、移動しない行を削除して保存

# ⑤ ドライラン (S3 は変更しない)。移動先のパスと件数を必ず確認
python s3_move.py move --config config.json --list list_000002.csv --dst-user-id 000003 --dry-run

# ⑥ 問題なければ実行。画面の指示に従って移動先ユーザーID・Identity ID（全体）・yes を入力
python s3_move.py move --config config.json --list list_000002.csv --dst-user-id 000003 --execute

# ⑦ 結果を確認 (移動元に残っていないこと、移動先に入ったこと)
python s3_move.py list --config config.json --user-id 000002
python s3_move.py list --config config.json --user-id 000003
```

- 期間で絞り込まず全件を対象にする場合は、③ で `--since` / `--until` を付けずに `-o` だけ指定します。
- 操作ログ（`logs/move_*.json`）とドライランレポート（`logs/dryrun_*.json`）は作業記録として保管してください。
- 途中でエラーが出た場合、失敗したフォルダの移動元は削除されていません。操作ログで状況を確認し、原因を取り除いてから ③ からやり直してください（移動済みのフォルダは移動元に無いため、再度対象になることはありません）。

## トラブルシューティング

AWS のエラーが出た場合、ツールは原因と対処を表示します。まず次のコマンドで認証情報を確認してください。

```bash
aws sts get-caller-identity --profile dcr_iijima_dev
aws s3 ls s3://cp-reportsys-620665995161-test150400-test/private/ --profile dcr_iijima_dev
```

| エラー | 原因 | 対処 |
| --- | --- | --- |
| `SignatureDoesNotMatch` | シークレットアクセスキーがアクセスキーID と一致しない | `aws configure --profile dcr_iijima_dev` でキーを入力し直す（コピー時の前後の空白・欠けに注意）。`~/.aws/credentials` の `[dcr_iijima_dev]` が重複していないか確認 |
| `InvalidAccessKeyId` | アクセスキーID が無効・削除済み | 管理者に有効なキーを発行してもらい、`aws configure` で設定し直す |
| `ExpiredToken` / SSO のログインが必要 | 一時認証情報・SSO の有効期限切れ | `aws sso login --profile dcr_iijima_dev` |
| `RequestTimeTooSkewed` | PC の時刻がずれている | PC の時刻を合わせる。WSL の場合は `sudo hwclock -s`、または PowerShell で `wsl --shutdown` して WSL を再起動 |
| `AccessDenied` | 権限不足 | 初回セットアップ手順 3 の IAM ポリシーが付いているか管理者に確認 |
| `NoSuchBucket` | バケット名・アカウントの誤り | `config.json` の `bucket` と、使っているプロファイルのアカウントを確認 |
| プロファイルが見つからない | プロファイル未作成・名前の誤り | `aws configure --profile dcr_iijima_dev` で作成。`config.json` の `profile` を確認 |

`~/.aws/credentials` の正しい形（`[dcr_iijima_dev]` は 1 回だけ、`=` の前後以外に空白を入れない）:

```ini
[dcr_iijima_dev]
aws_access_key_id = AKIAXXXXXXXXXXXXXXXX
aws_secret_access_key = xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## 使い方

### search: 対象フォルダの検索

```bash
python s3_move.py search --config config.json --user-id 000002
python s3_move.py search --config config.json --identity-id ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da
```

```
1 件見つかりました:
  ユーザーID=000002  Identity ID=ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da  (移動対象フォルダ checked: 1, unchecked: 1)
    private/ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da/000002/
```

### list: 移動対象フォルダの一覧（期間・フォルダ名で絞り込み、CSV 保存）

```bash
# 2026年10月にアップロードされた checked の結果フォルダを一覧表示し、CSV に保存
python s3_move.py list --config config.json --user-id 000002 --status checked \
                       --since 2026-10-01 --until 2026-10-31 -o list.csv
```

```
対象     : ユーザーID=000002 / Identity ID=ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da
フォルダ : checked
期間     : 最終アップロード日が 2026-10-01 〜 2026-10-31

    No  状態      結果フォルダ          PDF  CSV  他    サイズ  最初のアップロード    最終アップロード
     1. checked   SAT0202301              1    1   0     2.3KB  2026-10-22 11:02:23  2026-10-22 11:02:23

1 フォルダ / 2 ファイル / 2.3KB
最終アップロード日の範囲: 2026-10-22 〜 2026-10-22
月別フォルダ数 (最終アップロード日): 2026-10: 1
一覧を保存しました: list.csv
```

#### 絞り込み条件

| オプション | 内容 | 例 |
| --- | --- | --- |
| `--status` | `checked` / `unchecked` / `both`（既定） | `--status checked` |
| `--folder` | フォルダ名（完全一致）。複数指定・カンマ区切り可 | `--folder SAT0202301,SAT0202303` |
| `--name` | フォルダ名のパターン。`*` は任意の文字列、`?` は任意の 1 文字、`[12]` は 1 か 2。大文字小文字は区別しない。複数指定可 | `--name 'SAT2026*'` |
| `--since` / `--until` | アップロード日の期間（両端を含む） | `--since 2026-10-01 --until 2026-10-31` |

- 種類の違う条件（`--status`・フォルダ名・期間）は **すべて満たす** ものが対象です。
- `--folder` と `--name` を両方指定した場合は、**どちらかに一致** するものが対象です。
- `--name` のパターンは `'SAT2026*'` のように **引用符で囲んで** ください（囲まないと bash が `*` を先に展開してしまうことがあります）。
- `--folder` で指定したフォルダが見つからない場合は注意を表示します。
- `move` でも同じ条件で絞り込めます（`--list` との併用は不可）。

- アップロード日はフォルダ内ファイルの S3 最終更新日時です。期間判定は既定でフォルダ内の **最終** アップロード日を使います（`--date-basis first` で最初のアップロード日）。
- 保存した CSV（Excel で開けます）の列: `identity_id, user_id, status, folder, pdf_count, csv_count, other_count, file_count, total_size, first_uploaded, last_uploaded`

### move: 移動

**一覧 CSV で一括移動**（不要な行を削除した CSV を使う）

```bash
python s3_move.py move --config config.json --list list.csv --dst-user-id 000003 --dry-run  # ドライラン
python s3_move.py move --config config.json --list list.csv --dst-user-id 000003 --execute  # 実行
```

**期間で絞り込んで移動**

```bash
python s3_move.py move --config config.json --src-user-id 000002 --dst-user-id 000003 \
                       --since 2026-10-01 --until 2026-10-31 --execute
```

**一覧から番号で選んで移動**

```bash
python s3_move.py move --config config.json --src-user-id 000002 --dst-user-id 000003 --execute
```

ドライランの出力（S3 は変更しません）:

```
移動予定のファイル (ドライラン):
  [checked] SAT0202301
    private/ap-northeast-1:05efdd85-.../000002/checked/SAT0202301/a.csv
      → private/ap-northeast-1:11111111-.../000003/checked/SAT0202301/a.csv  (1.2KB, アップロード 2026-10-22 11:02:23)
    private/ap-northeast-1:05efdd85-.../000002/checked/SAT0202301/a.pdf
      → private/ap-northeast-1:11111111-.../000003/checked/SAT0202301/a.pdf  (1.2KB, アップロード 2026-10-22 11:02:23)

移動予定: 1 フォルダ / 2 ファイル / 2.3KB
対象外  : 1 フォルダ (移動不可の理由は上の一覧を参照)
ドライランレポート: logs/dryrun_20261022_110000.json
```

実行時の確認:

```
======================================================================
以下の内容で移動します。移動先に間違いがないか必ず確認してください。
  移動元: ユーザーID=000002 / Identity ID=ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da
  移動先: ユーザーID=000003 / Identity ID=ap-northeast-1:11111111-2222-3333-4444-555555abcdef
  件数  : 2 フォルダ / 3 ファイル / 3.5KB
======================================================================
    1. [checked] SAT0202301  (2件 2.3KB / .csv:1, .pdf:1 / 最終アップロード 2026-10-22 11:02:23)
         移動元: private/ap-northeast-1:05efdd85-.../000002/checked/SAT0202301/
         移動先: private/ap-northeast-1:11111111-.../000003/checked/SAT0202301/
    ...

確認のため、移動先のユーザーID を入力してください: 000003
確認のため、移動先の Identity ID を全体で入力してください (例: ap-northeast-1:xxxxxxxx-...): ap-northeast-1:11111111-2222-3333-4444-555555abcdef
2 フォルダを移動します。実行してよろしいですか? (yes/no): yes
移動中 (1/2): [checked] SAT0202301 ... 完了
移動中 (2/2): [unchecked] SAT0202303 ... 完了
操作ログ: logs/move_20261022_110223.json
2 フォルダの移動が完了しました。
```

### 移動先のフォルダが無い場合

移動先ユーザーの `checked` / `unchecked` フォルダがまだ無い場合は、次のように確認します。

```
※警告: 移動先の checked / unchecked フォルダが見つかりません。
  移動先: private/ap-northeast-1:1e814f9d-.../test2/
  この Identity ID 配下の既存ユーザーID: test
  ID の打ち間違いがないか必ず確認してください。
移動先の checked / unchecked フォルダを新規作成して移動しますか? (yes/no): yes
```

- `yes` と答えると、通常どおりフォルダ選択・ユーザーID / Identity ID の確認・`yes` の最終確認に進み、最終確認が済んでから `checked/` と `unchecked/` を作成して移動します。作成したフォルダは操作ログに記録されます。
- 途中で中止した場合、フォルダは作成されません。
- ドライランでは作成せず、警告だけを表示します。

### ID の指定について

- ユーザーID と Identity ID は **どちらか一方だけ指定すれば、もう一方を S3 から特定** します。一意に決まらない場合は中止するので、両方指定してください。
- 同じユーザーID が複数の Identity ID 配下にある場合（例: テスト用の `test`）は、候補ごとの移動対象フォルダ数・最終アップロード日時と、指定すべきオプションを表示して中止します。

  ```
  エラー: 移動先のユーザーID test に該当するIdentity ID が 2 件あります。どれを使うか --dst-identity-id で指定してください。

    1. Identity ID=ap-northeast-1:1e814f9d-d94a-4c4f-b33e-27855a8c0cc5
       移動対象フォルダ checked: 3, unchecked: 0 / 最終アップロード 2025-06-10 10:00:00
       指定方法: --dst-identity-id ap-northeast-1:1e814f9d-d94a-4c4f-b33e-27855a8c0cc5
    2. Identity ID=ap-northeast-1:9082937f-99cd-cedf-7b3b-995dfe80cc79
       ...
  ```

  候補は `search --user-id test` でも確認できます。どちらが正しいか分からない場合は、アプリ側（Cognito）で対象ユーザーの Identity ID を確認してください。
- ユーザーID だけを指定した場合は `private/` 配下の全 Identity ID を走査して探すため、Identity ID の数が多いと時間がかかります。

### オプション

共通: `--config` / `--bucket` / `--profile` / `--region`

| オプション | search | list | move | 説明 |
| --- | :-: | :-: | :-: | --- |
| `--user-id` / `--identity-id` | ✓ | ✓ | | 対象のユーザーID / Identity ID（少なくとも一方） |
| `--src-user-id` / `--src-identity-id` | | | ✓ | 移動元（`--list` 指定時は不要） |
| `--dst-user-id` / `--dst-identity-id` | | | ✓ | 移動先（少なくとも一方） |
| `--status` | | ✓ | ✓ | `checked` / `unchecked` / `both`（既定: `both`） |
| `--since` / `--until` | | ✓ | ✓ | アップロード日の期間（YYYY-MM-DD、両端を含む） |
| `--date-basis` | | ✓ | ✓ | 期間判定に使う日付 `last`（最終、既定）/ `first`（最初） |
| `-o`, `--output` | | ✓ | | 一覧を CSV に保存 |
| `--sort` | | ✓ | | 並び順 `name`（既定）/ `last`（最終アップロード日順）/ `first`（最初のアップロード日順） |
| `--folder NAME` | | ✓ | ✓ | フォルダ名で絞り込み（完全一致。複数指定・カンマ区切り可） |
| `--name PATTERN` | | ✓ | ✓ | フォルダ名をワイルドカードで絞り込み（例: `'SAT2026*'`） |
| `--list FILE` | | | ✓ | 一覧 CSV のフォルダを一括移動（`--folder` / `--name` / `--since` / `--until` / `--status` とは併用不可） |
| `--dry-run` | | | ✓ | ドライラン（既定）。S3 を変更せず移動予定ファイルの一覧とレポートを出力 |
| `--execute` | | | ✓ | 実際に移動する（`--dry-run` とは併用不可） |
| `--allow-new-destination` | | | ✓ | 移動先の `checked` / `unchecked` フォルダが無い場合に、作成の確認を省略して作成する（通常は使わない） |

### 元に戻すには

移動元と移動先を入れ替えて同じ手順で実行してください。移動した内容は操作ログで確認できます。

## テスト

```bash
pip install -r requirements-dev.txt
pytest
```

S3 は [moto](https://github.com/getmoto/moto) でモックしているため、AWS には接続しません。
