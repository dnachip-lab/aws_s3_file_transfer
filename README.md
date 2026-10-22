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
search で ID を確認 → list で一覧 CSV を作成 → CSV から不要な行を削除 → move --list でドライラン → move --list --execute
```

## 誤移動を防ぐ仕組み

| 仕組み | 内容 |
| --- | --- |
| 既定はドライラン | `--execute` を付けない限り、移動計画を表示するだけで S3 は変更しません |
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

## セットアップ

```bash
pip install -r requirements.txt
cp config.example.json config.json   # バケット名などを実際の環境に合わせて編集
```

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

AWS 認証情報は通常の boto3 の方法（`aws configure`、環境変数、`--profile` など）で設定します。
必要な権限: `s3:ListBucket`, `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`。

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
python s3_move.py move --list list.csv --dst-user-id 000003            # ドライラン
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
| `--execute` | | | ✓ | 実際に移動する（省略時はドライラン） |
| `--allow-new-destination` | | | ✓ | 移動先のフォルダが存在しなくても続行 |

### 元に戻すには

移動元と移動先を入れ替えて同じ手順で実行してください。移動した内容は操作ログで確認できます。

## テスト

```bash
pip install -r requirements-dev.txt
pytest
```

S3 は [moto](https://github.com/getmoto/moto) でモックしているため、AWS には接続しません。
