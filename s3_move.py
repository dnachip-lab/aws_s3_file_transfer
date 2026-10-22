#!/usr/bin/env python3
"""S3 結果フォルダ移動ツール

S3 の構成:
    {bucket}/private/{Identity ID}/{ユーザーID}/{checked|unchecked}/{結果フォルダ}/*.pdf, *.csv
    例: private/ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da/000002/checked/SAT0202301/

あるユーザーID / Identity ID の checked / unchecked フォルダ配下にある
結果フォルダを、別のユーザーID / Identity ID の同じ checked / unchecked
フォルダへ、確認しながら移動する。
ユーザーID と Identity ID はどちらか一方だけ指定すれば、もう一方を S3 から特定する。

安全のための方針:
  * 既定はドライラン。--execute を付けたときだけ実際に移動する。
  * 移動元と移動先が同一なら中止。
  * ID から相手側の ID が一意に決まらない場合は中止。
  * 移動先のフォルダが S3 上に存在しなければ中止 (ID の打ち間違い対策)。
  * 移動先に同名の結果フォルダ / 同名ファイルがある場合は上書きせずスキップ。
  * 実行前に移動計画を表示し、移動先のユーザーID と Identity ID 末尾の再入力、
    "yes" の入力を求める。
  * コピー後にサイズを検証し、フォルダ内の全ファイルの検証が済んでから
    移動元を削除する。
  * 全操作を JSON ログに記録する。
"""

import argparse
import datetime
import json
import os
import re
import sys
from dataclasses import dataclass, field

STATUSES = ("checked", "unchecked")
DELETE_BATCH = 1000
CONFIRM_SUFFIX_LEN = 6
DEFAULT_ROOT_PREFIX = "private/"
DEFAULT_IDENTITY_PATTERN = r"^[a-z]{2}(-[a-z]+)+-\d+:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class MoveError(Exception):
    pass


@dataclass(frozen=True)
class Owner:
    """結果フォルダの持ち主 (Identity ID + ユーザーID)。"""
    identity_id: str
    user_id: str

    def __str__(self):
        return f"ユーザーID={self.user_id} / Identity ID={self.identity_id}"


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
    root = config.get("root_prefix", DEFAULT_ROOT_PREFIX)
    config["root_prefix"] = root if root.endswith("/") else root + "/"
    config.setdefault("identity_id_pattern", DEFAULT_IDENTITY_PATTERN)
    return config


def validate_user_id(value):
    value = (value or "").strip()
    if not value:
        raise MoveError("ユーザーID が空です")
    if "/" in value or value in (".", ".."):
        raise MoveError(f"ユーザーID に使用できない文字が含まれています: {value!r}")
    return value


def validate_identity_id(value, pattern):
    value = (value or "").strip()
    if not value:
        raise MoveError("Identity ID が空です")
    if not re.match(pattern, value):
        raise MoveError(
            f"Identity ID の形式が正しくありません: {value!r} "
            "(例: ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da)")
    return value


def owner_prefix(config, owner):
    return f"{config['root_prefix']}{owner.identity_id}/{owner.user_id}/"


def status_prefix(config, owner, status):
    return f"{owner_prefix(config, owner)}{status}/"


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


def owner_has_results(s3, bucket, config, owner):
    """checked / unchecked いずれかのフォルダが存在するか。"""
    return any(prefix_exists(s3, bucket, status_prefix(config, owner, st)) for st in STATUSES)


# ---------------------------------------------------------------- ID の特定

def resolve_owner(s3, bucket, config, label, user_id, identity_id, out):
    """ユーザーID / Identity ID の片方しか指定されていなければ、もう片方を S3 から特定する。"""
    root = config["root_prefix"]
    if user_id and identity_id:
        return Owner(identity_id, user_id)

    if identity_id:
        users = list_subfolders(s3, bucket, f"{root}{identity_id}/")
        if not users:
            raise MoveError(f"{label}の Identity ID のフォルダが見つかりません: {root}{identity_id}/")
        if len(users) > 1:
            raise MoveError(
                f"{label}の Identity ID 配下に複数のユーザーID があります: {', '.join(users)}\n"
                f"ユーザーID も指定してください。")
        print(f"{label}のユーザーID を特定しました: {users[0]}", file=out)
        return Owner(identity_id, users[0])

    print(f"{label}のユーザーID {user_id} の Identity ID を検索しています...", file=out)
    matches = [ident for ident in list_subfolders(s3, bucket, root)
               if prefix_exists(s3, bucket, f"{root}{ident}/{user_id}/")]
    if not matches:
        raise MoveError(f"{label}のユーザーID が見つかりません: {user_id}")
    if len(matches) > 1:
        raise MoveError(
            f"{label}のユーザーID {user_id} が複数の Identity ID 配下にあります: {', '.join(matches)}\n"
            f"Identity ID も指定してください。")
    print(f"{label}の Identity ID を特定しました: {matches[0]}", file=out)
    return Owner(matches[0], user_id)


# ---------------------------------------------------------------- 計画

def build_plan(s3, bucket, config, src, dst, statuses, folder_names=None):
    plan = []
    for status in statuses:
        src_prefix = status_prefix(config, src, status)
        dst_prefix = status_prefix(config, dst, status)
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
        description="S3 上の結果フォルダを別のユーザーID / Identity ID のフォルダへ確認しながら移動します。"
                    "各 ID はどちらか一方だけの指定でも構いません。")
    p.add_argument("--config", default="config.json", help="設定ファイル (既定: config.json)")
    p.add_argument("--bucket", help="バケット名 (設定ファイルより優先)")
    p.add_argument("--profile", help="AWS プロファイル名")
    p.add_argument("--region", help="AWS リージョン")
    p.add_argument("--src-user-id", help="移動元のユーザーID (例: 000002)")
    p.add_argument("--src-identity-id", help="移動元の Identity ID (例: ap-northeast-1:05efdd85-...)")
    p.add_argument("--dst-user-id", help="移動先のユーザーID")
    p.add_argument("--dst-identity-id", help="移動先の Identity ID")
    p.add_argument("--status", choices=STATUSES + ("both",), default="both",
                   help="対象フォルダ (checked / unchecked / both、既定: both)")
    p.add_argument("--folder", action="append", dest="folders",
                   help="対象の結果フォルダ名 (例: SAT0202301。複数指定可。省略時は一覧から選択)")
    p.add_argument("--allow-new-destination", action="store_true",
                   help="移動先のフォルダが存在しなくても続行する (通常は使わないこと)")
    p.add_argument("--execute", action="store_true",
                   help="実際に移動する。省略時はドライラン (計画の表示のみ)")
    args = p.parse_args(argv)
    if not (args.src_user_id or args.src_identity_id):
        p.error("--src-user-id か --src-identity-id の少なくとも一方を指定してください")
    if not (args.dst_user_id or args.dst_identity_id):
        p.error("--dst-user-id か --dst-identity-id の少なくとも一方を指定してください")
    return args


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
    pattern = config["identity_id_pattern"]

    def ids(user_id, identity_id):
        return (validate_user_id(user_id) if user_id else None,
                validate_identity_id(identity_id, pattern) if identity_id else None)

    src_user, src_ident = ids(args.src_user_id, args.src_identity_id)
    dst_user, dst_ident = ids(args.dst_user_id, args.dst_identity_id)
    statuses = STATUSES if args.status == "both" else (args.status,)
    expected_exts = [e.lower() for e in config.get("expected_extensions", [".pdf", ".csv"])]

    s3 = s3 or make_client(args, config)

    print(f"バケット : {bucket}", file=out)
    print(f"モード   : {'実行' if args.execute else 'ドライラン (変更しません)'}", file=out)
    src = resolve_owner(s3, bucket, config, "移動元", src_user, src_ident, out)
    dst = resolve_owner(s3, bucket, config, "移動先", dst_user, dst_ident, out)
    print(f"移動元   : {src}", file=out)
    print(f"移動先   : {dst}", file=out)
    print(f"対象     : {', '.join(statuses)}", file=out)
    print("", file=out)

    if src == dst:
        raise MoveError("移動元と移動先が同じです")
    if not owner_has_results(s3, bucket, config, src):
        raise MoveError(f"移動元の checked / unchecked フォルダが見つかりません: {owner_prefix(config, src)}")
    if not owner_has_results(s3, bucket, config, dst):
        if not args.allow_new_destination:
            raise MoveError(
                f"移動先の checked / unchecked フォルダが見つかりません: {owner_prefix(config, dst)}\n"
                "ID の打ち間違いがないか確認してください。新規の移動先が意図どおりであれば "
                "--allow-new-destination を付けて再実行してください。")
        print(f"※警告: 移動先のフォルダは存在しません。新規に作成されます: {owner_prefix(config, dst)}",
              file=out)

    plan = build_plan(s3, bucket, config, src, dst, statuses, args.folders)
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

    if all(f.dst_exists for f in plan):
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
    print(f"  移動元: {src}", file=out)
    print(f"  移動先: {dst}", file=out)
    print("=" * 70, file=out)
    print_plan(targets, expected_exts, out)
    print("", file=out)

    typed = input_func("確認のため、移動先のユーザーID を入力してください: ")
    if typed.strip() != dst.user_id:
        print("移動先のユーザーID が一致しません。中止しました。", file=out)
        return 1
    typed = input_func(f"確認のため、移動先の Identity ID の末尾 {CONFIRM_SUFFIX_LEN} 文字を入力してください: ")
    if typed.strip().lower() != dst.identity_id[-CONFIRM_SUFFIX_LEN:].lower():
        print("移動先の Identity ID が一致しません。中止しました。", file=out)
        return 1
    answer = input_func(f"{len(targets)} フォルダを移動します。実行してよろしいですか? (yes/no): ")
    if answer.strip().lower() != "yes":
        print("中止しました。", file=out)
        return 1

    # 3. 実行
    log = []
    meta = {"bucket": bucket,
            "src": {"identity_id": src.identity_id, "user_id": src.user_id},
            "dst": {"identity_id": dst.identity_id, "user_id": dst.user_id},
            "started": now_iso(),
            "folders": [{"status": f.status, "name": f.name, "src": f.src_prefix,
                         "dst": f.dst_prefix} for f in targets]}
    failed = None
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
                failed = (f, e)
                break
    finally:
        meta["finished"] = now_iso()
        log_path = write_log(config.get("log_dir", "logs"), meta, log)
        print(f"操作ログ: {log_path}", file=out)

    if failed:
        f, e = failed
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
