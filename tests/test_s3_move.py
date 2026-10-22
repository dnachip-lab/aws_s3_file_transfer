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
        ]:
            s3.put_object(Bucket=BUCKET, Key=key, Body=b"content-" + key.encode())
        yield s3, str(config_path), tmp_path


def keys(s3):
    resp = s3.list_objects_v2(Bucket=BUCKET)
    return sorted(o["Key"] for o in resp.get("Contents", []))


def run(s3, config, args, answers=()):
    answers = iter(answers)
    out = io.StringIO()
    code = s3_move.run(["--config", config] + args, s3=s3,
                       input_func=lambda _prompt: next(answers), out=out)
    return code, out.getvalue()


BASE = ["--src-user-id", "000002", "--dst-user-id", "000003"]
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
    args = ["--src-identity-id", ID_A, "--dst-identity-id", ID_B, "--status", "unchecked", "--execute"]
    code, out = run(s3, config, args, ["all"] + CONFIRM)
    assert code == 0, out
    k = keys(s3)
    assert f"private/{ID_B}/000003/unchecked/SAT0202303/data.csv" in k
    assert f"private/{ID_A}/000002/checked/SAT0202301/report.pdf" in k  # checked はそのまま


def test_identity_with_multiple_users_requires_user_id(env):
    s3, config, _ = env
    with pytest.raises(s3_move.MoveError, match="複数のユーザーID"):
        run(s3, config, ["--src-user-id", "000002", "--dst-identity-id", ID_C])


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
    with pytest.raises(s3_move.MoveError, match="移動先のユーザーID が見つかりません"):
        run(s3, config, ["--src-user-id", "000002", "--dst-user-id", "00003"])


def test_missing_destination_folder_aborts(env):
    s3, config, _ = env
    args = ["--src-user-id", "000002", "--dst-identity-id", ID_B, "--dst-user-id", "000099"]
    with pytest.raises(s3_move.MoveError, match="移動先の checked / unchecked フォルダが見つかりません"):
        run(s3, config, args)


def test_same_source_and_destination_aborts(env):
    s3, config, _ = env
    with pytest.raises(s3_move.MoveError, match="同じ"):
        run(s3, config, ["--src-user-id", "000002", "--dst-identity-id", ID_A])


def test_conflicting_folder_cannot_be_selected(env):
    s3, config, _ = env
    code, out = run(s3, config, BASE + ["--execute"], ["2", "q"])
    assert code == 1
    assert "選択できません" in out
    assert "移動不可" in out


def test_invalid_identity_id_rejected(env):
    s3, config, _ = env
    with pytest.raises(s3_move.MoveError, match="形式が正しくありません"):
        run(s3, config, ["--src-user-id", "000002", "--dst-identity-id", "05efdd85-9e44"])


def test_invalid_user_id_rejected(env):
    s3, config, _ = env
    with pytest.raises(s3_move.MoveError, match="使用できない"):
        run(s3, config, ["--src-user-id", "000002", "--dst-user-id", "../000003"])
