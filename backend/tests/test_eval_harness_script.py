"""The eval harness CLI's offline paths: cases, the estimate, score's exit codes, baseline promotion.

``run`` and ``cleanup`` need a deployed stack; their logic lives in
``apis.shared.evals`` and is tested there.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))

import eval_harness  # noqa: E402

MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _case(cid: str):
    return {
        "id": f"tool_selection.{cid}",
        "family": "tool_selection",
        "turns": [{"prompt": "q"}],
        "run": {"enabled_tools": ["calculator"]},
        "expect": {"tools": {"must_call": ["calculator"]}},
    }


@pytest.fixture
def golden(tmp_path: Path):
    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    (cases_dir / "tool_selection.json").write_text(
        json.dumps([_case(c) for c in "abcdefgh"]), encoding="utf-8"
    )
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    baselines = tmp_path / "baselines"
    return ["--cases-dir", str(cases_dir), "--fixtures-dir", str(fixtures), "--baselines-dir", str(baselines)], baselines


def _write_run(out: Path, failing: str = "", k: int = 3, samples: int = 3):
    out.mkdir(parents=True, exist_ok=True)
    (out / "meta.json").write_text(json.dumps({"model_id": MODEL, "k": k, "startedAt": "t", "user_id": "u"}),
                                   encoding="utf-8")
    with (out / "results.jsonl").open("w", encoding="utf-8") as fh:
        for cid in "abcdefgh":
            for s in range(samples):
                passed = cid not in failing
                fh.write(json.dumps({
                    "case_id": f"tool_selection.{cid}", "family": "tool_selection", "sample": s,
                    "model_id": MODEL, "passed": passed, "failures": [] if passed else ["did not call calculator"],
                    "final_message": "ok", "tool_calls": [],
                }) + "\n")


def test_cases_command_reports_the_committed_set(capsys):
    assert eval_harness.main(["cases"]) == 0
    out = capsys.readouterr().out
    assert "tool_selection" in out and "The eval user's role must grant" in out


def test_estimate_counts_every_turn_and_flags_an_unpriced_model():
    from apis.shared.evals.cases import parse_case

    case = parse_case({**_case("a"), "turns": [{"prompt": "1"}, {"prompt": "2"}]}, family="tool_selection")
    turns, dollars, note = eval_harness.estimate([(case, 0), (case, 1)], MODEL)
    assert turns == 4 and dollars > 0 and note == ""
    assert "priced at" in eval_harness.estimate([(case, 0)], "some.unpriced-model")[2]


def test_baseline_then_score_passes_then_fails_on_regression(tmp_path, golden, capsys):
    flags, baselines = golden
    run1 = tmp_path / "run1"
    _write_run(run1)
    assert eval_harness.main(flags + ["baseline", "--out", str(run1)]) == 0
    assert (baselines / eval_harness.baseline_filename(MODEL)).is_file()

    assert eval_harness.main(flags + ["score", "--out", str(run1)]) == 0
    assert "GATE: pass" in capsys.readouterr().out

    run2 = tmp_path / "run2"
    _write_run(run2, failing="abc")
    report = tmp_path / "report.json"
    assert eval_harness.main(
        flags + ["score", "--out", str(run2), "--explain", "tool_selection", "--json", str(report)]
    ) == 1
    out = capsys.readouterr().out
    assert "GATE: FAIL" in out and "LOST  tool_selection.a" in out
    assert json.loads(report.read_text())["gateFailed"] is True


def test_explain_reads_cost_rows_unless_told_not_to(tmp_path, golden, capsys, monkeypatch):
    flags, _ = golden
    run1 = tmp_path / "run1"
    _write_run(run1)
    assert eval_harness.main(flags + ["baseline", "--out", str(run1)]) == 0
    run2 = tmp_path / "run2"
    _write_run(run2, failing="a")
    capsys.readouterr()

    asked = []

    async def fake_calls(meta, rows, family):
        asked.append(family)
        return {}

    monkeypatch.setattr(eval_harness, "_family_calls", fake_calls)
    assert eval_harness.main(flags + ["score", "--out", str(run2), "--explain", "tool_selection"]) == 0
    out = capsys.readouterr().out
    assert asked == ["tool_selection"]
    assert "  calls:" in out and "[0] FAIL  unavailable" in out

    assert eval_harness.main(
        flags + ["score", "--out", str(run2), "--explain", "tool_selection", "--no-cost-rows"]
    ) == 0
    assert asked == ["tool_selection"]
    assert "calls:" not in capsys.readouterr().out


class TestScorePublish:
    """``score --publish`` writes one LEDGER#EVAL row keyed by the Runtime build (release-ledger §5.2)."""

    RUNTIME = "d5ead33e05f3e229"
    APP_API = "0a0b0c0d0e0f1011"

    def _runs(self, tmp_path, golden):
        flags, _ = golden
        run1 = tmp_path / "run1"
        _write_run(run1)
        assert eval_harness.main(flags + ["baseline", "--out", str(run1)]) == 0
        run2 = tmp_path / "run2"
        _write_run(run2, failing="abc")
        meta = json.loads((run2 / "meta.json").read_text())
        meta.update({"startedAt": "2026-11-07T04:00:00.000001Z", "prefix": "dev-example", "region": "us-west-2",
                     "appVersion": "1.33.0", "build": self.APP_API,
                     "imageTags": {"inference-api": self.RUNTIME, "app-api": self.APP_API}})
        (run2 / "meta.json").write_text(json.dumps(meta))
        return flags, run2

    @pytest.fixture
    def published(self, monkeypatch):
        from apis.shared.costs import ledger_publish

        rows = []

        def fake_put(row, **kwargs):
            rows.append(dict(row))
            return True

        monkeypatch.setattr(ledger_publish, "put_ledger_row", fake_put)
        return rows

    def test_keys_the_row_by_the_cost_rows_build_and_keeps_the_exit_code(self, tmp_path, golden, capsys,
                                                                        monkeypatch, published):
        flags, run2 = self._runs(tmp_path, golden)

        async def calls(meta, session_ids):
            return {"s": [{"appVersion": "1.33.0", "build": self.RUNTIME, "cost": {"total": 0.01}}]}

        monkeypatch.setattr(eval_harness, "_session_calls", calls)
        with (run2 / "results.jsonl").open() as fh:
            rows = [json.loads(line) for line in fh]
        with (run2 / "results.jsonl").open("w") as fh:
            for r in rows:
                fh.write(json.dumps({**r, "session_id": "s"}) + "\n")

        assert eval_harness.main(flags + ["score", "--out", str(run2), "--publish"]) == 1
        assert len(published) == 1
        row = published[0]
        assert (row["PK"], row["SK"]) == ("LEDGER#EVAL", "2026-11-07T04:00:00.000001Z")
        assert (row["build"], row["buildSource"]) == (self.RUNTIME, "cost_rows")
        assert row["families"]["tool_selection"]["lost"] == 3 and row["gateFailed"] is True
        assert row["track"] == "manual"
        assert row["cases"]["tool_selection.a"] == {
            "pass": False, "passes": 0, "samples": 3, "base": True,
            "codes": ["tool_missing"], "names": ["calculator"],
        }
        assert row["cases"]["tool_selection.d"] == {"pass": True, "passes": 3, "samples": 3, "base": True}
        assert "ledger: published LEDGER#EVAL" in capsys.readouterr().out

    def test_track_names_the_pass(self, tmp_path, golden, monkeypatch, published):
        flags, run2 = self._runs(tmp_path, golden)
        eval_harness.main(flags + ["score", "--out", str(run2), "--publish", "--track", "nightly"])
        assert published[0]["track"] == "nightly"
        with pytest.raises(SystemExit):
            eval_harness.main(flags + ["score", "--out", str(run2), "--publish", "--track", "hourly"])

    def test_without_cost_rows_it_falls_back_to_the_runtime_image_tag_never_app_apis_build(
        self, tmp_path, golden, monkeypatch, published
    ):
        flags, run2 = self._runs(tmp_path, golden)

        async def no_calls(meta, session_ids):
            return {}

        monkeypatch.setattr(eval_harness, "_session_calls", no_calls)
        eval_harness.main(flags + ["score", "--out", str(run2), "--publish"])
        assert published[0]["build"] == self.RUNTIME and published[0]["buildSource"] == "image_tag"

    def test_a_failed_write_never_changes_the_exit_code(self, tmp_path, golden, capsys, monkeypatch):
        from apis.shared.costs import ledger_publish

        flags, _ = golden
        run1 = tmp_path / "run1"
        _write_run(run1)
        assert eval_harness.main(flags + ["baseline", "--out", str(run1)]) == 0
        meta = json.loads((run1 / "meta.json").read_text())
        meta.update({"startedAt": "2026-11-07T04:00:00Z", "prefix": "dev-example",
                     "appVersion": "1.33.0", "imageTags": {"inference-api": self.RUNTIME}})
        (run1 / "meta.json").write_text(json.dumps(meta))

        async def no_calls(meta, session_ids):
            return {}

        monkeypatch.setattr(eval_harness, "_session_calls", no_calls)
        monkeypatch.setattr(ledger_publish, "put_ledger_row", lambda row, **kw: False)
        assert eval_harness.main(flags + ["score", "--out", str(run1), "--publish"]) == 0
        assert "publish failed" in capsys.readouterr().err

    def test_a_run_with_no_build_anywhere_is_not_published(self, tmp_path, golden, capsys, monkeypatch, published):
        flags, run2 = self._runs(tmp_path, golden)
        meta = json.loads((run2 / "meta.json").read_text())
        meta.pop("imageTags")
        (run2 / "meta.json").write_text(json.dumps(meta))

        async def no_calls(meta, session_ids):
            return {}

        monkeypatch.setattr(eval_harness, "_session_calls", no_calls)
        assert eval_harness.main(flags + ["score", "--out", str(run2), "--publish"]) == 1
        assert published == [] and "not published" in capsys.readouterr().err

    def test_score_without_publish_writes_nothing(self, tmp_path, golden, published):
        flags, run2 = self._runs(tmp_path, golden)
        eval_harness.main(flags + ["score", "--out", str(run2)])
        assert published == []


def test_baseline_refuses_a_partial_run(tmp_path, golden, capsys):
    flags, baselines = golden
    run = tmp_path / "run"
    _write_run(run, samples=2)
    assert eval_harness.main(flags + ["baseline", "--out", str(run)]) == 2
    assert "fewer than k=3" in capsys.readouterr().err
    assert not baselines.exists()


def test_score_without_a_baseline_is_a_setup_error(tmp_path, golden, capsys):
    flags, _ = golden
    run = tmp_path / "run"
    _write_run(run)
    assert eval_harness.main(flags + ["score", "--out", str(run)]) == 2
    assert "no baseline" in capsys.readouterr().err


def test_run_refuses_production_before_anything_else(tmp_path, capsys):
    code = eval_harness.main([
        "run", "--out", str(tmp_path / "r"), "--model-id", MODEL, "--user-id", "u", "--prefix", "prod-boisestateai",
    ])
    assert code == 2
    assert "production" in capsys.readouterr().err
    assert not (tmp_path / "r").exists()


def test_run_without_yes_only_estimates(tmp_path, capsys):
    code = eval_harness.main([
        "run", "--out", str(tmp_path / "r"), "--model-id", MODEL, "--user-id", "u",
        "--prefix", "dev-example", "--family", "tool_selection",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "estimate only" in out
    assert not (tmp_path / "r").exists()


class TestRunAuthArguments:
    BASE = ["run", "--out", "x", "--model-id", MODEL, "--prefix", "dev-example"]

    def test_headless_grant_needs_a_user_id(self):
        with pytest.raises(SystemExit):
            eval_harness.parse_args(self.BASE)
        assert eval_harness.parse_args(self.BASE + ["--user-id", "u"]).auth == "headless-grant"

    def test_cognito_needs_a_base_url_and_no_user_id(self):
        with pytest.raises(SystemExit):
            eval_harness.parse_args(self.BASE + ["--auth", "cognito"])
        args = eval_harness.parse_args(self.BASE + ["--auth", "cognito", "--base-url", "https://dev.example/api"])
        assert args.user_id is None and args.base_url == "https://dev.example/api"

    def test_a_production_base_url_is_refused(self, tmp_path, capsys):
        code = eval_harness.main([
            "run", "--out", str(tmp_path / "r"), "--model-id", MODEL, "--prefix", "dev-example",
            "--auth", "cognito", "--base-url", "https://boisestate.ai/api",
        ])
        assert code == 2 and "production" in capsys.readouterr().err


class TestJudgeCommand:
    def _run_dir(self, tmp_path):
        out = tmp_path / "run"
        out.mkdir()
        (out / "meta.json").write_text(json.dumps({"model_id": MODEL, "k": 1}), encoding="utf-8")
        rows = [
            {"case_id": "general_chat.explain_mutex_vs_semaphore", "family": "general_chat", "sample": 0,
             "model_id": MODEL, "passed": True, "final_message": "ok", "tool_calls": [], "session": {}},
            {"case_id": "tool_selection.irrational_square_root_reaches_for_calculator", "family": "tool_selection",
             "sample": 0, "model_id": MODEL, "passed": True, "final_message": "295.535108",
             "tool_calls": [{"name": "calculator"}, {"name": "calculator"}], "session": {}},
        ]
        (out / "results.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return out

    def test_estimates_one_call_per_evaluator_and_per_tool_call(self, tmp_path, capsys):
        out = self._run_dir(tmp_path)
        assert eval_harness.main(["judge", "--out", str(out)]) == 0
        printed = capsys.readouterr().out
        # general_chat case: helpfulness + conciseness = 2; tool case: two tool calls = 2.
        assert "2 samples to judge" in printed and "~4 judge calls" in printed and "estimate only" in printed
        assert "judge its own answers" not in capsys.readouterr().err

    def test_warns_when_the_judge_is_the_model_under_test(self, tmp_path, capsys):
        out = self._run_dir(tmp_path)
        same = "global." + MODEL.removeprefix("us.")
        assert eval_harness.main(["judge", "--out", str(out), "--judge-model", same]) == 0
        assert "would judge its own answers" in capsys.readouterr().err

    def test_spending_without_the_package_is_a_clear_setup_error(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setitem(sys.modules, "strands_evals", None)  # import now raises ImportError
        out = self._run_dir(tmp_path)
        assert eval_harness.main(["judge", "--out", str(out), "--yes"]) == 2
        assert "dev extra" in capsys.readouterr().err


def test_cleaned_up_staged_samples_are_forgotten_so_a_resume_stages_them_again(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    staged = [
        {"case_id": "document_qa.a", "sample": 0, "session_id": "eval-ran", "upload_ids": ["u1"], "documents": ["manual"]},
        {"case_id": "document_qa.a", "sample": 1, "session_id": "eval-unrun", "upload_ids": ["u2"], "documents": ["manual"]},
    ]
    (out / eval_harness.STAGED_FILE).write_text("".join(json.dumps(e) + "\n" for e in staged), encoding="utf-8")
    unrun = eval_harness._unrun_staged(out, [{"session_id": "eval-ran"}])
    assert [e["session_id"] for e in unrun] == ["eval-unrun"]

    eval_harness._forget_staged(out, unrun)
    remaining = eval_harness.read_rows(out / eval_harness.STAGED_FILE)
    assert [e["session_id"] for e in remaining] == ["eval-ran"]


class TestOrphanSweep:
    def test_cleanup_needs_a_run_or_a_sweep(self, capsys):
        with pytest.raises(SystemExit):
            eval_harness.main(["cleanup"])
        assert "--out" in capsys.readouterr().err

    def test_a_sweep_needs_a_prefix_and_refuses_production(self, capsys):
        assert eval_harness.main(["cleanup", "--orphans"]) == 2
        assert eval_harness.main(["cleanup", "--orphans", "--prefix", "prod-boisestateai"]) == 2
        assert "production" in capsys.readouterr().err

    @pytest.fixture
    def deployment(self, monkeypatch):
        import boto3
        from moto import mock_aws

        import seed_eval_user
        import smoke_turns
        from apis.app_api.sessions.services import session_service
        from apis.shared.files import repository as repository_module
        from apis.shared.files.repository import FileUploadRepository
        from apis.shared.sessions import metadata

        for key, value in {"AWS_DEFAULT_REGION": "us-east-1", "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t",
                           "S3_USER_FILES_BUCKET_NAME": "files", "DYNAMODB_USER_FILES_TABLE_NAME": "files"}.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setattr(seed_eval_user, "eval_user_id", lambda prefix, region: "eval-user")
        monkeypatch.setattr(smoke_turns, "resolve_runtime_env", lambda prefix, region: None)

        class Session:
            def __init__(self, session_id, last):
                self.session_id, self.last_message_at = session_id, last

        async def list_user_sessions(user_id, limit=None, next_token=None):
            return [Session("eval-old", "2020-01-01T00:00:00Z"), Session("eval-new", "2999-01-01T00:00:00Z"),
                    Session("someone-elses", "2020-01-01T00:00:00Z")], None

        ended = []

        async def delete_session(self, user_id, session_id):
            ended.append(session_id)
            return True

        monkeypatch.setattr(metadata, "list_user_sessions", list_user_sessions)
        monkeypatch.setattr(session_service.SessionService, "__init__", lambda self: None)
        monkeypatch.setattr(session_service.SessionService, "delete_session", delete_session)
        with mock_aws():
            boto3.client("dynamodb", region_name="us-east-1").create_table(
                TableName="files",
                KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
                AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                      {"AttributeName": "SK", "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
            s3 = boto3.client("s3", region_name="us-east-1")
            s3.create_bucket(Bucket="files")
            monkeypatch.setattr(repository_module, "_repository_instance", FileUploadRepository(table_name="files"))
            for key in ("user-files/eval-user/eval-old/up-1/manual.pdf",
                        "user-files/eval-user/eval-old/up-1/_derived/text.jsonl"):
                s3.put_object(Bucket="files", Key=key, Body=b"x")
            yield s3, ended

    def _sweep(self, *extra):
        return eval_harness.main(["cleanup", "--orphans", "--prefix", "dev-x", "--region", "us-east-1",
                                  "--older-than", "-1", *extra])

    def test_lists_without_yes_and_deletes_with_it(self, deployment, capsys):
        s3, ended = deployment
        assert self._sweep() == 0
        out = capsys.readouterr().out
        assert "1 upload(s) and 1 session(s)" in out and "eval-old/up-1" in out and "dry run" in out
        assert len(s3.list_objects_v2(Bucket="files").get("Contents", [])) == 2 and ended == []

        assert self._sweep("--yes") == 0
        assert "swept 1 upload(s) and 1 session(s)" in capsys.readouterr().out
        assert s3.list_objects_v2(Bucket="files").get("Contents", []) == []
        assert ended == ["eval-old"]


# --------------------------------------------------------------------------- attachment_delivery (§8)


def test_presign_uploads_need_a_cognito_session():
    base = ["run", "--out", "x", "--model-id", MODEL, "--prefix", "dev-example"]
    assert eval_harness.parse_args(base + ["--user-id", "u"]).upload_via == "direct"
    with pytest.raises(SystemExit):
        eval_harness.parse_args(base + ["--user-id", "u", "--upload-via", "presign"])
    args = eval_harness.parse_args(base + ["--auth", "cognito", "--base-url", "https://dev.example/api",
                                           "--upload-via", "presign"])
    assert args.upload_via == "presign"


def test_the_estimate_prices_sized_attachments():
    from apis.shared.evals.cases import load_cases

    cases = {c.id: c for c in load_cases(eval_harness.CASES_DIR, families=["attachment_delivery"])}
    _, small, _ = eval_harness.estimate([(cases["attachment_delivery.lookup_md_5k"], 0)], MODEL)
    _, large, _ = eval_harness.estimate([(cases["attachment_delivery.lookup_md_300k"], 0)], MODEL)
    # ~300k tokens written to the cache at 1.25x Haiku 4.5's $1.10.
    assert large > 0.4 > small


def test_a_run_with_no_family_leaves_out_the_opt_in_family(tmp_path, capsys):
    from apis.shared.evals.cases import DEFAULT_FAMILIES, load_cases

    default = len(load_cases(eval_harness.CASES_DIR, families=list(DEFAULT_FAMILIES)))
    delivery = len(load_cases(eval_harness.CASES_DIR, families=["attachment_delivery"]))
    code = eval_harness.main([
        "run", "--out", str(tmp_path / "r"), "--model-id", MODEL, "--user-id", "u", "--prefix", "dev-example",
    ])
    assert code == 0
    every = eval_harness.main([
        "run", "--out", str(tmp_path / "r"), "--model-id", MODEL, "--user-id", "u", "--prefix", "dev-example",
        "--family", "attachment_delivery",
    ])
    assert every == 0
    first, second = capsys.readouterr().out.split("estimate only", 1)
    assert f"{default} cases × k=3" in first
    assert f"{delivery} cases × k=3" in second


def _write_delivery_run(out: Path):
    out.mkdir(parents=True, exist_ok=True)
    (out / "meta.json").write_text(json.dumps({"model_id": MODEL, "k": 1, "startedAt": "t"}), encoding="utf-8")
    (out / "results.jsonl").write_text(json.dumps({
        "case_id": "attachment_delivery.lookup_md_5k", "family": "attachment_delivery", "sample": 0,
        "model_id": MODEL, "passed": True, "failures": [], "final_message": "WP", "tool_calls": [],
        "session_id": "eval-x", "timing": [{"firstTokenMs": 900, "turnDurationMs": 2100}],
    }) + "\n", encoding="utf-8")


def test_readout_prints_the_per_case_table_and_writes_json(tmp_path, capsys):
    run = tmp_path / "run"
    _write_delivery_run(run)
    report = tmp_path / "readout.json"
    assert eval_harness.main(["readout", "--out", str(run), "--no-cost-rows", "--json", str(report)]) == 0
    out = capsys.readouterr().out
    assert "| lookup_md_5k | 1/1 |" in out and "0.9s" in out
    assert json.loads(report.read_text())["cases"][0]["firstTokenMs"] == 900


def test_baseline_family_splices_into_the_existing_file(tmp_path, golden, capsys):
    flags, baselines = golden
    run1 = tmp_path / "run1"
    _write_run(run1)
    assert eval_harness.main(flags + ["baseline", "--out", str(run1)]) == 0
    path = baselines / eval_harness.baseline_filename(MODEL)
    before = json.loads(path.read_text())

    run2 = tmp_path / "run2"
    _write_run(run2, failing="a")
    assert eval_harness.main(flags + ["baseline", "--out", str(run2), "--family", "tool_selection"]) == 0
    after = json.loads(path.read_text())
    assert before["cases"]["tool_selection.a"]["passed"] and not after["cases"]["tool_selection.a"]["passed"]
    assert "tool_selection" in after["familyPromotions"]
