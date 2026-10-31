#!/usr/bin/env python3
"""Evaluation harness: does this deploy answer as well as the last promoted one?

Spec: docs/specs/eval-harness.md. The golden set is ``backend/evals/cases/``;
every case is a synthetic conversation with expectations a pure function can
check. ``run`` drives each case ``k`` times through the deployed AgentCore
Runtime as the eval user (``run_agent_headless``, the same path a scheduled run
takes), ``score`` compares the run with the committed baseline for its model,
family by family, and exits non-zero when a gating family regressed.

Subcommands:

  cases     Validate every case file; print the family table and the tools and
            skills the eval user must be granted. No AWS.
  run       (spends) Run --family (default all) with --k samples on --model-id.
            Prints the estimate and stops unless --yes. Resumable: samples
            already in --out/results.jsonl are skipped. Deletes its sessions
            and fixture uploads at the end unless --keep.
  judge     (spends) Score the run's samples with the LLM judges their cases
            name (strands-agents-evals, a dev extra), over scrubbed sessions.
            Estimate first, stop without --yes; resumable into judgements.jsonl.
            Advisory: no family gates on a judge.
  score     Compare --out with baselines/<model>.json. Exit 1 if a gating family
            regressed, 2 if the comparison cannot be made. --explain FAMILY
            prints every flipped case with both answers and, best-effort,
            each sample's model calls from its session's C# cost rows (cache
            and compaction status, prompt hashes); that read is the only AWS
            call, and --no-cost-rows skips it.
  baseline  Promote --out to baselines/<model>.json. Refuses unless every case
            in the golden set ran at the run's full k. No AWS.
  cleanup   Delete the sessions and fixture uploads recorded in --out.

Usage (from backend/):

    uv run python scripts/eval_harness.py cases
    # As the eval user (seeded once per environment by scripts/seed_eval_user.py):
    AWS_PROFILE=dev-ai uv run python scripts/eval_harness.py run --out /tmp/eval-1 \
        --auth cognito --base-url https://dev.boisestate.ai/api --prefix dev-boisestateai-v2 \
        --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0 --yes
    # As any other owner of an active headless grant:
    AWS_PROFILE=dev-ai uv run python scripts/eval_harness.py run --out /tmp/eval-1 \\
        --user-id <cognito-sub> --prefix dev-boisestateai-v2 \\
        --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0            # estimate only
    AWS_PROFILE=dev-ai uv run python scripts/eval_harness.py run --out /tmp/eval-1 ... --yes
    uv run python scripts/eval_harness.py score --out /tmp/eval-1
    uv run python scripts/eval_harness.py score --out /tmp/eval-1 --explain tool_selection
    uv run python scripts/eval_harness.py baseline --out /tmp/eval-1

A branch is proved before it deploys by pointing the turns at a local
inference-api (``--invocations-url http://127.0.0.1:8001``) with the same
headless grant, as in docs/testing/smoke-regression.md. From a worktree, the
local server must be started with PYTHONPATH set to the worktree's src, or it
silently runs develop's code.

Two ways to name the user. ``--auth cognito`` (the default for the release
pass) signs in as the eval user with the scripted Hosted UI login, reading its
credentials from SSM, and renews that user's headless grant from the fresh
session (``POST /runs/grant``), so the grant's 30-day window never lapses.
``--auth headless-grant --user-id <sub>`` runs as the owner of an existing
grant (apis/shared/harness/grants.py) and writes into that person's account.
There is deliberately no way to target production: the code under test is the
same image, and a prod run would spend a user's quota to learn nothing dev
cannot tell us (spec D8).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apis.shared.evals.cases import FAMILIES, CaseError, EvalCase, load_cases, required_grants  # noqa: E402
from apis.shared.evals.judges import DEFAULT_JUDGE_MODEL, same_base_model  # noqa: E402
from apis.shared.evals.report import (  # noqa: E402
    ReportError,
    any_gate_failed,
    baseline_filename,
    build_baseline,
    case_outcomes,
    compare_judges,
    compare_run,
    explain_family,
    format_judge_table,
    format_table,
    load_baseline,
    read_rows,
)

logger = logging.getLogger("eval_harness")

BACKEND_DIR = Path(__file__).resolve().parent.parent
CASES_DIR = BACKEND_DIR / "evals" / "cases"
FIXTURES_DIR = BACKEND_DIR / "evals" / "fixtures"
BASELINES_DIR = BACKEND_DIR / "evals" / "baselines"

RESULTS_FILE = "results.jsonl"
META_FILE = "meta.json"
JUDGEMENTS_FILE = "judgements.jsonl"
#: A judge call carries the scrubbed session plus the evaluator's own prompt.
#: Sized from strands-evals' built-in prompts; refine from the first run.
EST_JUDGE_INPUT_TOKENS = 2_500
EST_JUDGE_OUTPUT_TOKENS = 300

#: Per-MTok input/output, Regional (us.*) rates, only to size the pre-flight
#: estimate. The catalog (curated-models.ts) is the source of truth.
RATES: Dict[str, Tuple[float, float]] = {
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": (1.10, 5.50),
    "us.anthropic.claude-sonnet-4-6": (3.30, 16.50),
    "us.anthropic.claude-sonnet-5": (2.20, 11.00),
}
FALLBACK_RATE_MODEL = "us.anthropic.claude-sonnet-4-6"
#: Base-rate-equivalent tokens per turn, measured: the first full k=3 run on dev
#: (Haiku 4.5, 2026-10-30, 219 turns) averaged ~2.1k uncached input, ~0.5k cache
#: write (1.25x), ~1.9k cache read (0.1x) and ~130 output tokens per turn, about
#: $0.86 in all. These figures add a third on top, so the estimate stays high.
EST_INPUT_TOKENS_PER_TURN = 4_000
EST_OUTPUT_TOKENS_PER_TURN = 200
#: Re-mint the Cognito bearer well inside its one-hour life.
BEARER_REUSE_SECONDS = 20 * 60


# --------------------------------------------------------------------------- helpers


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _git_commit() -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=BACKEND_DIR, capture_output=True, text=True, timeout=10, check=True
        )
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _image_tags(prefix: str, region: str) -> Dict[str, str]:
    """The deployed image tags the run measured, best effort (spec §8 baseline metadata)."""
    try:
        import boto3

        ssm = boto3.client("ssm", region_name=region)
    except Exception:  # noqa: BLE001 — metadata only, never blocks a run
        return {}
    tags: Dict[str, str] = {}
    for service in ("inference-api", "app-api"):
        try:
            # The parameter holds the full ECR URI, which names the AWS account;
            # baselines are committed, so keep only the tag after the last ':'.
            uri = ssm.get_parameter(Name=f"/{prefix}/{service}/image-tag")["Parameter"]["Value"]
            tags[service] = uri.rsplit(":", 1)[-1]
        except Exception:  # noqa: BLE001
            continue
    return tags


def _read_meta(out: Path) -> Dict[str, Any]:
    path = out / META_FILE
    if not path.is_file():
        raise ReportError(f"{path} not found; is --out a run directory?")
    return json.loads(path.read_text(encoding="utf-8"))


def _baseline_path(args: argparse.Namespace, model_id: str) -> Path:
    if getattr(args, "baseline", None):
        return Path(args.baseline)
    return Path(args.baselines_dir) / baseline_filename(model_id)


def estimate(todo: Sequence[Tuple[EvalCase, int]], model_id: str) -> Tuple[int, float, str]:
    turns = sum(len(case.turns) for case, _ in todo)
    rate_model = model_id if model_id in RATES else FALLBACK_RATE_MODEL
    rate_in, rate_out = RATES[rate_model]
    dollars = turns * (EST_INPUT_TOKENS_PER_TURN * rate_in + EST_OUTPUT_TOKENS_PER_TURN * rate_out) / 1_000_000
    note = "" if rate_model == model_id else f" (priced at {rate_model} rates; no table entry for {model_id})"
    return turns, dollars, note


class CachedBearerAuth:
    """Reuse one minted bearer for many turns instead of a Cognito exchange per turn."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._token: Optional[str] = None
        self._minted_at = 0.0
        self._lock = asyncio.Lock()

    async def mint_bearer_for_user(self, user_id: str) -> str:
        async with self._lock:
            if self._token is None or time.monotonic() - self._minted_at > BEARER_REUSE_SECONDS:
                self._token = await self._inner.mint_bearer_for_user(user_id)
                self._minted_at = time.monotonic()
            return self._token


async def sign_in_eval_user(base_url: str, prefix: str, region: str) -> str:
    """Sign in as the eval user, renew its headless grant, and return its user id.

    Credentials come from ``EVAL_USERNAME`` / ``EVAL_PASSWORD`` when set, else
    from the SSM SecureString ``seed_eval_user.py`` wrote. They are never logged.
    """
    from seed_eval_user import credentials_parameter
    from smoke_turns import AppApiTransport

    username = os.environ.get("EVAL_USERNAME")
    password = os.environ.get("EVAL_PASSWORD")
    if not (username and password):
        import boto3

        ssm = boto3.client("ssm", region_name=region)
        try:
            raw = ssm.get_parameter(Name=credentials_parameter(prefix), WithDecryption=True)["Parameter"]["Value"]
        except ssm.exceptions.ParameterNotFound as exc:
            raise ReportError(
                f"no eval user credentials at {credentials_parameter(prefix)}; "
                "run scripts/seed_eval_user.py for this environment first"
            ) from exc
        creds = json.loads(raw)
        username, password = creds["username"], creds["password"]

    transport = AppApiTransport(base_url, label="eval")
    try:
        await transport.login_cognito(username, password, "")
        await transport.enable_headless_grant()
    finally:
        await transport.close()
    user_id = transport.user.get("user_id")
    if not user_id:
        raise ReportError("signed in, but GET /auth/session named no user_id")
    return str(user_id)


# --------------------------------------------------------------------------- cases


def cmd_cases(args: argparse.Namespace) -> int:
    try:
        cases = load_cases(Path(args.cases_dir), families=args.family, fixtures_dir=Path(args.fixtures_dir))
    except CaseError as exc:
        print(f"invalid case set: {exc}", file=sys.stderr)
        return 2
    counts = Counter(case.family for case in cases)
    turns = Counter()
    for case in cases:
        turns[case.family] += len(case.turns)
    print(f"{'family':<22} {'cases':>5} {'turns':>5}  gate")
    for family in FAMILIES:
        if counts.get(family):
            gate = "advisory" if family == "general_chat" else "gating"
            print(f"{family:<22} {counts[family]:>5} {turns[family]:>5}  {gate}")
    print(f"{'total':<22} {len(cases):>5} {sum(turns.values()):>5}")
    grants = required_grants(cases)
    print("\nThe eval user's role must grant:")
    print(f"  tools:  {', '.join(grants['tools']) or '—'}")
    print(f"  skills: {', '.join(grants['skills']) or '—'}")
    return 0


# --------------------------------------------------------------------------- run


def _done_samples(rows: Sequence[Dict[str, Any]]) -> Set[Tuple[str, int]]:
    return {(str(r["case_id"]), int(r["sample"])) for r in rows}


def _prepare_meta(out: Path, args: argparse.Namespace, families: List[str]) -> Dict[str, Any]:
    """Create meta.json, or check a resumed run is the same run."""
    path = out / META_FILE
    if path.is_file():
        meta = json.loads(path.read_text(encoding="utf-8"))
        if meta.get("model_id") != args.model_id or int(meta.get("k", 0)) != args.k:
            raise ReportError(
                f"{out} holds a run on {meta.get('model_id')} with k={meta.get('k')}; "
                "use a new --out for a different model or k"
            )
        if meta.get("user_id") != args.user_id:
            raise ReportError(f"{out} holds a run as a different user; use a new --out")
        meta["families"] = sorted(set(meta.get("families") or []) | set(families), key=FAMILIES.index)
    else:
        meta = {
            "model_id": args.model_id,
            "k": args.k,
            "families": families,
            "startedAt": _now_iso(),
            "target": f"local:{args.invocations_url}" if args.invocations_url else f"runtime:{args.prefix}",
            "prefix": args.prefix,
            "region": args.region,
            "auth": args.auth,
            "user_id": args.user_id,
            "gitCommit": _git_commit(),
        }
    path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return meta


async def cmd_run(args: argparse.Namespace) -> int:
    from smoke_turns import _looks_like_prod

    if _looks_like_prod(args.base_url or args.invocations_url, args.prefix):
        print("refusing: this looks like production; the eval harness targets dev or a local stack (spec D8)",
              file=sys.stderr)
        return 2
    try:
        cases = load_cases(Path(args.cases_dir), families=args.family, fixtures_dir=Path(args.fixtures_dir))
    except CaseError as exc:
        print(f"invalid case set: {exc}", file=sys.stderr)
        return 2
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2

    out = Path(args.out)
    rows = read_rows(out / RESULTS_FILE)
    done = _done_samples(rows)
    todo = [(case, s) for case in cases for s in range(args.k) if (case.id, s) not in done]
    turns, dollars, note = estimate(todo, args.model_id)
    print(
        f"{len(cases)} cases × k={args.k} on {args.model_id}: {len(todo)} samples to run "
        f"({len(done)} already in {RESULTS_FILE}), {turns} turns, ≈ ${dollars:.2f}{note}"
    )
    if not todo:
        print("nothing to run")
        return 0
    if not args.yes:
        print("estimate only; pass --yes to spend")
        return 0

    from smoke_turns import resolve_runtime_env

    from apis.shared.evals.execute import EVAL_TRIGGER, OAuthRequiredError, run_sample
    from apis.shared.evals.fixtures import FixtureError, load_fixture, register_upload
    from apis.shared.harness.auth import CognitoRefreshBearerAuth, HeadlessAuthError
    from apis.shared.harness.runner import run_agent_headless

    resolve_runtime_env(args.prefix, args.region)
    if args.auth == "cognito":
        try:
            args.user_id = await sign_in_eval_user(args.base_url, args.prefix, args.region)
        except (ReportError, RuntimeError) as exc:
            print(f"could not sign in as the eval user: {exc}", file=sys.stderr)
            return 2
        print("signed in as the eval user; headless grant renewed")
    out.mkdir(parents=True, exist_ok=True)
    families = sorted({case.family for case in cases}, key=FAMILIES.index)
    try:
        meta = _prepare_meta(out, args, families)
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if "imageTags" not in meta and not args.invocations_url:
        meta["imageTags"] = _image_tags(args.prefix, args.region)
        (out / META_FILE).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    auth = CachedBearerAuth(CognitoRefreshBearerAuth())
    fixtures_dir = Path(args.fixtures_dir)

    async def run_turn(**kwargs: Any) -> Any:
        return await run_agent_headless(
            user_id=args.user_id,
            auth=auth,
            invocations_base_url=args.invocations_url,
            trigger=EVAL_TRIGGER,
            timeout_seconds=args.timeout,
            **kwargs,
        )

    async def register_fixture(session_id: str, filename: str) -> str:
        data, mime_type = load_fixture(fixtures_dir, filename)
        return await register_upload(args.user_id, session_id, filename, mime_type, data)

    results_path = out / RESULTS_FILE
    queue: asyncio.Queue = asyncio.Queue()
    for item in todo:
        queue.put_nowait(item)
    fatal: List[str] = []
    new_rows: List[Dict[str, Any]] = []

    async def worker() -> None:
        while not fatal:
            try:
                case, sample = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                row = await run_sample(
                    case,
                    sample=sample,
                    model_id=args.model_id,
                    run_turn=run_turn,
                    register_fixture=register_fixture,
                    fixtures_dir=fixtures_dir,
                )
            except (OAuthRequiredError, HeadlessAuthError, FixtureError) as exc:
                fatal.append(f"{type(exc).__name__}: {exc}")
                return
            with results_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            new_rows.append(row)
            verdict = "PASS" if row["passed"] else "FAIL"
            detail = "" if row["passed"] else "  " + "; ".join(row["failures"])
            print(f"{verdict}  {case.id} [{sample}]{detail}", flush=True)

    await asyncio.gather(*(worker() for _ in range(max(1, args.workers))))

    if not args.keep:
        await _cleanup(new_rows, args.user_id)

    if fatal:
        print(f"\nrun stopped: {fatal[0]}", file=sys.stderr)
        print("fix the eval user's grants or the environment, then re-run the same command to resume",
              file=sys.stderr)
        return 2

    all_rows = read_rows(results_path)
    by_family: Dict[str, List[bool]] = defaultdict(list)
    for outcome in case_outcomes(all_rows).values():
        by_family[outcome.family].append(outcome.passed)
    tokens = Counter()
    for row in new_rows:
        tokens.update(row.get("usage") or {})
    print("\ncase outcomes (majority of samples) — compare with `score`, never read alone:")
    for family in FAMILIES:
        if family in by_family:
            outcomes = by_family[family]
            print(f"  {family:<22} {sum(outcomes)}/{len(outcomes)}")
    if tokens:
        print("tokens this invocation: " + ", ".join(f"{k}={v:,}" for k, v in sorted(tokens.items())))
    return 0


# --------------------------------------------------------------------------- cleanup


async def _cleanup(rows: Sequence[Dict[str, Any]], user_id: str) -> Tuple[int, int]:
    from apis.app_api.sessions.services.session_service import SessionService
    from apis.shared.evals.fixtures import delete_upload

    sessions = 0
    uploads = 0
    service = SessionService()
    for row in rows:
        for upload_id in row.get("upload_ids") or []:
            try:
                uploads += 1 if await delete_upload(user_id, upload_id) else 0
            except Exception as exc:  # noqa: BLE001 — cleanup reports, never aborts
                logger.warning("could not delete upload %s: %s", upload_id, exc)
        try:
            if await service.delete_session(user_id=user_id, session_id=row["session_id"]):
                sessions += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not delete session %s: %s", row.get("session_id"), exc)
    print(f"cleanup: deleted {sessions} session(s) and {uploads} fixture upload(s)")
    return sessions, uploads


async def cmd_cleanup(args: argparse.Namespace) -> int:
    from smoke_turns import resolve_runtime_env

    out = Path(args.out)
    try:
        meta = _read_meta(out)
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    resolve_runtime_env(meta["prefix"], meta.get("region") or args.region)
    await _cleanup(read_rows(out / RESULTS_FILE), meta["user_id"])
    return 0


# --------------------------------------------------------------------------- score / baseline


def _judge_calls(case: EvalCase, row: Dict[str, Any]) -> int:
    """Model calls one row costs: one per evaluator, one per tool call for the tool-level judge."""
    calls = 0
    for name in case.judge.evaluators:
        calls += max(1, len(row.get("tool_calls") or [])) if name == "tool_selection" else 1
    return calls + (1 if case.judge.rubric else 0)


async def cmd_judge(args: argparse.Namespace) -> int:
    out = Path(args.out)
    try:
        meta = _read_meta(out)
        cases = {c.id: c for c in load_cases(Path(args.cases_dir), families=args.family)}
    except (ReportError, CaseError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    rows = [r for r in read_rows(out / RESULTS_FILE) if r["case_id"] in cases]
    judgements_path = out / JUDGEMENTS_FILE
    done = {
        (j["case_id"], int(j["sample"]))
        for j in read_rows(judgements_path)
        if j.get("judge_model") == args.judge_model
    }
    todo = [
        r for r in rows
        if (cases[r["case_id"]].judge.evaluators or cases[r["case_id"]].judge.rubric)
        and (r["case_id"], int(r["sample"])) not in done
    ]
    calls = sum(_judge_calls(cases[r["case_id"]], r) for r in todo)
    rate_model = args.judge_model if args.judge_model in RATES else FALLBACK_RATE_MODEL
    rate_in, rate_out = RATES[rate_model]
    dollars = calls * (EST_JUDGE_INPUT_TOKENS * rate_in + EST_JUDGE_OUTPUT_TOKENS * rate_out) / 1_000_000
    print(f"{len(todo)} samples to judge with {args.judge_model}: ~{calls} judge calls, ≈ ${dollars:.2f}")
    if same_base_model(args.judge_model, str(meta.get("model_id") or "")):
        print(
            f"warning: {args.judge_model} would judge its own answers; a judge tends to favour its own model "
            "(docs/specs/eval-harness.md §14, question 2), so pick a different --judge-model",
            file=sys.stderr,
        )
    if not todo:
        print("nothing to judge")
        return 0
    if not args.yes:
        print("estimate only; pass --yes to spend")
        return 0

    from apis.shared.evals.judges import StrandsEvalsJudge, judge_row

    try:
        import strands_evals  # noqa: F401 — fail fast with a clear message, not per sample
    except ImportError:
        print("strands-agents-evals is not installed; it is a dev extra (`uv sync --extra dev`)", file=sys.stderr)
        return 2

    judge = StrandsEvalsJudge(args.judge_model)
    semaphore = asyncio.Semaphore(max(1, args.workers))
    lock = asyncio.Lock()

    async def one(row: Dict[str, Any]) -> None:
        case = cases[row["case_id"]]
        async with semaphore:
            results = await asyncio.to_thread(
                judge_row, row, evaluators=case.judge.evaluators, rubric=case.judge.rubric,
                judge=judge, judge_model=args.judge_model, expected_refusal=case.expect.refuses,
            )
        async with lock:
            with judgements_path.open("a", encoding="utf-8") as fh:
                for result in results:
                    fh.write(json.dumps(result) + "\n")
        summary = ", ".join(
            f"{j['evaluator']}={j['score']:.2f}" if j["score"] is not None else f"{j['evaluator']}: {j['error']}"
            for j in results
        )
        print(f"{row['case_id']} [{row['sample']}]  {summary}", flush=True)

    await asyncio.gather(*(one(r) for r in todo))
    return 0


async def _family_calls(
    meta: Dict[str, Any], rows: Sequence[Dict[str, Any]], family: str
) -> Optional[Dict[str, Sequence[Dict[str, Any]]]]:
    """The ``C#`` rows of every session in ``family``, best-effort (spec §14 q3).

    Reads the deployment's sessions-metadata table, so it needs credentials
    for the run's account; without them every sample prints "unavailable"
    and ``score``'s result is unchanged.
    """
    prefix, region = meta.get("prefix"), meta.get("region")
    if not prefix:
        print("cost rows: the run's meta.json names no prefix; skipped", file=sys.stderr)
        return None
    from apis.shared.config.runtime_environment import derived_environment_manifest
    from apis.shared.evals.calls import fetch_session_calls
    from apis.shared.storage.dynamodb_storage import DynamoDBStorage

    table = next(v for v in derived_environment_manifest() if v.name == "DYNAMODB_SESSIONS_METADATA_TABLE_NAME")
    os.environ["DYNAMODB_SESSIONS_METADATA_TABLE_NAME"] = table.derive(prefix, None) or ""
    if region:
        os.environ.setdefault("AWS_DEFAULT_REGION", region)
    session_ids = [str(r.get("session_id")) for r in rows if r.get("family") == family and r.get("session_id")]
    try:
        storage = DynamoDBStorage()
    except Exception as exc:  # noqa: BLE001 — a diagnostic never fails score
        print(f"cost rows: unavailable ({exc})", file=sys.stderr)
        return {}
    found = await fetch_session_calls(session_ids, storage)
    print(f"cost rows: read {len(found)} of {len(set(session_ids))} sessions in {family}", file=sys.stderr)
    return found


def cmd_score(args: argparse.Namespace) -> int:
    out = Path(args.out)
    try:
        meta = _read_meta(out)
        rows = read_rows(out / RESULTS_FILE)
        if not rows:
            raise ReportError(f"no results in {out / RESULTS_FILE}")
        baseline = load_baseline(_baseline_path(args, meta["model_id"]))
        comparisons = compare_run(baseline, rows, model_id=meta["model_id"])
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    print(f"run {out} on {meta['model_id']} (k={meta['k']}) vs baseline promoted {baseline.get('promotedAt')}\n")
    print(format_table(comparisons))
    judgements = read_rows(out / JUDGEMENTS_FILE)
    judge_comparisons = []
    if judgements:
        models = sorted({str(j.get("judge_model")) for j in judgements})
        try:
            judge_comparisons = compare_judges(baseline, judgements, judge_model=models[0] if len(models) == 1 else None)
        except ReportError as exc:
            print(f"\njudges not compared: {exc}")
        if judge_comparisons:
            print("\nLLM judges (advisory, mean score 0-1, paired cases only):\n")
            print(format_judge_table(judge_comparisons))
    if args.explain:
        calls = None if args.no_cost_rows else asyncio.run(_family_calls(meta, rows, args.explain))
        print()
        print(explain_family(args.explain, baseline, rows, calls_by_session=calls))
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "modelId": meta["model_id"],
                    "k": meta["k"],
                    "run": {key: meta.get(key) for key in ("startedAt", "target", "gitCommit", "imageTags")},
                    "baselinePromotedAt": baseline.get("promotedAt"),
                    "families": [c.to_dict() for c in comparisons],
                    "judges": [
                        {"family": c.family, "evaluator": c.evaluator, "n": c.n,
                         "baseMean": c.base_mean, "runMean": c.run_mean}
                        for c in judge_comparisons
                    ],
                    "gateFailed": any_gate_failed(comparisons),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
    failed = any_gate_failed(comparisons)
    print("\nGATE: " + ("FAIL — a gating family regressed; run with --explain <family>" if failed else "pass"))
    return 1 if failed else 0


def cmd_baseline(args: argparse.Namespace) -> int:
    out = Path(args.out)
    try:
        meta = _read_meta(out)
        cases = load_cases(Path(args.cases_dir), fixtures_dir=Path(args.fixtures_dir))
        baseline = build_baseline(
            read_rows(out / RESULTS_FILE), cases=cases, meta=meta,
            judgements=read_rows(out / JUDGEMENTS_FILE),
        )
    except (ReportError, CaseError) as exc:
        print(f"not promoting: {exc}", file=sys.stderr)
        return 2

    path = _baseline_path(args, meta["model_id"])
    if path.is_file():
        previous = json.loads(path.read_text(encoding="utf-8"))
        flips = sorted(
            cid
            for cid, entry in baseline["cases"].items()
            if cid in (previous.get("cases") or {}) and previous["cases"][cid].get("passed") != entry["passed"]
        )
        print(f"replacing {path.name} (promoted {previous.get('promotedAt')}); {len(flips)} case outcome(s) change:")
        for cid in flips:
            print(f"  {'now passes' if baseline['cases'][cid]['passed'] else 'now fails '}  {cid}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    passed = sum(1 for entry in baseline["cases"].values() if entry["passed"])
    print(f"wrote {path} — {passed}/{len(baseline['cases'])} cases pass on {meta['model_id']}; commit it with the run's score output")
    return 0


# --------------------------------------------------------------------------- CLI


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cases-dir", default=str(CASES_DIR))
    p.add_argument("--fixtures-dir", default=str(FIXTURES_DIR))
    p.add_argument("--baselines-dir", default=str(BASELINES_DIR))
    sub = p.add_subparsers(dest="command", required=True)

    def families(value: str) -> List[str]:
        return [f.strip() for f in value.split(",") if f.strip()]

    s = sub.add_parser("cases", help="validate the golden set and list required grants")
    s.add_argument("--family", type=families)

    s = sub.add_parser("run", help="(spends) run the golden set against a deployed or local stack")
    s.add_argument("--out", required=True, help="run directory (created; resumable)")
    s.add_argument("--model-id", required=True, help="model under test; the baseline is per model (spec D10)")
    s.add_argument("--auth", choices=["cognito", "headless-grant"], default="headless-grant",
                   help="cognito: sign in as the seeded eval user (needs --base-url); "
                        "headless-grant: run as --user-id, the owner of an active grant")
    s.add_argument("--base-url", help="app-api origin for --auth cognito, e.g. https://dev.boisestate.ai/api")
    s.add_argument("--user-id", help="Cognito sub of the grant owner (--auth headless-grant)")
    s.add_argument("--prefix", required=True, help="CDK project prefix, e.g. dev-boisestateai-v2")
    s.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    s.add_argument("--invocations-url", help="send turns to this inference-api base (local stack) instead of the Runtime")
    s.add_argument("--family", type=families, help="comma-separated families (default: all)")
    s.add_argument("--k", type=int, default=3, help="samples per case (default 3)")
    s.add_argument("--workers", type=int, default=4, help="cases run concurrently; turns within a case never are")
    s.add_argument("--timeout", type=float, default=300.0, help="per-turn stream budget in seconds")
    s.add_argument("--keep", action="store_true", help="do not delete the sessions and uploads this run created")
    s.add_argument("--yes", action="store_true", help="spend: without it, print the estimate and stop")

    s = sub.add_parser("judge", help="(spends) score a run's samples with the LLM judges their cases name")
    s.add_argument("--out", required=True)
    s.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL, help="Bedrock model id for every judge")
    s.add_argument("--family", type=families, help="comma-separated families (default: every judged case)")
    s.add_argument("--workers", type=int, default=4, help="samples judged concurrently")
    s.add_argument("--yes", action="store_true", help="spend: without it, print the estimate and stop")

    s = sub.add_parser("score", help="compare a run with its model's baseline; exit 1 on a gating regression")
    s.add_argument("--out", required=True)
    s.add_argument("--baseline", help="baseline file (default: baselines/<model>.json)")
    s.add_argument("--explain", help="print every flipped case in this family, with its samples' model calls")
    s.add_argument("--no-cost-rows", action="store_true",
                   help="with --explain, skip reading the samples' C# cost rows (no AWS)")
    s.add_argument("--json", help="also write the comparison as JSON to this path")

    s = sub.add_parser("baseline", help="promote a complete run to baselines/<model>.json")
    s.add_argument("--out", required=True)
    s.add_argument("--baseline", help="write here instead of baselines/<model>.json")

    s = sub.add_parser("cleanup", help="delete the sessions and fixture uploads a run recorded")
    s.add_argument("--out", required=True)
    s.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))

    args = p.parse_args(argv)
    if getattr(args, "k", 1) < 1:
        p.error("--k must be at least 1")
    if args.command == "run":
        if args.auth == "cognito" and not args.base_url:
            p.error("--auth cognito needs --base-url (the app-api origin it signs in through)")
        if args.auth == "headless-grant" and not args.user_id:
            p.error("--auth headless-grant needs --user-id")
    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = parse_args(argv)
    if args.command == "cases":
        return cmd_cases(args)
    if args.command == "run":
        return asyncio.run(cmd_run(args))
    if args.command == "judge":
        return asyncio.run(cmd_judge(args))
    if args.command == "score":
        return cmd_score(args)
    if args.command == "baseline":
        return cmd_baseline(args)
    return asyncio.run(cmd_cleanup(args))


if __name__ == "__main__":
    sys.exit(main())
