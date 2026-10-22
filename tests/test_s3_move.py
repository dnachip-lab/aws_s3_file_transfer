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


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-northeast-1")
    config = {
        "bucket": BUCKET,
        "region": "ap-northeast-1",
        "id_types": {
            "user_id": "results/users/{id}/{status}/",
            "identified_id": "results/identified/{id}/{status}/",
        },
        "expected_extensions": [".pdf", ".csv"],
        "log_dir": str(tmp_path / "logs"),
    }
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    with mock_aws():
        s3 = boto3.client("s3", region_name="ap-northeast-1")
        s3.create_bucket(Bucket=BUCKET,
                         CreateBucketConfiguration={"LocationConstraint": "ap-northeast-1"})
        for key in [
            "results/users/U001/checked/R1/report.pdf",
            "results/users/U001/checked/R1/data.csv",
            "results/users/U001/checked/R2/report.pdf",
            "results/users/U001/unchecked/R3/data.csv",
            "results/users/U002/checked/EXIST/report.pdf",
            "results/users/U002/unchecked/R9/data.csv",
            "results/identified/X100/checked/R2/report.pdf",
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


BASE = ["--src-type", "user_id", "--src-id", "U001", "--dst-type", "user_id", "--dst-id", "U002"]


def test_dry_run_changes_nothing(env):
    s3, config, _ = env
    before = keys(s3)
    code, out = run(s3, config, BASE)
    assert code == 0
    assert "ドライラン" in out
    assert "results/users/U002/checked/R1/" in out
    assert keys(s3) == before


def test_execute_moves_selected_folders(env):
    s3, config, tmp = env
    code, out = run(s3, config, BASE + ["--execute"], ["1,3", "U002", "yes"])
    assert code == 0, out
    k = keys(s3)
    assert "results/users/U002/checked/R1/report.pdf" in k
    assert "results/users/U002/checked/R1/data.csv" in k
    assert "results/users/U002/unchecked/R3/data.csv" in k
    assert not any(x.startswith("results/users/U001/checked/R1/") for x in k)
    assert "results/users/U001/checked/R2/report.pdf" in k  # 未選択は残る
    body = s3.get_object(Bucket=BUCKET, Key="results/users/U002/checked/R1/data.csv")["Body"].read()
    assert body == b"content-results/users/U001/checked/R1/data.csv"
    logs = os.listdir(tmp / "logs")
    assert len(logs) == 1


def test_status_keeps_checked_unchecked(env):
    s3, config, _ = env
    code, _ = run(s3, config, BASE + ["--status", "unchecked", "--execute"], ["all", "U002", "yes"])
    assert code == 0
    k = keys(s3)
    assert "results/users/U002/unchecked/R3/data.csv" in k
    assert "results/users/U001/checked/R1/report.pdf" in k


def test_wrong_confirmation_id_aborts(env):
    s3, config, _ = env
    before = keys(s3)
    code, out = run(s3, config, BASE + ["--execute"], ["all", "U003", "yes"])
    assert code == 1
    assert "一致しません" in out
    assert keys(s3) == before


def test_not_yes_aborts(env):
    s3, config, _ = env
    before = keys(s3)
    code, _ = run(s3, config, BASE + ["--execute"], ["all", "U002", "y"])
    assert code == 1
    assert keys(s3) == before


def test_missing_destination_id_aborts(env):
    s3, config, _ = env
    args = ["--src-type", "user_id", "--src-id", "U001",
            "--dst-type", "user_id", "--dst-id", "U0002", "--execute"]
    with pytest.raises(s3_move.MoveError, match="移動先 ID のフォルダが見つかりません"):
        run(s3, config, args)


def test_same_source_and_destination_aborts(env):
    s3, config, _ = env
    args = ["--src-type", "user_id", "--src-id", "U001", "--dst-type", "user_id", "--dst-id", "U001"]
    with pytest.raises(s3_move.MoveError, match="同じ"):
        run(s3, config, args)


def test_conflicting_folder_cannot_be_selected(env):
    s3, config, _ = env
    args = ["--src-type", "user_id", "--src-id", "U001",
            "--dst-type", "identified_id", "--dst-id", "X100", "--execute"]
    # R2 (2番) は移動先に既にあるので選択エラー → 再入力で q
    code, out = run(s3, config, args, ["2", "q"])
    assert code == 1
    assert "選択できません" in out
    # all は衝突フォルダを除外する
    code, out = run(s3, config, args, ["all", "X100", "yes"])
    assert code == 0, out
    k = keys(s3)
    assert "results/identified/X100/checked/R1/report.pdf" in k
    assert "results/users/U001/checked/R2/report.pdf" in k  # 衝突したものは移動されない


def test_invalid_id_rejected(env):
    s3, config, _ = env
    args = ["--src-type", "user_id", "--src-id", "U001", "--dst-type", "user_id", "--dst-id", "../U002"]
    with pytest.raises(s3_move.MoveError, match="使用できない"):
        run(s3, config, args)


def test_folder_option_unknown_name(env):
    s3, config, _ = env
    with pytest.raises(s3_move.MoveError, match="ありません"):
        run(s3, config, BASE + ["--folder", "NOPE"])
