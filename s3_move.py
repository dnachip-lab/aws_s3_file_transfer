#!/usr/bin/env python3
"""S3 結果フォルダ移動ツール

あるユーザーID / Identified ID の checked / unchecked フォルダ配下にある
結果フォルダ (PDF, CSV) を、別のユーザーID / Identified ID の同じ
checked / unchecked フォルダへ、確認しながら移動する。

安全のための方針:
  * 既定はドライラン。--execute を付けたときだけ実際に移動する。
  * 移動元と移動先が同一なら中止。
  * 移動先 ID のフォルダが S3 上に存在しなければ中止 (ID の打ち間違い対策)。
  * 移動先に同名の結果フォルダ / 同名ファイルがある場合は上書きせずスキップ。
  * 実行前に移動計画を表示し、移動先 ID の再入力と "yes" の入力を求める。
  * コピー後にサイズを検証し、フォルダ内の全ファイルの検証が済んでから
    移動元を削除する。
  * 全操作を JSON ログに記録する。
"""

import argparse
import datetime
import json
import os
import sys
from dataclasses import dataclass, field

STATUSES = ("checked", "unchecked")
DELETE_BATCH = 1000


class MoveError(Exception):
    pass


@dataclass
class ResultFolder:
    status: str
    name: str
    src_prefix: str
    dst_prefix: str
    objects: list = field(default_factory=list)
    dst_exists: bool = False

    @property
    def total_size(self):
        return sum(o["Size"] for o in self.objects)

    def ext_counts(self):
        counts = {}
        for o in self.objects:
            ext = os.path.splitext(o["Key"])[1].lower() or "(なし)"
            counts[ext] = counts.get(ext, 0) + 1
        return counts


# ---------------------------------------------------------------- 設定 / パス

def load_config(path):
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    if not config.get("id_types"):
        raise MoveError("設定ファイルに id_types がありません")
    for id_type, template in config["id_types"].items():
        if "{id}" not in template or "{status}" not in template:
            raise MoveError(
                f"id_types.{id_type} のテンプレートには {{id}} と {{status}} が必要です: {template}")
    return config


def validate_id(id_value):
    id_value = (id_value or "").strip()
    if not id_value:
        raise MoveError("ID が空です")
    if "/" in id_value or id_value in (".", ".."):
        raise MoveError(f"ID に使用できない文字が含まれています: {id_value!r}")
    return id_value


def build_prefix(config, id_type, id_value, status):
    if id_type not in config["id_types"]:
        raise MoveError(
            f"未定義の ID 種別です: {id_type} (定義済み: {', '.join(config['id_types'])})")
    prefix = config["id_types"][id_type].format(id=id_value, status=status)
    return prefix if prefix.endswith("/") else prefix + "/"


def id_exists(s3, bucket, config, id_type, id_value):
    """その ID の checked / unchecked いずれかのフォルダが存在するか。"""
    return any(prefix_exists(s3, bucket, build_prefix(config, id_type, id_value, st))
               for st in STATUSES)


# ---------------------------------------------------------------- S3 操作

def list_subfolders(s3, bucket, prefix):
    names = []
    for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix, Delimiter="/"):
        for cp in page.get("CommonPrefixes", []):
            names.append(cp["Prefix"][len(prefix):].rstrip("/"))
    return sorted(names)


def list_objects(s3, bucket, prefix):
    objects = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            objects.append({"Key": o["Key"], "Size": o["Size"], "ETag": o.get("ETag")})
    return objects


def prefix_exists(s3, bucket, prefix):
    resp = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
    return resp.get("KeyCount", 0) > 0


def object_exists(s3, bucket, key):
    from botocore.exceptions import ClientError
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


# ---------------------------------------------------------------- 計画

def build_plan(s3, bucket, config, src_type, src_id, dst_type, dst_id, statuses, folder_names=None):
    plan = []
    for status in statuses:
        src_prefix = build_prefix(config, src_type, src_id, status)
        dst_prefix = build_prefix(config, dst_type, dst_id, status)
        for name in list_subfolders(s3, bucket, src_prefix):
            if folder_names and name not in folder_names:
                continue
            folder = ResultFolder(
                status=status,
                name=name,
                src_prefix=f"{src_prefix}{name}/",
                dst_prefix=f"{dst_prefix}{name}/",
            )
            folder.objects = list_objects(s3, bucket, folder.src_prefix)
            folder.dst_exists = prefix_exists(s3, bucket, folder.dst_prefix)
            plan.append(folder)
    return plan


def format_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


def print_plan(plan, expected_exts, out=sys.stdout):
    for i, f in enumerate(plan, 1):
        exts = ", ".join(f"{k}:{v}" for k, v in sorted(f.ext_counts().items()))
        mark = "  [移動不可: 移動先に同名フォルダあり]" if f.dst_exists else ""
        print(f"  {i:>3}. [{f.status}] {f.name}  ({len(f.objects)}件 {format_size(f.total_size)} / {exts}){mark}",
              file=out)
        print(f"         移動元: {f.src_prefix}", file=out)
        print(f"         移動先: {f.dst_prefix}", file=out)
        unexpected = [e for e in f.ext_counts() if expected_exts and e not in expected_exts]
        if unexpected:
            print(f"         ※注意: PDF/CSV 以外のファイルを含みます ({', '.join(unexpected)})", file=out)


def parse_selection(text, plan):
    text = text.strip().lower()
    movable = [i for i, f in enumerate(plan, 1) if not f.dst_exists]
    if text == "all":
        return movable
    selected = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            nums = range(int(a), int(b) + 1)
        else:
            nums = [int(part)]
        for n in nums:
            if n < 1 or n > len(plan):
                raise ValueError(f"範囲外の番号です: {n}")
            if plan[n - 1].dst_exists:
                raise ValueError(f"{n} は移動先に同名フォルダがあるため選択できません")
            if n not in selected:
                selected.append(n)
    return selected


# ---------------------------------------------------------------- 実行

def move_folder(s3, bucket, folder, log):
    """フォルダ内の全オブジェクトをコピー・検証し、すべて成功したら移動元を削除する。"""
    copied = []
    for obj in folder.objects:
        dst_key = folder.dst_prefix + obj["Key"][len(folder.src_prefix):]
        if object_exists(s3, bucket, dst_key):
            raise MoveError(f"移動先に既にファイルがあります (上書きしません): {dst_key}")
        s3.copy({"Bucket": bucket, "Key": obj["Key"]}, bucket, dst_key)
        head = s3.head_object(Bucket=bucket, Key=dst_key)
        if head["ContentLength"] != obj["Size"]:
            raise MoveError(
                f"コピー後のサイズが一致しません: {dst_key} ({head['ContentLength']} != {obj['Size']})")
        copied.append(obj["Key"])
        log.append({"action": "copy", "src": obj["Key"], "dst": dst_key, "size": obj["Size"],
                    "time": now_iso()})

    for i in range(0, len(copied), DELETE_BATCH):
        batch = copied[i:i + DELETE_BATCH]
        resp = s3.delete_objects(
            Bucket=bucket, Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True})
        if resp.get("Errors"):
            raise MoveError(f"移動元の削除に失敗しました: {resp['Errors']}")
        for k in batch:
            log.append({"action": "delete", "src": k, "time": now_iso()})


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def write_log(log_dir, meta, entries):
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"move_{datetime.datetime.now():%Y%m%d_%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "operations": entries}, f, ensure_ascii=False, indent=2)
    return path


# ---------------------------------------------------------------- CLI

def parse_args(argv):
    p = argparse.ArgumentParser(
        description="S3 上の結果フォルダを別のユーザーID / Identified ID のフォルダへ確認しながら移動します。")
    p.add_argument("--config", default="config.json", help="設定ファイル (既定: config.json)")
    p.add_argument("--bucket", help="バケット名 (設定ファイルより優先)")
    p.add_argument("--profile", help="AWS プロファイル名")
    p.add_argument("--region", help="AWS リージョン")
    p.add_argument("--src-type", required=True, help="移動元の ID 種別 (例: user_id, identified_id)")
    p.add_argument("--src-id", required=True, help="移動元の ID")
    p.add_argument("--dst-type", required=True, help="移動先の ID 種別 (例: user_id, identified_id)")
    p.add_argument("--dst-id", required=True, help="移動先の ID")
    p.add_argument("--status", choices=STATUSES + ("both",), default="both",
                   help="対象フォルダ (checked / unchecked / both、既定: both)")
    p.add_argument("--folder", action="append", dest="folders",
                   help="対象の結果フォルダ名 (複数指定可。省略時は一覧から選択)")
    p.add_argument("--allow-new-destination", action="store_true",
                   help="移動先 ID のフォルダが存在しなくても続行する (通常は使わないこと)")
    p.add_argument("--execute", action="store_true",
                   help="実際に移動する。省略時はドライラン (計画の表示のみ)")
    return p.parse_args(argv)


def make_client(args, config):
    import boto3
    session = boto3.Session(profile_name=args.profile) if args.profile else boto3.Session()
    return session.client("s3", region_name=args.region or config.get("region"))


def run(argv=None, s3=None, input_func=input, out=sys.stdout):
    args = parse_args(argv)
    config = load_config(args.config)
    bucket = args.bucket or config.get("bucket")
    if not bucket:
        raise MoveError("バケット名が指定されていません (--bucket または設定ファイルの bucket)")
    src_id = validate_id(args.src_id)
    dst_id = validate_id(args.dst_id)
    statuses = STATUSES if args.status == "both" else (args.status,)
    expected_exts = [e.lower() for e in config.get("expected_extensions", [".pdf", ".csv"])]

    if args.src_type == args.dst_type and src_id == dst_id:
        raise MoveError("移動元と移動先が同じです")
    for id_type in (args.src_type, args.dst_type):
        build_prefix(config, id_type, "x", STATUSES[0])  # 未定義の種別ならここでエラー

    s3 = s3 or make_client(args, config)

    print(f"バケット : {bucket}", file=out)
    print(f"移動元   : {args.src_type} = {src_id}", file=out)
    print(f"移動先   : {args.dst_type} = {dst_id}", file=out)
    print(f"対象     : {', '.join(statuses)}", file=out)
    print(f"モード   : {'実行' if args.execute else 'ドライラン (変更しません)'}", file=out)
    print("", file=out)

    if not id_exists(s3, bucket, config, args.src_type, src_id):
        raise MoveError(f"移動元 ID のフォルダが見つかりません: {args.src_type} = {src_id}")
    if not id_exists(s3, bucket, config, args.dst_type, dst_id):
        dst_root = " / ".join(build_prefix(config, args.dst_type, dst_id, st) for st in STATUSES)
        if not args.allow_new_destination:
            raise MoveError(
                f"移動先 ID のフォルダが見つかりません: {dst_root}\n"
                "ID の打ち間違いがないか確認してください。新規 ID への移動が意図どおりであれば "
                "--allow-new-destination を付けて再実行してください。")
        print(f"※警告: 移動先 ID のフォルダは存在しません。新規に作成されます: {dst_root}", file=out)

    plan = build_plan(s3, bucket, config, args.src_type, src_id, args.dst_type, dst_id,
                      statuses, args.folders)
    if args.folders:
        missing = sorted(set(args.folders) - {f.name for f in plan})
        if missing:
            raise MoveError(f"指定された結果フォルダが移動元にありません: {', '.join(missing)}")
    if not plan:
        print("移動対象の結果フォルダはありません。", file=out)
        return 0

    print("移動元の結果フォルダ:", file=out)
    print_plan(plan, expected_exts, out)
    print("", file=out)

    if not args.execute:
        print("ドライランのため終了します。実際に移動するには --execute を付けて実行してください。", file=out)
        return 0

    if not any(not f.dst_exists for f in plan):
        print("移動できる結果フォルダがありません (すべて移動先に同名フォルダがあります)。", file=out)
        return 1

    # 1. 対象の選択
    while True:
        text = input_func("移動するフォルダの番号を入力してください (例: 1,3,5-7 / all / q で中止): ")
        if text.strip().lower() == "q":
            print("中止しました。", file=out)
            return 1
        try:
            selected = parse_selection(text, plan)
        except ValueError as e:
            print(f"  入力エラー: {e}", file=out)
            continue
        if selected:
            break
        print("  1 つ以上選択してください。", file=out)

    targets = [plan[n - 1] for n in selected]

    # 2. 最終確認
    print("", file=out)
    print("=" * 70, file=out)
    print("以下の内容で移動します。移動先に間違いがないか必ず確認してください。", file=out)
    print(f"  移動元: {args.src_type} = {src_id}", file=out)
    print(f"  移動先: {args.dst_type} = {dst_id}", file=out)
    print("=" * 70, file=out)
    print_plan(targets, expected_exts, out)
    print("", file=out)

    typed = input_func(f"確認のため、移動先の ID を入力してください ({args.dst_type}): ")
    if typed.strip() != dst_id:
        print("移動先 ID が一致しません。中止しました。", file=out)
        return 1
    answer = input_func(f"{len(targets)} フォルダを移動します。実行してよろしいですか? (yes/no): ")
    if answer.strip().lower() != "yes":
        print("中止しました。", file=out)
        return 1

    # 3. 実行
    log = []
    meta = {"bucket": bucket, "src_type": args.src_type, "src_id": src_id,
            "dst_type": args.dst_type, "dst_id": dst_id, "started": now_iso(),
            "folders": [{"status": f.status, "name": f.name, "src": f.src_prefix,
                         "dst": f.dst_prefix} for f in targets]}
    failed = []
    try:
        for f in targets:
            print(f"移動中: [{f.status}] {f.name} ...", end=" ", file=out, flush=True)
            try:
                move_folder(s3, bucket, f, log)
                print("完了", file=out)
            except Exception as e:  # 1 フォルダの失敗で残りを続けず停止する
                print("失敗", file=out)
                log.append({"action": "error", "folder": f.src_prefix, "error": str(e),
                            "time": now_iso()})
                failed.append((f, e))
                break
    finally:
        meta["finished"] = now_iso()
        log_path = write_log(config.get("log_dir", "logs"), meta, log)
        print(f"操作ログ: {log_path}", file=out)

    if failed:
        f, e = failed[0]
        print(f"エラーのため処理を中断しました: {e}", file=out)
        print(f"失敗したフォルダ [{f.status}] {f.name} の移動元ファイルは削除されていません。"
              "移動先にコピー済みのファイルは操作ログで確認してください。", file=out)
        return 2
    print(f"{len(targets)} フォルダの移動が完了しました。", file=out)
    return 0


def main():
    try:
        sys.exit(run(sys.argv[1:]))
    except MoveError as e:
        print(f"エラー: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n中止しました。", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
