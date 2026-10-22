#!/usr/bin/env python3
"""S3 結果フォルダ移動ツール

S3 の構成:
    {bucket}/private/{Identity ID}/{ユーザーID}/{checked|unchecked}/{結果フォルダ}/*.pdf, *.csv
    例: private/ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da/000002/checked/SAT0202301/

サブコマンド:
    search  Identity ID またはユーザーID で対象フォルダを検索する
    list    移動対象となる結果フォルダ (PDF / CSV を含む) の一覧を、アップロード日などの
            情報付きで表示する。期間指定で絞り込み、CSV に保存できる
    move    結果フォルダを別のユーザーID / Identity ID の同じ checked / unchecked フォルダへ
            確認しながら移動する。期間指定、または list で保存した CSV による一括移動ができる

move の安全方針:
  * 既定はドライラン。--execute を付けたときだけ実際に移動する。
  * 移動元と移動先が同一なら中止。ID から相手側の ID が一意に決まらない場合も中止。
  * 移動先のフォルダが S3 上に存在しなければ中止 (ID の打ち間違い対策)。
  * 移動先に同名の結果フォルダ / 同名ファイルがある場合は上書きせず対象外にする。
  * 一覧 CSV 作成後にファイル数が変わったフォルダは対象外にする。
  * 実行前に移動計画を表示し、移動先のユーザーID と Identity ID (全体) の再入力、
    "yes" の入力を求める。
  * コピー後にサイズを検証し、フォルダ内の全ファイルの検証が済んでから
    移動元を削除する。
  * 全操作を JSON ログに記録する。
"""

import argparse
import csv
import fnmatch
import datetime
import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field

STATUSES = ("checked", "unchecked")
DELETE_BATCH = 1000
DEFAULT_ROOT_PREFIX = "private/"
DEFAULT_EXTENSIONS = (".pdf", ".csv")
DEFAULT_UTC_OFFSET_HOURS = 9
DEFAULT_IDENTITY_PATTERN = r"^[a-z]{2}(-[a-z]+)+-\d+:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
LIST_COLUMNS = ["identity_id", "user_id", "status", "folder", "pdf_count", "csv_count",
                "other_count", "file_count", "total_size", "first_uploaded", "last_uploaded"]


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
    objects: list = field(default_factory=list)
    dst_prefix: str = ""
    blocked: str = ""  # 移動できない理由 (空なら移動可)

    @property
    def total_size(self):
        return sum(o["Size"] for o in self.objects)

    @property
    def first_uploaded(self):
        return min((o["LastModified"] for o in self.objects), default=None)

    @property
    def last_uploaded(self):
        return max((o["LastModified"] for o in self.objects), default=None)

    def ext_counts(self):
        counts = {}
        for o in self.objects:
            ext = os.path.splitext(o["Key"])[1].lower() or "(なし)"
            counts[ext] = counts.get(ext, 0) + 1
        return counts

    def count_of(self, ext):
        return self.ext_counts().get(ext, 0)

    def other_count(self, expected_exts):
        return sum(v for k, v in self.ext_counts().items() if k not in expected_exts)

    def has_targets(self, expected_exts):
        return any(self.count_of(e) for e in expected_exts)


# ---------------------------------------------------------------- 設定 / パス

def load_config(path):
    with open(path, encoding="utf-8") as f:
        config = json.load(f)
    root = config.get("root_prefix", DEFAULT_ROOT_PREFIX)
    config["root_prefix"] = root if root.endswith("/") else root + "/"
    config.setdefault("identity_id_pattern", DEFAULT_IDENTITY_PATTERN)
    config["expected_extensions"] = [
        e.lower() for e in config.get("expected_extensions", DEFAULT_EXTENSIONS)]
    config["tz"] = datetime.timezone(
        datetime.timedelta(hours=config.get("utc_offset_hours", DEFAULT_UTC_OFFSET_HOURS)))
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


def validated_ids(config, user_id, identity_id):
    return (validate_user_id(user_id) if user_id else None,
            validate_identity_id(identity_id, config["identity_id_pattern"]) if identity_id else None)


def owner_prefix(config, owner):
    return f"{config['root_prefix']}{owner.identity_id}/{owner.user_id}/"


def status_prefix(config, owner, status):
    return f"{owner_prefix(config, owner)}{status}/"


def parse_date(value):
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"日付は YYYY-MM-DD 形式で指定してください: {value}")


def format_dt(dt, tz):
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def format_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024


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
            if o["Key"].endswith("/"):  # コンソールで作成したフォルダのマーカー
                continue
            objects.append({"Key": o["Key"], "Size": o["Size"],
                            "LastModified": o["LastModified"]})
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


# ---------------------------------------------------------------- 検索 / ID の特定

def find_owners(s3, bucket, config, user_id=None, identity_id=None):
    """ユーザーID / Identity ID に該当する持ち主をすべて返す。"""
    root = config["root_prefix"]
    if user_id and identity_id:
        exists = prefix_exists(s3, bucket, f"{root}{identity_id}/{user_id}/")
        return [Owner(identity_id, user_id)] if exists else []
    if identity_id:
        return [Owner(identity_id, u) for u in list_subfolders(s3, bucket, f"{root}{identity_id}/")]
    return [Owner(ident, user_id) for ident in list_subfolders(s3, bucket, root)
            if prefix_exists(s3, bucket, f"{root}{ident}/{user_id}/")]


OPTION_PREFIX = {"移動元": "--src-", "移動先": "--dst-", "対象": "--"}


def owner_summary(s3, bucket, config, owner):
    """持ち主の移動対象フォルダ数 (checked / unchecked) と最終アップロード日時。"""
    folders = collect_folders(s3, bucket, config, owner, STATUSES)
    counts = ", ".join(f"{st}: {sum(f.status == st for f in folders)}" for st in STATUSES)
    last = max((f.last_uploaded for f in folders), default=None)
    return f"移動対象フォルダ {counts} / 最終アップロード {format_dt(last, config['tz']) or 'なし'}"


def check_pair_mismatch(s3, bucket, config, label, owner, out):
    """指定されたユーザーID と Identity ID の組が S3 に無いとき、そのユーザーID が別の Identity ID
    配下に存在すれば組み合わせの誤りとして中止する。"""
    print(f"{label}のユーザーID {owner.user_id} が他の Identity ID 配下にないか確認しています...", file=out)
    others = [o for o in find_owners(s3, bucket, config, owner.user_id) if o != owner]
    if others:
        option = OPTION_PREFIX.get(label, "--") + "identity-id"
        lines = [f"{label}のユーザーID {owner.user_id} と Identity ID {owner.identity_id} の組み合わせが"
                 "一致しません。",
                 f"  ユーザーID {owner.user_id} は次の Identity ID 配下にあります:"]
        for o in others:
            lines.append(f"    {option} {o.identity_id}")
            lines.append(f"      {owner_summary(s3, bucket, config, o)}")
        users = list_subfolders(s3, bucket, f"{config['root_prefix']}{owner.identity_id}/")
        if users:
            lines.append(f"  指定した Identity ID 配下のユーザーID: {', '.join(users[:20])}")
        lines.append("ユーザーID と Identity ID の組み合わせを確認してください。")
        raise MoveError("\n".join(lines))


def resolve_owner(s3, bucket, config, label, user_id, identity_id, out):
    """ユーザーID / Identity ID から持ち主を 1 つに特定する。一意に決まらなければ中止。"""
    if user_id and identity_id:
        owner = Owner(identity_id, user_id)
        if not prefix_exists(s3, bucket, owner_prefix(config, owner)):
            check_pair_mismatch(s3, bucket, config, label, owner, out)
        return owner
    if user_id:
        print(f"{label}のユーザーID {user_id} の Identity ID を検索しています...", file=out)
    matches = find_owners(s3, bucket, config, user_id, identity_id)
    given = f"ユーザーID {user_id}" if user_id else f"Identity ID {identity_id}"
    if not matches:
        raise MoveError(f"{label}の{given} が見つかりません")
    if len(matches) > 1:
        other = "Identity ID" if user_id else "ユーザーID"
        option = OPTION_PREFIX.get(label, "--") + ("identity-id" if user_id else "user-id")
        lines = [f"{label}の{given} に該当する{other} が {len(matches)} 件あります。"
                 f"どれを使うか {option} で指定してください。", ""]
        for i, m in enumerate(matches, 1):
            value = m.identity_id if user_id else m.user_id
            lines.append(f"  {i}. {other}={value}")
            lines.append(f"     {owner_summary(s3, bucket, config, m)}")
            lines.append(f"     指定方法: {option} {value}")
        raise MoveError("\n".join(lines))
    owner = matches[0]
    if user_id:
        print(f"{label}の Identity ID を特定しました: {owner.identity_id}", file=out)
    else:
        print(f"{label}のユーザーID を特定しました: {owner.user_id}", file=out)
    return owner


# ---------------------------------------------------------------- 結果フォルダの収集

def load_folder(s3, bucket, config, owner, status, name):
    prefix = f"{status_prefix(config, owner, status)}{name}/"
    return ResultFolder(status=status, name=name, src_prefix=prefix,
                        objects=list_objects(s3, bucket, prefix))


def split_names(values):
    """--folder / --name の値 (複数指定・カンマ区切り可) を平坦なリストにする。"""
    return [v.strip() for value in (values or []) for v in value.split(",") if v.strip()]


def name_matches(name, folders=None, patterns=None):
    """フォルダ名の条件: --folder (完全一致) と --name (ワイルドカード、大文字小文字を区別しない)
    のどれかに一致すれば対象。どちらも未指定なら全件対象。"""
    if not folders and not patterns:
        return True
    if folders and name in folders:
        return True
    return any(fnmatch.fnmatchcase(name.lower(), p.lower()) for p in patterns or [])


def name_filter_text(args):
    parts = []
    if args.folders:
        parts.append("フォルダ名 " + ", ".join(args.folders))
    if args.names:
        parts.append("パターン " + ", ".join(args.names))
    return " または ".join(parts) if parts else "指定なし"


def collect_folders(s3, bucket, config, owner, statuses, folder_names=None, patterns=None):
    """移動対象となる結果フォルダ (PDF / CSV を含むもの) を返す。"""
    folders = []
    for status in statuses:
        for name in list_subfolders(s3, bucket, status_prefix(config, owner, status)):
            if not name_matches(name, folder_names, patterns):
                continue
            folder = load_folder(s3, bucket, config, owner, status, name)
            if folder.has_targets(config["expected_extensions"]):
                folders.append(folder)
    return folders


def in_period(folder, since, until, basis, tz):
    dt = folder.last_uploaded if basis == "last" else folder.first_uploaded
    if dt is None:
        return False
    d = dt.astimezone(tz).date()
    return (since is None or d >= since) and (until is None or d <= until)


def filter_period(folders, args, config):
    if not (args.since or args.until):
        return folders
    return [f for f in folders
            if in_period(f, args.since, args.until, args.date_basis, config["tz"])]


def period_text(args):
    if not (args.since or args.until):
        return "指定なし"
    basis = "最終アップロード日" if args.date_basis == "last" else "最初のアップロード日"
    return f"{basis}が {args.since or ''} 〜 {args.until or ''}"


# ---------------------------------------------------------------- 一覧ファイル

def write_list_csv(path, owner, folders, config):
    exts = config["expected_extensions"]
    # Excel で開けるように BOM 付き UTF-8
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LIST_COLUMNS)
        w.writeheader()
        for folder in folders:
            w.writerow({
                "identity_id": owner.identity_id,
                "user_id": owner.user_id,
                "status": folder.status,
                "folder": folder.name,
                "pdf_count": folder.count_of(".pdf"),
                "csv_count": folder.count_of(".csv"),
                "other_count": folder.other_count(exts),
                "file_count": len(folder.objects),
                "total_size": folder.total_size,
                "first_uploaded": format_dt(folder.first_uploaded, config["tz"]),
                "last_uploaded": format_dt(folder.last_uploaded, config["tz"]),
            })


def read_list_csv(path, config):
    """一覧 CSV を読み、(持ち主, [(status, folder, file_count)]) を返す。"""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise MoveError(f"一覧ファイルに行がありません: {path}")
    missing = {"identity_id", "user_id", "status", "folder"} - set(rows[0])
    if missing:
        raise MoveError(f"一覧ファイルに必要な列がありません: {', '.join(sorted(missing))}")

    owners = set()
    entries = []
    for i, row in enumerate(rows, 2):
        user_id, identity_id = validated_ids(config, row["user_id"], row["identity_id"])
        if not (user_id and identity_id):
            raise MoveError(f"一覧ファイル {i} 行目: identity_id と user_id は必須です")
        status = row["status"].strip()
        if status not in STATUSES:
            raise MoveError(f"一覧ファイル {i} 行目: status が不正です: {status!r}")
        name = row["folder"].strip()
        if not name or "/" in name:
            raise MoveError(f"一覧ファイル {i} 行目: folder が不正です: {name!r}")
        count = row.get("file_count", "").strip()
        owners.add(Owner(identity_id, user_id))
        entries.append((status, name, int(count) if count else None))
    if len(owners) > 1:
        raise MoveError("一覧ファイルに複数の移動元 (ユーザーID / Identity ID) が含まれています。"
                        "移動元ごとにファイルを分けてください。")
    if len({(s, n) for s, n, _ in entries}) != len(entries):
        raise MoveError("一覧ファイルに同じフォルダが重複しています")
    return owners.pop(), entries


# ---------------------------------------------------------------- 表示

def display_width(text):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def ljust(text, width):
    """全角文字を 2 桁として左詰めする。"""
    return text + " " * max(width - display_width(text), 0)


def rjust(text, width):
    return " " * max(width - display_width(text), 0) + text


def print_folder_table(folders, config, out):
    exts = config["expected_extensions"]
    tz = config["tz"]
    print(f"  {'No':>4}  {ljust('状態', 9)} {ljust('結果フォルダ', 20)} {'PDF':>4} {'CSV':>4} "
          f"{rjust('他', 3)} {rjust('サイズ', 9)}  {ljust('最初のアップロード', 19)}  "
          f"{ljust('最終アップロード', 19)}".rstrip(), file=out)
    for i, f in enumerate(folders, 1):
        print(f"  {i:>4}. {f.status:<9} {f.name:<20} {f.count_of('.pdf'):>4} {f.count_of('.csv'):>4} "
              f"{f.other_count(exts):>3} {format_size(f.total_size):>9}  "
              f"{format_dt(f.first_uploaded, tz):<19}  {format_dt(f.last_uploaded, tz):<19}", file=out)


def print_plan(plan, config, out):
    exts = config["expected_extensions"]
    tz = config["tz"]
    for i, f in enumerate(plan, 1):
        counts = ", ".join(f"{k}:{v}" for k, v in sorted(f.ext_counts().items()))
        mark = f"  [移動不可: {f.blocked}]" if f.blocked else ""
        print(f"  {i:>3}. [{f.status}] {f.name}  ({len(f.objects)}件 {format_size(f.total_size)} / "
              f"{counts} / 最終アップロード {format_dt(f.last_uploaded, tz)}){mark}", file=out)
        print(f"         移動元: {f.src_prefix}", file=out)
        print(f"         移動先: {f.dst_prefix}", file=out)
        if f.other_count(exts):
            others = [e for e in f.ext_counts() if e not in exts]
            print(f"         ※注意: PDF/CSV 以外のファイルを含みます ({', '.join(others)})", file=out)


def parse_selection(text, plan):
    text = text.strip().lower()
    if text == "all":
        return [i for i, f in enumerate(plan, 1) if not f.blocked]
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
            if plan[n - 1].blocked:
                raise ValueError(f"{n} は選択できません ({plan[n - 1].blocked})")
            if n not in selected:
                selected.append(n)
    return selected


# ---------------------------------------------------------------- 実行

def move_folder(s3, bucket, folder, log):
    """フォルダ内の全オブジェクトをコピー・検証し、すべて成功したら移動元を削除する。"""
    copied = []
    for obj in folder.objects:
        dst_key = dst_key_of(folder, obj)
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


def print_new_destination_warning(s3, bucket, config, dst, out):
    """移動先が存在しないとき、打ち間違いに気付けるよう周辺の状況を表示する。"""
    root = config["root_prefix"]
    print("※警告: 移動先の checked / unchecked フォルダが見つかりません。", file=out)
    print(f"  移動先: {owner_prefix(config, dst)}", file=out)
    users = list_subfolders(s3, bucket, f"{root}{dst.identity_id}/")
    if users:
        shown = ", ".join(users[:20]) + (f" ほか {len(users) - 20} 件" if len(users) > 20 else "")
        print(f"  この Identity ID 配下の既存ユーザーID: {shown}", file=out)
    else:
        print("  この Identity ID のフォルダも存在しません。", file=out)
    print("  ID の打ち間違いがないか必ず確認してください。", file=out)


def create_destination_folders(s3, bucket, config, dst):
    """移動先の checked / unchecked フォルダ (S3 コンソールと同じ空のフォルダオブジェクト) を作成する。"""
    created = []
    for st in STATUSES:
        key = status_prefix(config, dst, st)
        if not prefix_exists(s3, bucket, key):
            s3.put_object(Bucket=bucket, Key=key, Body=b"")
            created.append(key)
    return created


def identity_confirmed(typed, identity_id):
    """確認入力が Identity ID 全体と一致するか (前後の空白・大文字小文字は無視)。"""
    return typed.strip().lower() == identity_id.lower()


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def write_log(log_dir, meta, entries, kind="move"):
    os.makedirs(log_dir, exist_ok=True)
    path = os.path.join(log_dir, f"{kind}_{datetime.datetime.now():%Y%m%d_%H%M%S}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "operations": entries}, f, ensure_ascii=False, indent=2)
    return path


def dst_key_of(folder, obj):
    return folder.dst_prefix + obj["Key"][len(folder.src_prefix):]


def dry_run_report(plan, config, out):
    """ドライラン: 移動されるファイルを 1 件ずつ表示し、レポート用の操作一覧を返す。"""
    tz = config["tz"]
    entries = []
    movable = [f for f in plan if not f.blocked]
    print("移動予定のファイル (ドライラン):", file=out)
    for f in movable:
        print(f"  [{f.status}] {f.name}", file=out)
        for obj in f.objects:
            dst = dst_key_of(f, obj)
            print(f"    {obj['Key']}", file=out)
            print(f"      → {dst}  ({format_size(obj['Size'])}, "
                  f"アップロード {format_dt(obj['LastModified'], tz)})", file=out)
            entries.append({"action": "planned_move", "src": obj["Key"], "dst": dst,
                            "size": obj["Size"]})
    for f in plan:
        if f.blocked:
            entries.append({"action": "skip", "folder": f.src_prefix, "reason": f.blocked})
    print("", file=out)
    print(f"移動予定: {len(movable)} フォルダ / {sum(len(f.objects) for f in movable)} ファイル / "
          f"{format_size(sum(f.total_size for f in movable))}", file=out)
    if len(movable) != len(plan):
        print(f"対象外  : {len(plan) - len(movable)} フォルダ (移動不可の理由は上の一覧を参照)", file=out)
    return entries


# ---------------------------------------------------------------- サブコマンド

def load_move_log(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    meta = data.get("meta", {})
    if meta.get("dry_run"):
        raise MoveError(f"ドライランのレポートは元に戻せません (S3 は変更されていません): {path}")
    if "src" not in meta or "dst" not in meta:
        raise MoveError(f"操作ログの形式が正しくありません: {path}")
    return meta, data.get("operations", [])


def build_undo_plan(s3, bucket, operations):
    """操作ログから元に戻す手順を作る。
    戻り値: (restore, cleanup, remove_markers, problems)
      restore : 移動済み (移動元削除済み) のファイル。移動先 → 移動元へ戻して移動先を削除
      cleanup : コピーのみ (移動元が残っている) のファイル。移動先のコピーを削除
      remove_markers : 移動時に作成したフォルダオブジェクト
    """
    deleted = {o["src"] for o in operations if o.get("action") == "delete"}
    restore, cleanup, problems = [], [], []
    for o in operations:
        if o.get("action") != "copy":
            continue
        original, moved, size = o["src"], o["dst"], o["size"]
        try:
            head = s3.head_object(Bucket=bucket, Key=moved)
        except Exception:
            head = None
        if head is None:
            problems.append(f"移動先にファイルがありません (既に戻した可能性があります): {moved}")
            continue
        if head["ContentLength"] != size:
            problems.append(f"移動後にファイルが変更されています (サイズ {size} → {head['ContentLength']}): {moved}")
            continue
        if original in deleted:
            if object_exists(s3, bucket, original):
                problems.append(f"移動元に同名ファイルが既にあります (上書きしません): {original}")
                continue
            restore.append({"original": original, "moved": moved, "size": size})
        else:
            if not object_exists(s3, bucket, original):
                problems.append(f"移動元のファイルが見つかりません: {original}")
                continue
            cleanup.append({"original": original, "moved": moved, "size": size})
    markers = [o["key"] for o in operations
               if o.get("action") == "create_folder" and object_exists(s3, bucket, o["key"])]
    return restore, cleanup, markers, problems


def cmd_undo(args, config, s3, bucket, input_func, out):
    meta, operations = load_move_log(args.log)
    if meta.get("bucket") and meta["bucket"] != bucket:
        raise MoveError(f"操作ログのバケット ({meta['bucket']}) と設定のバケット ({bucket}) が異なります")
    src = Owner(meta["src"]["identity_id"], meta["src"]["user_id"])
    dst = Owner(meta["dst"]["identity_id"], meta["dst"]["user_id"])

    print(f"バケット : {bucket}", file=out)
    print(f"モード   : {'実行' if args.execute else 'ドライラン (S3 は変更しません)'}", file=out)
    print(f"操作ログ : {args.log} (実行日時 {meta.get('started', '?')})", file=out)
    print(f"元に戻す : {dst}", file=out)
    print(f"      → : {src}", file=out)
    print("", file=out)

    restore, cleanup, markers, problems = build_undo_plan(s3, bucket, operations)
    if restore:
        print(f"移動元へ戻すファイル ({len(restore)} 件):", file=out)
        for r in restore:
            print(f"  {r['moved']}", file=out)
            print(f"    → {r['original']}", file=out)
    if cleanup:
        print(f"移動先から削除するコピー ({len(cleanup)} 件、移動元は残っています):", file=out)
        for c in cleanup:
            print(f"  {c['moved']}", file=out)
    if markers:
        print("移動時に作成したフォルダ (中身が空なら削除):", file=out)
        for m in markers:
            print(f"  {m}", file=out)
    if problems:
        print("", file=out)
        print(f"※ 次の {len(problems)} 件は対象外です:", file=out)
        for p_ in problems:
            print(f"  {p_}", file=out)
    print("", file=out)
    if not (restore or cleanup or markers):
        print("元に戻す対象はありません。", file=out)
        return 0 if not problems else 1
    if not args.execute:
        print("ドライランのため終了します。元に戻すには --execute を付けて実行してください。", file=out)
        return 0

    typed = input_func("確認のため、戻し先 (元の移動元) のユーザーID を入力してください: ")
    if typed.strip() != src.user_id:
        print("ユーザーID が一致しません。中止しました。", file=out)
        return 1
    answer = input_func(f"{len(restore)} ファイルを戻し、{len(cleanup)} 件のコピーを削除します。"
                        "実行してよろしいですか? (yes/no): ")
    if answer.strip().lower() != "yes":
        print("中止しました。", file=out)
        return 1

    log = []
    undo_meta = {"bucket": bucket, "undo_of": args.log,
                 "src": meta["dst"], "dst": meta["src"], "started": now_iso()}
    try:
        for r in restore:
            s3.copy({"Bucket": bucket, "Key": r["moved"]}, bucket, r["original"])
            head = s3.head_object(Bucket=bucket, Key=r["original"])
            if head["ContentLength"] != r["size"]:
                raise MoveError(f"戻したファイルのサイズが一致しません: {r['original']}")
            log.append({"action": "copy", "src": r["moved"], "dst": r["original"], "size": r["size"],
                        "time": now_iso()})
        to_delete = [r["moved"] for r in restore] + [c["moved"] for c in cleanup]
        for i in range(0, len(to_delete), DELETE_BATCH):
            batch = to_delete[i:i + DELETE_BATCH]
            resp = s3.delete_objects(
                Bucket=bucket, Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True})
            if resp.get("Errors"):
                raise MoveError(f"移動先の削除に失敗しました: {resp['Errors']}")
            for k in batch:
                log.append({"action": "delete", "src": k, "time": now_iso()})
        for m in markers:
            remaining = [o for o in list_objects(s3, bucket, m)]
            if remaining:
                print(f"フォルダに他のファイルがあるため残します: {m}", file=out)
                continue
            s3.delete_object(Bucket=bucket, Key=m)
            log.append({"action": "delete_folder", "key": m, "time": now_iso()})
    finally:
        undo_meta["finished"] = now_iso()
        path = write_log(config.get("log_dir", "logs"), undo_meta, log, kind="undo")
        print(f"操作ログ: {path}", file=out)
    print(f"元に戻しました ({len(restore)} ファイルを戻し、{len(cleanup)} 件のコピーを削除)。", file=out)
    return 0


def cmd_search(args, config, s3, bucket, input_func, out):
    user_id, identity_id = validated_ids(config, args.user_id, args.identity_id)
    owners = find_owners(s3, bucket, config, user_id, identity_id)
    if not owners:
        print("該当するフォルダは見つかりませんでした。", file=out)
        return 1
    print(f"{len(owners)} 件見つかりました:", file=out)
    for o in owners:
        print(f"  ユーザーID={o.user_id}  Identity ID={o.identity_id}", file=out)
        print(f"    {owner_summary(s3, bucket, config, o)}", file=out)
        print(f"    {owner_prefix(config, o)}", file=out)
    return 0


def basis_date(folder, basis, tz):
    dt = folder.last_uploaded if basis == "last" else folder.first_uploaded
    return dt.astimezone(tz).date() if dt else None


def sort_folders(folders, key):
    if key == "name":
        return folders
    attr = "last_uploaded" if key == "last" else "first_uploaded"
    return sorted(folders, key=lambda f: getattr(f, attr))


def print_date_summary(folders, args, config, out):
    """期間指定の目安として、アップロード日の範囲と月別のフォルダ数を表示する。"""
    dates = [basis_date(f, args.date_basis, config["tz"]) for f in folders]
    basis = "最終アップロード日" if args.date_basis == "last" else "最初のアップロード日"
    print(f"{basis}の範囲: {min(dates)} 〜 {max(dates)}", file=out)
    months = {}
    for d in dates:
        months[d.strftime("%Y-%m")] = months.get(d.strftime("%Y-%m"), 0) + 1
    print(f"月別フォルダ数 ({basis}): " +
          ", ".join(f"{m}: {n}" for m, n in sorted(months.items())), file=out)


def print_empty_hint(s3, bucket, config, owner, args, out):
    """絞り込みで 0 件になったとき、どの条件で外れたかの目安を表示する。"""
    every = collect_folders(s3, bucket, config, owner, STATUSES)
    if not every:
        print("  (このユーザーには PDF/CSV を含む結果フォルダがありません)", file=out)
        return
    by_name = [f for f in every if name_matches(f.name, args.folders, args.names)]
    counts = ", ".join(f"{st}: {sum(f.status == st for f in by_name)}" for st in STATUSES)
    if args.folders or args.names:
        print(f"  フォルダ名の条件だけで絞り込んだ場合: {len(by_name)} 件 ({counts})", file=out)
    else:
        print(f"  絞り込みなしの場合: {len(every)} 件 ({counts})", file=out)
    if by_name and args.status != "both":
        print("  --status の指定を見直してください (both にすると checked / unchecked の両方が対象)。",
              file=out)
    elif by_name and (args.since or args.until):
        dates = [basis_date(f, args.date_basis, config["tz"]) for f in by_name]
        print(f"  期間の指定を見直してください (該当フォルダのアップロード日: {min(dates)} 〜 {max(dates)})。",
              file=out)


def cmd_list(args, config, s3, bucket, input_func, out):
    user_id, identity_id = validated_ids(config, args.user_id, args.identity_id)
    owner = resolve_owner(s3, bucket, config, "対象", user_id, identity_id, out)
    statuses = STATUSES if args.status == "both" else (args.status,)
    folders = collect_folders(s3, bucket, config, owner, statuses, args.folders, args.names)
    missing = sorted(set(args.folders or []) - {f.name for f in folders})
    folders = sort_folders(filter_period(folders, args, config), args.sort)

    print(f"対象     : {owner}", file=out)
    print(f"フォルダ : {', '.join(statuses)}", file=out)
    print(f"フォルダ名: {name_filter_text(args)}", file=out)
    print(f"期間     : {period_text(args)}", file=out)
    print("", file=out)
    if missing:
        print(f"※注意: 指定されたフォルダが見つかりません (PDF/CSV を含むもの): {', '.join(missing)}",
              file=out)
    if not folders:
        print("該当する結果フォルダはありません。", file=out)
        print_empty_hint(s3, bucket, config, owner, args, out)
        return 0
    print_folder_table(folders, config, out)
    print("", file=out)
    print(f"{len(folders)} フォルダ / {sum(len(f.objects) for f in folders)} ファイル / "
          f"{format_size(sum(f.total_size for f in folders))}", file=out)
    print_date_summary(folders, args, config, out)
    if not (args.since or args.until):
        print("期間で絞り込むには --since YYYY-MM-DD / --until YYYY-MM-DD を付けて実行してください。", file=out)
    if args.output:
        write_list_csv(args.output, owner, folders, config)
        print(f"一覧を保存しました: {args.output}", file=out)
        print("不要な行を削除してから move --list で一括移動できます。", file=out)
    return 0


def cmd_move(args, config, s3, bucket, input_func, out):
    statuses = STATUSES if args.status == "both" else (args.status,)
    dst_user, dst_ident = validated_ids(config, args.dst_user_id, args.dst_identity_id)

    print(f"バケット : {bucket}", file=out)
    print(f"モード   : {'実行' if args.execute else 'ドライラン (S3 は変更しません)'}", file=out)

    entries = None
    if args.list:
        src, entries = read_list_csv(args.list, config)
        src_user, src_ident = validated_ids(config, args.src_user_id, args.src_identity_id)
        if (src_user and src_user != src.user_id) or (src_ident and src_ident != src.identity_id):
            raise MoveError(f"指定した移動元と一覧ファイルの移動元が一致しません: 一覧ファイル {src}")
        print(f"一覧     : {args.list} ({len(entries)} フォルダ)", file=out)
    else:
        src_user, src_ident = validated_ids(config, args.src_user_id, args.src_identity_id)
        if not (src_user or src_ident):
            raise MoveError("--src-user-id か --src-identity-id、または --list を指定してください")
        src = resolve_owner(s3, bucket, config, "移動元", src_user, src_ident, out)
    dst = resolve_owner(s3, bucket, config, "移動先", dst_user, dst_ident, out)

    print(f"移動元   : {src}", file=out)
    print(f"移動先   : {dst}", file=out)
    if not args.list:
        print(f"対象     : {', '.join(statuses)}", file=out)
        print(f"フォルダ名: {name_filter_text(args)}", file=out)
        print(f"期間     : {period_text(args)}", file=out)
    print("", file=out)

    if src == dst:
        raise MoveError("移動元と移動先が同じです")
    if not owner_has_results(s3, bucket, config, src):
        raise MoveError(f"移動元の checked / unchecked フォルダが見つかりません: {owner_prefix(config, src)}")
    create_dst = False
    if not owner_has_results(s3, bucket, config, dst):
        print_new_destination_warning(s3, bucket, config, dst, out)
        if not args.execute:
            print("ドライランのため作成しません。--execute で実行するときに作成するか確認します。", file=out)
        elif args.allow_new_destination:
            print("--allow-new-destination が指定されているため、移動時に作成します。", file=out)
            create_dst = True
        else:
            answer = input_func("移動先の checked / unchecked フォルダを新規作成して移動しますか? (yes/no): ")
            if answer.strip().lower() != "yes":
                print("中止しました。", file=out)
                return 1
            create_dst = True
        print("", file=out)

    # 計画の作成
    if entries is not None:
        plan = []
        for status, name, expected_count in entries:
            folder = load_folder(s3, bucket, config, src, status, name)
            if not folder.objects:
                folder.blocked = "移動元にフォルダがありません"
            elif expected_count is not None and expected_count != len(folder.objects):
                folder.blocked = (f"一覧作成後にファイル数が変わっています "
                                  f"({expected_count} → {len(folder.objects)})")
            plan.append(folder)
    else:
        plan = collect_folders(s3, bucket, config, src, statuses, args.folders, args.names)
        if args.folders:
            missing = sorted(set(args.folders) - {f.name for f in plan})
            if missing:
                raise MoveError(f"指定された結果フォルダが移動元にありません (PDF/CSV を含むもの): "
                                f"{', '.join(missing)}")
        plan = filter_period(plan, args, config)
    for f in plan:
        f.dst_prefix = f"{status_prefix(config, dst, f.status)}{f.name}/"
        if not f.blocked and prefix_exists(s3, bucket, f.dst_prefix):
            f.blocked = "移動先に同名フォルダがあります"

    if not plan:
        print("移動対象の結果フォルダはありません。", file=out)
        return 0

    blocked = [f for f in plan if f.blocked]
    if entries is not None and args.execute:
        # 一覧 CSV での実行は番号選択がないため、移動対象は最終確認でだけ表示する
        if blocked:
            print("移動できないため対象外になる結果フォルダ:", file=out)
            print_plan(blocked, config, out)
            print("", file=out)
    else:
        print("移動対象の結果フォルダ:", file=out)
        print_plan(plan, config, out)
        print("", file=out)
    if blocked:
        print(f"※ {len(blocked)} フォルダは移動できないため対象外になります。", file=out)

    if not args.execute:
        entries = dry_run_report(plan, config, out)
        meta = {"bucket": bucket, "dry_run": True,
                "src": {"identity_id": src.identity_id, "user_id": src.user_id},
                "dst": {"identity_id": dst.identity_id, "user_id": dst.user_id},
                "list_file": args.list, "created": now_iso()}
        path = write_log(config.get("log_dir", "logs"), meta, entries, kind="dryrun")
        print(f"ドライランレポート: {path}", file=out)
        print("", file=out)
        print("ドライランのため S3 は変更していません。内容に問題がなければ、"
              "同じコマンドの --dry-run を --execute に替えて (または --execute を付けて) 実行してください。",
              file=out)
        return 0

    if len(blocked) == len(plan):
        print("移動できる結果フォルダがありません。", file=out)
        return 1

    # 1. 対象の選択 (一覧ファイル指定時は一覧の全フォルダ)
    if entries is not None:
        targets = [f for f in plan if not f.blocked]
    else:
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
    print(f"  件数  : {len(targets)} フォルダ / {sum(len(f.objects) for f in targets)} ファイル / "
          f"{format_size(sum(f.total_size for f in targets))}", file=out)
    if create_dst:
        print(f"  ※ 移動先フォルダを新規作成します: "
              f"{', '.join(status_prefix(config, dst, st) for st in STATUSES)}", file=out)
    print("=" * 70, file=out)
    print_plan(targets, config, out)
    print("", file=out)

    typed = input_func("確認のため、移動先のユーザーID を入力してください: ")
    if typed.strip() != dst.user_id:
        print("移動先のユーザーID が一致しません。中止しました。", file=out)
        return 1
    typed = input_func("確認のため、移動先の Identity ID を全体で入力してください"
                       " (例: ap-northeast-1:xxxxxxxx-...): ")
    if not identity_confirmed(typed, dst.identity_id):
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
            "list_file": args.list,
            "started": now_iso(),
            "folders": [{"status": f.status, "name": f.name, "src": f.src_prefix,
                         "dst": f.dst_prefix} for f in targets]}
    failed = None
    done = 0
    try:
        if create_dst:
            for key in create_destination_folders(s3, bucket, config, dst):
                log.append({"action": "create_folder", "key": key, "time": now_iso()})
                print(f"フォルダを作成しました: {key}", file=out)
        for i, f in enumerate(targets, 1):
            print(f"移動中 ({i}/{len(targets)}): [{f.status}] {f.name} ...", end=" ", file=out, flush=True)
            try:
                move_folder(s3, bucket, f, log)
                done += 1
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
        print(f"エラーのため処理を中断しました ({done}/{len(targets)} フォルダ完了): {e}", file=out)
        print(f"失敗したフォルダ [{f.status}] {f.name} の移動元ファイルは削除されていません。"
              "移動先にコピー済みのファイルは操作ログで確認してください。", file=out)
        return 2
    print(f"{len(targets)} フォルダの移動が完了しました。", file=out)
    return 0


# ---------------------------------------------------------------- CLI

def parse_args(argv):
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default="config.json", help="設定ファイル (既定: config.json)")
    common.add_argument("--bucket", help="バケット名 (設定ファイルより優先)")
    common.add_argument("--profile", help="AWS プロファイル名 (設定ファイルの profile より優先)")
    common.add_argument("--region", help="AWS リージョン")

    period = argparse.ArgumentParser(add_help=False)
    period.add_argument("--status", choices=STATUSES + ("both",), default="both",
                        help="対象フォルダ (checked / unchecked / both、既定: both)")
    period.add_argument("--since", type=parse_date, help="アップロード日がこの日以降 (YYYY-MM-DD)")
    period.add_argument("--until", type=parse_date, help="アップロード日がこの日以前 (YYYY-MM-DD)")
    period.add_argument("--folder", action="append", dest="folders",
                        help="結果フォルダ名で絞り込む (完全一致。例: SAT0202301。"
                             "複数指定・カンマ区切り可)")
    period.add_argument("--name", action="append", dest="names",
                        help="結果フォルダ名をワイルドカードで絞り込む (* は任意の文字列、? は任意の 1 文字。"
                             "例: 'SAT2026*'。複数指定可。大文字小文字は区別しない)")
    period.add_argument("--date-basis", choices=("last", "first"), default="last",
                        help="期間判定に使う日付: フォルダ内の最終 (last) / 最初 (first) のアップロード日 "
                             "(既定: last)")

    p = argparse.ArgumentParser(
        description="S3 上の結果フォルダ (PDF / CSV) を検索・一覧表示し、"
                    "別のユーザーID / Identity ID のフォルダへ確認しながら移動します。")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("search", parents=[common], help="ユーザーID / Identity ID で対象フォルダを検索")
    sp.add_argument("--user-id", help="ユーザーID (例: 000002)")
    sp.add_argument("--identity-id", help="Identity ID (例: ap-northeast-1:05efdd85-...)")

    lp = sub.add_parser("list", parents=[common, period],
                        help="移動対象となる結果フォルダの一覧 (アップロード日付き) を表示・保存")
    lp.add_argument("--user-id", help="ユーザーID")
    lp.add_argument("--identity-id", help="Identity ID")
    lp.add_argument("--output", "-o", help="一覧を CSV ファイルに保存する")
    lp.add_argument("--sort", choices=("name", "last", "first"), default="name",
                    help="並び順: name (状態・フォルダ名順、既定) / last (最終アップロード日順) / "
                         "first (最初のアップロード日順)")

    mp = sub.add_parser("move", parents=[common, period], help="結果フォルダを確認しながら移動")
    mp.add_argument("--src-user-id", help="移動元のユーザーID")
    mp.add_argument("--src-identity-id", help="移動元の Identity ID")
    mp.add_argument("--dst-user-id", help="移動先のユーザーID")
    mp.add_argument("--dst-identity-id", help="移動先の Identity ID")
    mp.add_argument("--list", help="list -o で保存した一覧 CSV に含まれるフォルダを一括移動する")
    mp.add_argument("--allow-new-destination", action="store_true",
                    help="移動先の checked / unchecked フォルダが無い場合に、確認なしで作成する "
                         "(通常は使わず、実行時の確認で yes と答えること)")
    mode = mp.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true",
                      help="ドライラン: S3 を変更せず、移動されるファイルの一覧とレポートを出力する (既定)")
    mode.add_argument("--execute", action="store_true",
                      help="実際に移動する。省略時はドライラン")

    up = sub.add_parser("undo", parents=[common], help="move の操作ログを使って移動を元に戻す")
    up.add_argument("--log", required=True, help="元に戻す move の操作ログ (logs/move_*.json)")
    umode = up.add_mutually_exclusive_group()
    umode.add_argument("--dry-run", action="store_true", help="ドライラン (既定): 戻す内容の表示のみ")
    umode.add_argument("--execute", action="store_true", help="実際に元に戻す")

    args = p.parse_args(argv)
    if hasattr(args, "folders"):
        args.folders = split_names(args.folders)
        args.names = split_names(args.names)
    if args.command in ("search", "list") and not (args.user_id or args.identity_id):
        p.error("--user-id か --identity-id の少なくとも一方を指定してください")
    if args.command == "move":
        if not (args.dst_user_id or args.dst_identity_id):
            p.error("--dst-user-id か --dst-identity-id の少なくとも一方を指定してください")
        if args.list and (args.folders or args.names or args.since or args.until
                          or args.status != "both"):
            p.error("--list と --folder / --name / --since / --until / --status は同時に指定できません")
    if getattr(args, "since", None) and getattr(args, "until", None) and args.since > args.until:
        p.error("--since は --until 以前の日付を指定してください")
    return args


def make_client(args, config):
    import boto3
    profile = args.profile or config.get("profile")
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    return session.client("s3", region_name=args.region or config.get("region"))


AWS_ERROR_HINTS = {
    "SignatureDoesNotMatch": (
        "AWS の認証に失敗しました (シークレットアクセスキーがアクセスキーID と一致しません)。",
        "aws configure --profile {profile} でアクセスキーID とシークレットアクセスキーを入力し直してください "
        "(コピー時の前後の空白・欠けに注意)。\n"
        "~/.aws/credentials の [{profile}] に同じ設定が重複していないかも確認してください。"),
    "InvalidAccessKeyId": (
        "AWS の認証に失敗しました (アクセスキーID が存在しないか無効です)。",
        "aws configure --profile {profile} で正しいアクセスキーを設定してください。"),
    "InvalidClientTokenId": (
        "AWS の認証に失敗しました (アクセスキーID が無効です)。",
        "aws configure --profile {profile} で正しいアクセスキーを設定してください。"),
    "ExpiredToken": (
        "AWS の認証情報の有効期限が切れています。",
        "SSO の場合は aws sso login --profile {profile} を実行してください。"),
    "RequestTimeTooSkewed": (
        "PC の時刻が実際の時刻とずれているため、AWS に拒否されました。",
        "PC の時刻を合わせてください (WSL の場合は sudo hwclock -s、または WSL を再起動: wsl --shutdown)。"),
    "AccessDenied": (
        "S3 へのアクセス権限がありません。",
        "プロファイル {profile} のユーザー / ロールに、バケット {bucket} の private/ 以下への "
        "s3:ListBucket / GetObject / PutObject / DeleteObject 権限があるか管理者に確認してください。"),
    "NoSuchBucket": (
        "バケットが見つかりません。",
        "設定ファイルの bucket ({bucket}) と、接続先の AWS アカウント (プロファイル {profile}) を確認してください。"),
}


def explain_aws_error(e, profile, bucket):
    """boto3 / botocore の例外を、原因と対処を書いた MoveError に変換する。"""
    from botocore import exceptions as bex
    no_profile = not profile
    profile = profile or "default"
    if isinstance(e, bex.ClientError):
        code = e.response.get("Error", {}).get("Code", "")
        summary, hint = AWS_ERROR_HINTS.get(code, (f"AWS がエラーを返しました ({code})。", ""))
        msg = f"{summary}\n  詳細: {e}"
    elif isinstance(e, bex.ProfileNotFound):
        msg = f"AWS プロファイル {profile} が見つかりません。"
        hint = "aws configure --profile {profile} でプロファイルを作成するか、設定ファイルの profile を確認してください。"
    elif isinstance(e, (bex.NoCredentialsError, bex.PartialCredentialsError)):
        msg = "AWS の認証情報が見つかりません。"
        hint = "aws configure --profile {profile} で認証情報を設定してください。"
    elif isinstance(e, (bex.SSOError, bex.TokenRetrievalError)):
        msg = "SSO のログインが必要です。"
        hint = "aws sso login --profile {profile} を実行してください。"
    else:
        msg = f"AWS への接続でエラーが発生しました: {e}"
        hint = ""
    lines = [msg, f"  プロファイル: {profile}{' (設定ファイルに profile がないため既定を使用)' if no_profile else ''}"
                  f" / バケット: {bucket}"]
    if no_profile:
        lines.append('対処: 設定ファイルに "profile": "プロファイル名" を追加するか、'
                     "--profile プロファイル名 を付けて実行してください。")
    elif hint:
        lines.append("対処: " + hint.format(profile=profile, bucket=bucket))
    if not no_profile:
        lines.append(f"接続確認: aws sts get-caller-identity --profile {profile}")
    return MoveError("\n".join(lines))


COMMANDS = {"search": cmd_search, "list": cmd_list, "move": cmd_move, "undo": cmd_undo}


def run(argv=None, s3=None, input_func=input, out=sys.stdout):
    args = parse_args(argv)
    config = load_config(args.config)
    bucket = args.bucket or config.get("bucket")
    if not bucket:
        raise MoveError("バケット名が指定されていません (--bucket または設定ファイルの bucket)")
    from botocore.exceptions import BotoCoreError, ClientError
    try:
        s3 = s3 or make_client(args, config)
        return COMMANDS[args.command](args, config, s3, bucket, input_func, out)
    except (BotoCoreError, ClientError) as e:
        raise explain_aws_error(e, args.profile or config.get("profile"), bucket) from e


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
