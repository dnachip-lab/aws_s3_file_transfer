# aws_s3_file_transfer

S3 上で、あるユーザーID / Identity ID の `checked` / `unchecked` フォルダにある結果フォルダ（PDF, CSV）を、
別のユーザーID / Identity ID の同じ `checked` / `unchecked` フォルダへ **確認しながら** 移動するツールです。

S3 の構成:

```
{バケット}/private/{Identity ID}/{ユーザーID}/{checked|unchecked}/{結果フォルダ}/*.pdf, *.csv

例: private/ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da/000002/checked/SAT0202301/
```

移動のイメージ:

```
private/ap-northeast-1:05efdd85-.../000002/checked/SAT0202301/report.pdf
  → private/ap-northeast-1:11111111-.../000003/checked/SAT0202301/report.pdf
```

ユーザーID と Identity ID は、移動元・移動先それぞれ **どちらか一方だけ指定すれば、もう一方を S3 から特定** します。
一意に決まらない場合（1 つの Identity ID 配下に複数のユーザーID がある等）は中止するので、両方指定してください。

## 誤移動を防ぐ仕組み

| 仕組み | 内容 |
| --- | --- |
| 既定はドライラン | `--execute` を付けない限り、移動計画を表示するだけで S3 は変更しません |
| ID の特定と存在チェック | 指定した ID が S3 に無い・一意に決まらない場合は中止します。移動先の `checked` / `unchecked` フォルダがどちらも無い場合も中止します（ID の打ち間違い対策）。新規の移動先へ移すときだけ `--allow-new-destination` を付けます |
| Identity ID の形式チェック | `ap-northeast-1:xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx` の形式でなければ中止します |
| 同一 ID の拒否 | 移動元と移動先が同じなら中止します |
| 上書き禁止 | 移動先に同名の結果フォルダがあるものは選択できません。ファイル単位でも既存ファイルがあれば中止します |
| checked / unchecked は維持 | `checked` は `checked` へ、`unchecked` は `unchecked` へのみ移動します |
| 段階的な確認 | ① 移動するフォルダを番号で選択 → ② 移動先のユーザーID を再入力 → ③ 移動先 Identity ID の末尾 6 文字を入力（どちらも一致しなければ中止）→ ④ `yes` と入力 |
| コピー → 検証 → 削除 | コピー後にサイズを照合し、フォルダ内の全ファイルが検証できてから移動元を削除します。エラー時はその時点で停止し、失敗したフォルダの移動元は残ります |
| 操作ログ | コピー・削除した全ファイルを `logs/move_YYYYmmdd_HHMMSS.json` に記録します |
| 想定外ファイルの警告 | PDF / CSV 以外のファイルを含むフォルダには注意を表示します |

## セットアップ

```bash
pip install -r requirements.txt
cp config.example.json config.json   # バケット名・フォルダ構成を実際の環境に合わせて編集
```

```json
{
  "bucket": "cp-reportsys-620665995161-test150400-test",
  "region": "ap-northeast-1",
  "root_prefix": "private/",
  "expected_extensions": [".pdf", ".csv"],
  "log_dir": "logs"
}
```

AWS 認証情報は通常の boto3 の方法（`aws configure`、環境変数、`--profile` など）で設定します。
必要な権限: `s3:ListBucket`, `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`。

## 使い方

### 1. まずドライランで確認

```bash
# ユーザーID で指定（Identity ID は自動で特定）
python s3_move.py --src-user-id 000002 --dst-user-id 000003
```

```
バケット : cp-reportsys-620665995161-test150400-test
モード   : ドライラン (変更しません)
移動元のユーザーID 000002 の Identity ID を検索しています...
移動元の Identity ID を特定しました: ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da
移動先のユーザーID 000003 の Identity ID を検索しています...
移動先の Identity ID を特定しました: ap-northeast-1:11111111-2222-3333-4444-555555abcdef
移動元   : ユーザーID=000002 / Identity ID=ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da
移動先   : ユーザーID=000003 / Identity ID=ap-northeast-1:11111111-2222-3333-4444-555555abcdef
対象     : checked, unchecked

移動元の結果フォルダ:
    1. [checked] SAT0202301  (2件 1.2MB / .csv:1, .pdf:1)
         移動元: private/ap-northeast-1:05efdd85-.../000002/checked/SAT0202301/
         移動先: private/ap-northeast-1:11111111-.../000003/checked/SAT0202301/
    2. [checked] SAT0202302  (1件 800.0KB / .pdf:1)  [移動不可: 移動先に同名フォルダあり]
         ...
```

### 2. 実行

```bash
python s3_move.py --src-user-id 000002 --dst-user-id 000003 --execute
```

```
移動するフォルダの番号を入力してください (例: 1,3,5-7 / all / q で中止): 1
...（最終確認の一覧）...
確認のため、移動先のユーザーID を入力してください: 000003
確認のため、移動先の Identity ID の末尾 6 文字を入力してください: abcdef
1 フォルダを移動します。実行してよろしいですか? (yes/no): yes
移動中: [checked] SAT0202301 ... 完了
操作ログ: logs/move_20261022_103000.json
```

### ID の指定方法

```bash
# Identity ID で指定（配下のユーザーID が 1 つなら自動で特定）
python s3_move.py --src-identity-id ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da \
                  --dst-identity-id ap-northeast-1:11111111-2222-3333-4444-555555abcdef

# 両方指定（最も確実。一意に決まらない場合はこちら）
python s3_move.py --src-identity-id ap-northeast-1:05efdd85-... --src-user-id 000002 \
                  --dst-identity-id ap-northeast-1:11111111-... --dst-user-id 000003

# checked の特定の結果フォルダだけ
python s3_move.py --src-user-id 000002 --dst-user-id 000003 --status checked --folder SAT0202301
```

ユーザーID だけを指定した場合は `private/` 配下の全 Identity ID を走査して探すため、Identity ID の数が多いと時間がかかります。

### オプション

| オプション | 説明 |
| --- | --- |
| `--src-user-id` / `--src-identity-id` | 移動元のユーザーID / Identity ID（少なくとも一方） |
| `--dst-user-id` / `--dst-identity-id` | 移動先のユーザーID / Identity ID（少なくとも一方） |
| `--status` | `checked` / `unchecked` / `both`（既定: `both`） |
| `--folder NAME` | 対象の結果フォルダ名を指定（例: `SAT0202301`、複数指定可）。省略時は一覧から選択 |
| `--execute` | 実際に移動する（省略時はドライラン） |
| `--allow-new-destination` | 移動先のフォルダが存在しなくても続行 |
| `--config` / `--bucket` / `--profile` / `--region` | 設定ファイル・バケット・AWS プロファイル・リージョン |

### 元に戻すには

移動元と移動先を入れ替えて同じ手順で実行してください。移動した内容は操作ログで確認できます。

## テスト

```bash
pip install -r requirements-dev.txt
pytest
```

S3 は [moto](https://github.com/getmoto/moto) でモックしているため、AWS には接続しません。
