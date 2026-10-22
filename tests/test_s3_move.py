import csv
import datetime
import io
import json
import os
import sys

import boto3
import pytest
from moto import mock_aws

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import s3_move  # noqa: E402

BUCKET = "test-bucket"
ID_A = "ap-northeast-1:05efdd85-9e44-4765-90e4-d8a9138bb7da"
ID_B = "ap-northeast-1:11111111-2222-3333-4444-555555abcdef"
ID_C = "ap-northeast-1:99999999-8888-7777-6666-000000000001"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    config = {
        "bucket": BUCKET,
        "region": "ap-northeast-1",
        "root_prefix": "private/",
        "log_dir": str(tmp_path / "logs"),
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    with mock_aws():
        s3 = boto3.client("s3", region_name="ap-northeast-1")
        s3.create_bucket(Bucket=BUCKET,
                         CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"})
        for key in [
            f"private/{ID_A}/000002/checked/SAT0202301/report.pdf",
            f"private/{ID_A}/000002/checked/SAT0202301/data.csv",
            f"private/{ID_A}/000002/checked/SAT0202302/report.pdf",
            f"private/{ID_A}/000002/unchecked/SAT0202303/data.csv",
            f"private/{ID_B}/000003/checked/SAT0202302/report.pdf",
            f"private/{ID_B}/000003/unchecked/OLD/data.csv",
            f"private/{ID_C}/000004/checked/X/a.pdf",
            f"private/{ID_C}/000005/checked/Y/a.pdf",
            f"private/{ID_A}/000002/checked/EMPTY/memo.txt",  # PDF/CSV なし → 対象外
        ]:
            s3.put_object(Bucket=BUCKET, Key=key, Body=b"content-" + key.encode())
        yield s3, str(config_path), tmp_path


def keys(s3):
    resp = s3.list_objects_v2(Bucket=BUCKET)
    return sorted(o["Key"] for o in resp.get("Contents", []))


def run(s3, config, args, answers=()):
    answers = iter(answers)
    out = io.StringIO()
    code = s3_move.run(args[:1] + ["--config", config] + args[1:], s3=s3,
                       input_func=lambda _prompt: next(answers), out=out)
    return code, out.getvalue()


BASE = ["move", "--src-user-id", "000002", "--dst-user-id", "000003"]
CONFIRM = ["000003", "abcdef", "yes"]  # ユーザーID, Identity ID 末尾6文字, yes


def test_dry_run_resolves_identity_and_changes_nothing(env):
    s3, config, _ = env
    before = keys(s3)
    code, out = run(s3, config, BASE)
    assert code == 0
    assert "ドライラン" in out
    assert ID_A in out and ID_B in out
    assert f"private/{ID_B}/000003/checked/SAT0202301/" in out
    assert keys(s3) == before


def test_execute_moves_selected_folders(env):
    s3, config, tmp = env
    # 1: checked/SAT0202301, 2: checked/SAT0202302 (衝突), 3: unchecked/SAT0202303
    code, out = run(s3, config, BASE + ["--execute"], ["1,3"] + CONFIRM)
    assert code == 0, out
    k = keys(s3)
    assert f"private/{ID_B}/000003/checked/SAT0202301/report.pdf" in k
    assert f"private/{ID_B}/000003/checked/SAT0202301/data.csv" in k
    assert f"private/{ID_B}/000003/unchecked/SAT0202303/data.csv" in k
    assert not any(x.startswith(f"private/{ID_A}/000002/checked/SAT0202301/") for x in k)
    assert f"private/{ID_A}/000002/checked/SAT0202302/report.pdf" in k
    body = s3.get_object(Bucket=BUCKET,
                         Key=f"private/{ID_B}/000003/checked/SAT0202301/data.csv")["Body"].read()
    assert body == f"content-private/{ID_A}/000002/checked/SAT0202301/data.csv".encode()
    log_files = os.listdir(tmp / "logs")
    assert len(log_files) == 1
    log = json.loads((tmp / "logs" / log_files[0]).read_text())
    assert log["meta"]["dst"] == {"identity_id": ID_B, "user_id": "000003"}


def test_identity_id_only(env):
    s3, config, _ = env
    args = ["move", "--src-identity-id", ID_A, "--dst-identity-id", ID_B, "--status", "unchecked", "--execute"]
    code, out = run(s3, config, args, ["all"] + CONFIRM)
    assert code == 0, out
    k = keys(s3)
    assert f"private/{ID_B}/000003/unchecked/SAT0202303/data.csv" in k
    assert f"private/{ID_A}/000002/checked/SAT0202301/report.pdf" in k  # checked はそのまま


def test_identity_with_multiple_users_requires_user_id(env):
    s3, config, _ = env
    with pytest.raises(s3_move.MoveError, match="該当するユーザーID が 2 件あります"):
        run(s3, config, ["move", "--src-user-id", "000002", "--dst-identity-id", ID_C])


def test_folder_option(env):
    s3, config, _ = env
    code, out = run(s3, config, BASE + ["--folder", "SAT0202301", "--execute"], ["all"] + CONFIRM)
    assert code == 0, out
    k = keys(s3)
    assert f"private/{ID_B}/000003/checked/SAT0202301/report.pdf" in k
    assert f"private/{ID_A}/000002/unchecked/SAT0202303/data.csv" in k


def test_wrong_user_id_confirmation_aborts(env):
    s3, config, _ = env
    before = keys(s3)
    code, out = run(s3, config, BASE + ["--execute"], ["all", "000004", "abcdef", "yes"])
    assert code == 1
    assert "ユーザーID が一致しません" in out
    assert keys(s3) == before


def test_wrong_identity_confirmation_aborts(env):
    s3, config, _ = env
    before = keys(s3)
    code, out = run(s3, config, BASE + ["--execute"], ["all", "000003", "bb7da0", "yes"])
    assert code == 1
    assert "Identity ID が一致しません" in out
    assert keys(s3) == before


def test_not_yes_aborts(env):
    s3, config, _ = env
    before = keys(s3)
    code, _ = run(s3, config, BASE + ["--execute"], ["all", "000003", "abcdef", "y"])
    assert code == 1
    assert keys(s3) == before


def test_unknown_destination_user_aborts(env):
    s3, config, _ = env
    with pytest.raises(s3_move.MoveError, match="移動先のユーザーID 00003 が見つかりません"):
        run(s3, config, ["move", "--src-user-id", "000002", "--dst-user-id", "00003"])


def test_missing_destination_folder_aborts(env):
    s3, config, _ = env
    args = ["move", "--src-user-id", "000002", "--dst-identity-id", ID_B, "--dst-user-id", "000099"]
    with pytest.raises(s3_move.MoveError, match="移動先の checked / unchecked フォルダが見つかりません"):
        run(s3, config, args)


def test_same_source_and_destination_aborts(env):
    s3, config, _ = env
    with pytest.raises(s3_move.MoveError, match="同じ"):
        run(s3, config, ["move", "--src-user-id", "000002", "--dst-identity-id", ID_A])


def test_conflicting_folder_cannot_be_selected(env):
    s3, config, _ = env
    code, out = run(s3, config, BASE + ["--execute"], ["2", "q"])
    assert code == 1
    assert "選択できません" in out
    assert "移動不可" in out


def test_invalid_identity_id_rejected(env):
    s3, config, _ = env
    with pytest.raises(s3_move.MoveError, match="形式が正しくありません"):
        run(s3, config, ["move", "--src-user-id", "000002", "--dst-identity-id", "05efdd85-9e44"])


def test_invalid_user_id_rejected(env):
    s3, config, _ = env
    with pytest.raises(s3_move.MoveError, match="使用できない"):
        run(s3, config, ["move", "--src-user-id", "000002", "--dst-user-id", "../000003"])


JST = datetime.timezone(datetime.timedelta(hours=9))
TODAY = datetime.datetime.now(JST).date()
YESTERDAY = TODAY - datetime.timedelta(days=1)


def test_folder_without_pdf_csv_is_excluded(env):
    s3, config, _ = env
    code, out = run(s3, config, BASE)
    assert "EMPTY" not in out


def test_search_by_user_id(env):
    s3, config, _ = env
    code, out = run(s3, config, ["search", "--user-id", "000002"])
    assert code == 0
    assert "1 件見つかりました" in out
    assert ID_A in out
    assert "移動対象フォルダ checked: 2, unchecked: 1 / 最終アップロード " in out


def test_search_by_identity_id_lists_all_users(env):
    s3, config, _ = env
    code, out = run(s3, config, ["search", "--identity-id", ID_C])
    assert code == 0
    assert "2 件見つかりました" in out
    assert "000004" in out and "000005" in out


def test_search_not_found(env):
    s3, config, _ = env
    code, out = run(s3, config, ["search", "--user-id", "999999"])
    assert code == 1


def test_list_shows_upload_dates_and_saves_csv(env):
    s3, config, tmp = env
    path = tmp / "list.csv"
    code, out = run(s3, config, ["list", "--user-id", "000002", "-o", str(path)])
    assert code == 0, out
    assert "SAT0202301" in out and "SAT0202303" in out
    assert TODAY.isoformat() in out
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    assert [(r["status"], r["folder"]) for r in rows] == [
        ("checked", "SAT0202301"), ("checked", "SAT0202302"), ("unchecked", "SAT0202303")]
    r = rows[0]
    assert r["identity_id"] == ID_A and r["user_id"] == "000002"
    assert (r["pdf_count"], r["csv_count"], r["file_count"]) == ("1", "1", "2")
    assert r["last_uploaded"].startswith(TODAY.isoformat())


def test_list_period_filter(env):
    s3, config, _ = env
    code, out = run(s3, config, ["list", "--user-id", "000002", "--since", TODAY.isoformat()])
    assert "SAT0202301" in out
    code, out = run(s3, config, ["list", "--user-id", "000002", "--until", YESTERDAY.isoformat()])
    assert "該当する結果フォルダはありません" in out


def test_in_period_basis():
    utc = datetime.timezone.utc
    folder = s3_move.ResultFolder("checked", "R", "p/", objects=[
        {"Key": "p/a.pdf", "Size": 1, "LastModified": datetime.datetime(2025, 1, 10, 0, 0, tzinfo=utc)},
        {"Key": "p/a.csv", "Size": 1, "LastModified": datetime.datetime(2025, 3, 31, 16, 0, tzinfo=utc)},
    ])
    d = datetime.date
    # 最終アップロードは JST で 2025-04-01 01:00
    assert s3_move.in_period(folder, d(2025, 4, 1), None, "last", JST)
    assert not s3_move.in_period(folder, None, d(2025, 3, 31), "last", JST)
    assert s3_move.in_period(folder, None, d(2025, 1, 10), "first", JST)
    assert not s3_move.in_period(folder, d(2025, 1, 11), None, "first", JST)


def test_move_with_period_filter(env):
    s3, config, _ = env
    before = keys(s3)
    code, out = run(s3, config, BASE + ["--until", YESTERDAY.isoformat(), "--execute"])
    assert code == 0
    assert "移動対象の結果フォルダはありません" in out
    assert keys(s3) == before


def test_bulk_move_from_list(env):
    s3, config, tmp = env
    path = tmp / "list.csv"
    run(s3, config, ["list", "--user-id", "000002", "-o", str(path)])
    # SAT0202302 の行を削除 (不要な行を消す運用)
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    path.write_text("\n".join(l for l in lines if "SAT0202302" not in l) + "\n", encoding="utf-8-sig")

    code, out = run(s3, config, ["move", "--list", str(path), "--dst-user-id", "000003"])
    assert code == 0 and "ドライラン" in out

    code, out = run(s3, config, ["move", "--list", str(path), "--dst-user-id", "000003", "--execute"],
                    CONFIRM)  # 番号選択なし
    assert code == 0, out
    k = keys(s3)
    assert f"private/{ID_B}/000003/checked/SAT0202301/report.pdf" in k
    assert f"private/{ID_B}/000003/unchecked/SAT0202303/data.csv" in k
    assert f"private/{ID_A}/000002/checked/SAT0202302/report.pdf" in k


def test_bulk_move_skips_changed_and_conflicting(env):
    s3, config, tmp = env
    path = tmp / "list.csv"
    run(s3, config, ["list", "--user-id", "000002", "-o", str(path)])
    s3.put_object(Bucket=BUCKET, Key=f"private/{ID_A}/000002/unchecked/SAT0202303/new.pdf", Body=b"x")
    code, out = run(s3, config, ["move", "--list", str(path), "--dst-user-id", "000003", "--execute"],
                    CONFIRM)
    assert code == 0, out
    assert "ファイル数が変わっています" in out
    assert "移動先に同名フォルダがあります" in out
    k = keys(s3)
    assert f"private/{ID_B}/000003/checked/SAT0202301/report.pdf" in k
    assert f"private/{ID_A}/000002/unchecked/SAT0202303/data.csv" in k  # 変化したので移動しない
    assert f"private/{ID_A}/000002/checked/SAT0202302/report.pdf" in k  # 衝突したので移動しない


def test_bulk_move_list_with_multiple_sources_rejected(env):
    s3, config, tmp = env
    path = tmp / "list.csv"
    path.write_text(
        "identity_id,user_id,status,folder\n"
        f"{ID_A},000002,checked,SAT0202301\n"
        f"{ID_B},000003,checked,SAT0202302\n", encoding="utf-8")
    with pytest.raises(s3_move.MoveError, match="複数の移動元"):
        run(s3, config, ["move", "--list", str(path), "--dst-identity-id", ID_C, "--dst-user-id", "000004"])


def test_bulk_move_src_mismatch_rejected(env):
    s3, config, tmp = env
    path = tmp / "list.csv"
    run(s3, config, ["list", "--user-id", "000002", "-o", str(path)])
    with pytest.raises(s3_move.MoveError, match="一致しません"):
        run(s3, config, ["move", "--list", str(path), "--src-user-id", "000009", "--dst-user-id", "000003"])


def test_dry_run_flag_lists_files_and_writes_report(env):
    s3, config, tmp = env
    before = keys(s3)
    code, out = run(s3, config, BASE + ["--dry-run"])
    assert code == 0
    assert keys(s3) == before
    assert f"→ private/{ID_B}/000003/checked/SAT0202301/data.csv" in out
    assert "移動予定: 2 フォルダ / 3 ファイル" in out
    assert "対象外  : 1 フォルダ" in out
    reports = [f for f in os.listdir(tmp / "logs") if f.startswith("dryrun_")]
    assert len(reports) == 1
    report = json.loads((tmp / "logs" / reports[0]).read_text())
    assert report["meta"]["dry_run"] is True
    planned = [o for o in report["operations"] if o["action"] == "planned_move"]
    skipped = [o for o in report["operations"] if o["action"] == "skip"]
    assert len(planned) == 3 and len(skipped) == 1


def test_dry_run_and_execute_are_exclusive(env):
    s3, config, _ = env
    with pytest.raises(SystemExit):
        run(s3, config, BASE + ["--dry-run", "--execute"])


def test_aws_error_is_explained(env):
    s3, config, _ = env
    from botocore.exceptions import ClientError
    from unittest import mock
    err = ClientError({"Error": {"Code": "SignatureDoesNotMatch", "Message": "bad sig"}}, "ListObjectsV2")
    with mock.patch.object(s3, "get_paginator", side_effect=err):
        with pytest.raises(s3_move.MoveError) as ei:
            run(s3, config, ["search", "--profile", "dcr_iijima_dev", "--user-id", "000002"])
    msg = str(ei.value)
    assert "シークレットアクセスキー" in msg
    assert "プロファイル: dcr_iijima_dev" in msg
    assert "aws configure --profile" in msg
    assert "aws sts get-caller-identity" in msg


def test_profile_not_found_is_explained(env, tmp_path):
    _, config, _ = env
    cfg = json.loads(open(config).read())
    cfg["profile"] = "no_such_profile_xyz"
    path = tmp_path / "c2.json"
    path.write_text(json.dumps(cfg))
    with pytest.raises(s3_move.MoveError, match="プロファイル no_such_profile_xyz が見つかりません"):
        s3_move.run(["search", "--config", str(path), "--user-id", "000002"], out=io.StringIO())


def test_aws_error_without_profile_suggests_setting_it(env):
    s3, config, _ = env
    from botocore.exceptions import ClientError
    from unittest import mock
    err = ClientError({"Error": {"Code": "SignatureDoesNotMatch", "Message": "bad sig"}}, "ListObjectsV2")
    with mock.patch.object(s3, "get_paginator", side_effect=err):
        with pytest.raises(s3_move.MoveError) as ei:
            run(s3, config, ["search", "--user-id", "000002"])
    msg = str(ei.value)
    assert "設定ファイルに profile がないため既定を使用" in msg
    assert '"profile"' in msg and "--profile" in msg


def test_list_sort_and_date_summary(env):
    s3, config, _ = env
    utc = datetime.timezone.utc
    dates = {
        "SAT0202301": datetime.datetime(2026, 9, 5, 3, 0, tzinfo=utc),
        "SAT0202302": datetime.datetime(2026, 8, 20, 3, 0, tzinfo=utc),
        "SAT0202303": datetime.datetime(2026, 9, 30, 16, 0, tzinfo=utc),  # JST 2026-10-01
    }
    real = s3_move.list_objects

    def fake(s3_, bucket, prefix):
        objs = real(s3_, bucket, prefix)
        for o in objs:
            for name, dt in dates.items():
                if f"/{name}/" in o["Key"]:
                    o["LastModified"] = dt
        return objs

    from unittest import mock
    with mock.patch.object(s3_move, "list_objects", side_effect=fake):
        code, out = run(s3, config, ["list", "--user-id", "000002", "--sort", "last"])
    assert code == 0
    order = [n for n in ("SAT0202302", "SAT0202301", "SAT0202303")]
    positions = [out.index(n) for n in order]
    assert positions == sorted(positions)
    assert "最終アップロード日の範囲: 2026-08-20 〜 2026-10-01" in out
    assert "2026-08: 1, 2026-09: 1, 2026-10: 1" in out
    assert "--since YYYY-MM-DD" in out


def list_csv_folders(path):
    with open(path, encoding="utf-8-sig") as f:
        return [(r["status"], r["folder"]) for r in csv.DictReader(f)]


def test_list_filter_by_folder_names_and_save(env):
    s3, config, tmp = env
    path = tmp / "list.csv"
    code, out = run(s3, config, ["list", "--user-id", "000002",
                                 "--folder", "SAT0202301,SAT0202303", "-o", str(path)])
    assert code == 0, out
    assert "フォルダ名: フォルダ名 SAT0202301, SAT0202303" in out
    assert list_csv_folders(path) == [("checked", "SAT0202301"), ("unchecked", "SAT0202303")]


def test_list_filter_by_wildcard(env):
    s3, config, tmp = env
    path = tmp / "list.csv"
    code, out = run(s3, config, ["list", "--user-id", "000002", "--name", "sat020230[12]",
                                 "--status", "checked", "-o", str(path)])
    assert code == 0, out
    assert list_csv_folders(path) == [("checked", "SAT0202301"), ("checked", "SAT0202302")]


def test_list_folder_and_pattern_are_or(env):
    s3, config, tmp = env
    path = tmp / "list.csv"
    run(s3, config, ["list", "--user-id", "000002", "--folder", "SAT0202303",
                     "--name", "*01", "-o", str(path)])
    assert list_csv_folders(path) == [("checked", "SAT0202301"), ("unchecked", "SAT0202303")]


def test_list_missing_folder_warns(env):
    s3, config, _ = env
    code, out = run(s3, config, ["list", "--user-id", "000002", "--folder", "NOPE"])
    assert code == 0
    assert "指定されたフォルダが見つかりません" in out and "NOPE" in out


def test_move_with_wildcard(env):
    s3, config, _ = env
    code, out = run(s3, config, BASE + ["--name", "*2303", "--execute"], ["all"] + CONFIRM)
    assert code == 0, out
    k = keys(s3)
    assert f"private/{ID_B}/000003/unchecked/SAT0202303/data.csv" in k
    assert f"private/{ID_A}/000002/checked/SAT0202301/report.pdf" in k


def test_list_option_conflicts_with_name(env):
    s3, config, tmp = env
    with pytest.raises(SystemExit):
        run(s3, config, ["move", "--list", "x.csv", "--dst-user-id", "000003", "--name", "SAT*"])


def test_list_empty_result_explains_status(env):
    s3, config, _ = env
    code, out = run(s3, config, ["list", "--user-id", "000002", "--name", "*2303", "--status", "checked"])
    assert code == 0
    assert "該当する結果フォルダはありません" in out
    assert "フォルダ名の条件だけで絞り込んだ場合: 1 件 (checked: 0, unchecked: 1)" in out
    assert "--status の指定を見直してください" in out


def test_list_empty_result_explains_period(env):
    s3, config, _ = env
    code, out = run(s3, config, ["list", "--user-id", "000002", "--until", YESTERDAY.isoformat()])
    assert "絞り込みなしの場合: 3 件 (checked: 2, unchecked: 1)" in out
    assert "期間の指定を見直してください" in out


def test_table_header_alignment():
    assert s3_move.display_width("状態") == 4
    assert s3_move.ljust("状態", 9) == "状態     "


def test_ambiguous_user_id_lists_candidates(env):
    s3, config, _ = env
    s3.put_object(Bucket=BUCKET, Key=f"private/{ID_C}/000003/unchecked/Z/a.csv", Body=b"x")
    with pytest.raises(s3_move.MoveError) as ei:
        run(s3, config, ["move", "--src-user-id", "000002", "--dst-user-id", "000003"])
    msg = str(ei.value)
    assert "Identity ID が 2 件あります" in msg
    assert f"--dst-identity-id {ID_B}" in msg
    assert f"--dst-identity-id {ID_C}" in msg
    assert "移動対象フォルダ checked: 1, unchecked: 1" in msg  # ID_B 側
    assert "移動対象フォルダ checked: 0, unchecked: 1" in msg  # ID_C 側
    # Identity ID を指定すれば進める
    code, out = run(s3, config, ["move", "--src-user-id", "000002", "--dst-user-id", "000003",
                                 "--dst-identity-id", ID_C, "--dry-run"])
    assert code == 0, out
    assert f"private/{ID_C}/000003/checked/SAT0202301/" in out
