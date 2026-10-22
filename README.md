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
| ID の特定と存在チェック | 指定した ID が S3 に無い・一意に決まらない場合は中止します。移動先の `checked` / `unchecked` フォルダがどちらも無い場合も中止します（ID の打ち間違い対策）。新規の移動先へ移すときだけ `--allow-new-destination` を付けます |
| Identity ID の形式チェック | `ap-northeast-1:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` の形式でなければ中止します |
| 同一 ID の拒否 | 移動元と移動先が同じなら中止します |
| 上書き禁止 | 移動先に同名の結果フォルダがあるものは対象外にします。ファイル単位でも既存ファイルがあれば中止します |
| 一覧 CSV の鮮度チェック | 一覧作成後にファイル数が変わったフォルダ・無くなったフォルダは対象外にします。1 つの CSV に複数の移動元が混在していたら中止します |
| checked / unchecked は維持 | `checked` は `checked` へ、`unchecked` は `unchecked` へのみ移動します |
| 段階的な確認 | ① 移動するフォルダを番号で選択（一覧 CSV 指定時は CSV の内容が対象）→ ② 移動先のユーザーID を再入力 → ③ 移動先 Identity ID の末尾 6 文字を入力（どちらも一致しなければ中止）→ ④ `yes` と入力 |
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

Mac / Linux:

```bash
git clone https://github.com/dnachip-lab/aws_s3_file_transfer.git
cd aws_s3_file_transfer
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. AWS の認証情報を設定する

管理者から発行されたアクセスキー（または SSO の情報）を使って、プロファイルを作成します。
ここではプロファイル名を `reportsys` とします。

```bash
# アクセスキーの場合
aws configure --profile reportsys
#   AWS Access Key ID     : （発行されたキー）
#   AWS Secret Access Key : （発行されたシークレット）
#   Default region name   : ap-northeast-1
#   Default output format : json

# SSO の場合
aws configure sso --profile reportsys
```

設定できたか確認します（アカウント ID と利用者名が表示されれば OK）:

```bash
aws sts get-caller-identity --profile reportsys
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
cp config.example.json config.json        # Windows: copy config.example.json config.json
```

`config.json` を開き、バケット名を対象環境（テスト / 本番）に合わせて編集します。

```json
{
  "bucket": "cp-reportsys-620665995161-test150400-test",
  "region": "ap-northeast-1",
  "root_prefix": "private/",
  "expected_extensions": [".pdf", ".csv"],
  "utc_offset_hours": 9,
  "log_dir": "logs"
}
```

- `utc_offset_hours`: アップロード日の表示・期間判定に使うタイムゾーン（既定: 9 = 日本時間）
- テスト用と本番用で設定を分けたい場合は `config.test.json` / `config.prod.json` のように作り、`--config config.prod.json` で切り替えます。

### 5. 動作確認

既存のユーザーID で検索し、結果が表示されればセットアップ完了です（S3 は変更しません）。

```bash
python s3_move.py search --user-id 000002 --profile reportsys
```

> 毎回 `--profile` を付けるのが面倒な場合は、環境変数 `AWS_PROFILE` を設定してもかまいません
> （PowerShell: `$env:AWS_PROFILE="reportsys"` / Mac・Linux: `export AWS_PROFILE=reportsys`）。

## 2回目以降の手順

### 1. 準備

```bash
cd aws_s3_file_transfer
.venv\Scripts\Activate.ps1                 # Mac / Linux: source .venv/bin/activate
git pull                                    # ツールを最新にする
pip install -r requirements.txt             # 更新があった場合に依存関係を反映
aws sts get-caller-identity --profile reportsys   # 認証が有効か確認 (SSO は期限切れなら aws sso login --profile reportsys)
```

`config.json` のバケット名が今回の作業対象（テスト / 本番）になっているか確認してください。

### 2. 移動作業

以下では `--profile reportsys` を省略しています（`AWS_PROFILE` を設定していない場合は各コマンドに付けてください）。

```bash
# ① 移動元・移動先の ID を確認
python s3_move.py search --user-id 000002
python s3_move.py search --user-id 000003

# ② 移動対象の一覧を作成 (必要に応じて --status / --since / --until で絞り込み)
python s3_move.py list --user-id 000002 --since 2026-10-01 --until 2026-10-31 -o list_000002.csv

# ③ list_000002.csv を Excel 等で開き、移動しない行を削除して保存

# ④ ドライラン (S3 は変更しない)。移動先のパスと件数を必ず確認
python s3_move.py move --list list_000002.csv --dst-user-id 000003 --dry-run

# ⑤ 問題なければ実行。画面の指示に従って移動先ユーザーID・Identity ID 末尾 6 文字・yes を入力
python s3_move.py move --list list_000002.csv --dst-user-id 000003 --execute

# ⑥ 結果を確認 (移動元に残っていないこと、移動先に入ったこと)
python s3_move.py list --user-id 000002
python s3_move.py list --user-id 000003
```

- 操作ログ（`logs/move_*.json`）とドライランレポート（`logs/dryrun_*.json`）は作業記録として保管してください。
- 途中でエラーが出た場合、失敗したフォルダの移動元は削除されていません。操作ログで状況を確認し、原因を取り除いてから ② からやり直してください（移動済みのフォルダは移動元に無いため、再度対象になることはありません）。

## 使い方

### search: 対象フォルダの検索

```bash
python s3_move.py search --user-id 000002
python s3_move.py search --identity-id ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da
```

```
1 件見つかりました:
  ユーザーID=000002  Identity ID=ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da  (移動対象フォルダ checked: 1, unchecked: 1)
    private/ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da/000002/
```

### list: 移動対象フォルダの一覧（期間指定・CSV 保存）

```bash
# 2026年10月にアップロードされた checked の結果フォルダを一覧表示し、CSV に保存
python s3_move.py list --user-id 000002 --status checked \
                       --since 2026-10-01 --until 2026-10-31 -o list.csv
```

```
対象     : ユーザーID=000002 / Identity ID=ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da
フォルダ : checked
期間     : 最終アップロード日が 2026-10-01 〜 2026-10-31

    No  状態      結果フォルダ          PDF  CSV  他    サイズ  最初のアップロード    最終アップロード
     1. checked   SAT0202301              1    1   0     2.3KB  2026-10-22 11:02:23  2026-10-22 11:02:23

1 フォルダ / 2 ファイル / 2.3KB
一覧を保存しました: list.csv
```

- アップロード日はフォルダ内ファイルの S3 最終更新日時です。期間判定は既定でフォルダ内の **最終** アップロード日を使います（`--date-basis first` で最初のアップロード日）。
- 保存した CSV（Excel で開けます）の列: `identity_id, user_id, status, folder, pdf_count, csv_count, other_count, file_count, total_size, first_uploaded, last_uploaded`

### move: 移動

**一覧 CSV で一括移動**（不要な行を削除した CSV を使う）

```bash
python s3_move.py move --list list.csv --dst-user-id 000003 --dry-run  # ドライラン
python s3_move.py move --list list.csv --dst-user-id 000003 --execute  # 実行
```

**期間で絞り込んで移動**

```bash
python s3_move.py move --src-user-id 000002 --dst-user-id 000003 \
                       --since 2026-10-01 --until 2026-10-31 --execute
```

**一覧から番号で選んで移動**

```bash
python s3_move.py move --src-user-id 000002 --dst-user-id 000003 --execute
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
確認のため、移動先の Identity ID の末尾 6 文字を入力してください: abcdef
2 フォルダを移動します。実行してよろしいですか? (yes/no): yes
移動中 (1/2): [checked] SAT0202301 ... 完了
移動中 (2/2): [unchecked] SAT0202303 ... 完了
操作ログ: logs/move_20261022_110223.json
2 フォルダの移動が完了しました。
```

### ID の指定について

- ユーザーID と Identity ID は **どちらか一方だけ指定すれば、もう一方を S3 から特定** します。一意に決まらない場合は中止するので、両方指定してください。
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
| `--folder NAME` | | | ✓ | 対象の結果フォルダ名（複数指定可） |
| `--list FILE` | | | ✓ | 一覧 CSV のフォルダを一括移動（`--folder` / `--since` / `--until` / `--status` とは併用不可） |
| `--dry-run` | | | ✓ | ドライラン（既定）。S3 を変更せず移動予定ファイルの一覧とレポートを出力 |
| `--execute` | | | ✓ | 実際に移動する（`--dry-run` とは併用不可） |
| `--allow-new-destination` | | | ✓ | 移動先のフォルダが存在しなくても続行 |

### 元に戻すには

移動元と移動先を入れ替えて同じ手順で実行してください。移動した内容は操作ログで確認できます。

## テスト

```bash
pip install -r requirements-dev.txt
pytest
```

S3 は [moto](https://github.com/getmoto/moto) でモックしているため、AWS には接続しません。
