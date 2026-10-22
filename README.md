# aws_s3_file_transfer

S3 上で、あるユーザーID / Identified ID の `checked` / `unchecked` フォルダにある結果フォルダ（PDF, CSV）を、
別のユーザーID / Identified ID の同じ `checked` / `unchecked` フォルダへ **確認しながら** 移動するツールです。

```
results/users/U001/checked/R1/report.pdf   →  results/users/U002/checked/R1/report.pdf
results/users/U001/unchecked/R3/data.csv   →  results/identified/X100/unchecked/R3/data.csv
```

## 誤移動を防ぐ仕組み

| 仕組み | 内容 |
| --- | --- |
| 既定はドライラン | `--execute` を付けない限り、移動計画を表示するだけで S3 は変更しません |
| 移動先 ID の存在チェック | 移動先 ID の `checked` / `unchecked` フォルダがどちらも無い場合は中止します（ID の打ち間違い対策）。新規 ID へ移動するときだけ `--allow-new-destination` を付けます |
| 同一 ID の拒否 | 移動元と移動先が同じなら中止します |
| 上書き禁止 | 移動先に同名の結果フォルダがあるものは選択できません。ファイル単位でも既存ファイルがあれば中止します |
| checked / unchecked は維持 | `checked` は `checked` へ、`unchecked` は `unchecked` へのみ移動します |
| 3 段階の確認 | ① 移動するフォルダを番号で選択 → ② 移動先 ID を再入力（一致しなければ中止）→ ③ `yes` と入力 |
| コピー → 検証 → 削除 | コピー後にサイズを照合し、フォルダ内の全ファイルが検証できてから移動元を削除します。エラー時はその時点で停止し、失敗したフォルダの移動元は残ります |
| 操作ログ | コピー・削除した全ファイルを `logs/move_YYYYmmdd_HHMMSS.json` に記録します |
| 想定外ファイルの警告 | PDF / CSV 以外のファイルを含むフォルダには注意を表示します |

## セットアップ

```bash
pip install -r requirements.txt
cp config.example.json config.json   # バケット名・フォルダ構成を実際の環境に合わせて編集
```

`config.json` の `id_types` で、ID 種別ごとのフォルダ構成を指定します。`{id}` と `{status}`（checked / unchecked）が置き換えられます。

```json
{
  "bucket": "your-bucket-name",
  "region": "ap-northeast-1",
  "id_types": {
    "user_id": "results/users/{id}/{status}/",
    "identified_id": "results/identified/{id}/{status}/"
  },
  "expected_extensions": [".pdf", ".csv"],
  "log_dir": "logs"
}
```

AWS 認証情報は通常の boto3 の方法（`aws configure`、環境変数、`--profile` など）で設定します。
必要な権限: `s3:ListBucket`, `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`。

## 使い方

### 1. まずドライランで確認

```bash
python s3_move.py --src-type user_id --src-id U001 --dst-type user_id --dst-id U002
```

```
バケット : your-bucket-name
移動元   : user_id = U001
移動先   : user_id = U002
対象     : checked, unchecked
モード   : ドライラン (変更しません)

移動元の結果フォルダ:
    1. [checked] R1  (2件 8B / .csv:1, .pdf:1)
         移動元: results/users/U001/checked/R1/
         移動先: results/users/U002/checked/R1/
    2. [unchecked] R2  (1件 4B / .csv:1)
         移動元: results/users/U001/unchecked/R2/
         移動先: results/users/U002/unchecked/R2/
```

### 2. 実行

```bash
python s3_move.py --src-type user_id --src-id U001 --dst-type identified_id --dst-id X100 --execute
```

```
移動するフォルダの番号を入力してください (例: 1,3,5-7 / all / q で中止): 1
...（最終確認の一覧）...
確認のため、移動先の ID を入力してください (identified_id): X100
1 フォルダを移動します。実行してよろしいですか? (yes/no): yes
移動中: [checked] R1 ... 完了
操作ログ: logs/move_20261022_103000.json
```

### オプション

| オプション | 説明 |
| --- | --- |
| `--src-type` / `--dst-type` | ID 種別（`config.json` の `id_types` のキー。例: `user_id`, `identified_id`） |
| `--src-id` / `--dst-id` | 移動元 / 移動先の ID |
| `--status` | `checked` / `unchecked` / `both`（既定: `both`） |
| `--folder NAME` | 対象の結果フォルダ名を指定（複数指定可）。省略時は一覧から選択 |
| `--execute` | 実際に移動する（省略時はドライラン） |
| `--allow-new-destination` | 移動先 ID のフォルダが存在しなくても続行 |
| `--config` / `--bucket` / `--profile` / `--region` | 設定ファイル・バケット・AWS プロファイル・リージョン |

### 元に戻すには

移動元と移動先を入れ替えて同じ手順で実行してください。移動した内容は操作ログで確認できます。

## テスト

```bash
pip install -r requirements-dev.txt
pytest
```

S3 は [moto](https://github.com/getmoto/moto) でモックしているため、AWS には接続しません。
