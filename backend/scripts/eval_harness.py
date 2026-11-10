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
            call, and --no-cost-rows skips it. --publish also writes the
            comparison as one LEDGER#EVAL row into the deployment's
            system-cost-rollup table (release-ledger spec §5.2), keyed by the
            Runtime build that served the turns; best-effort, idempotent per
            run, and it never changes the exit code.
  baseline  Promote --out to baselines/<model>.json. Refuses unless every case
            in the golden set ran at the run's full k. No AWS.
  cleanup   Delete the sessions and fixture uploads recorded in --out.

Usage (from backend/):

    uv run python scripts/eval_harness.py cases
    # As the eval user (seeded once per environment by scripts/seed_eval_user.py):
    AWS_PROFILE=dev-ai uv run python scripts/eval_harness.py run --out /tmp/eval-1 \
        --auth cognito --base-url https://dev.boisestate.ai/api --prefix dev-boisestateai-v2 \
        --model-id us.anthropic.claude-haiku-5-5 --yes
    # As any other owner of an active headless grant:
    AWS_PROFILE=dev-ai uv run python scripts/eval_harness.py run --out /tmp/eval-1 \\
        --user-id <cognito-sub> --prefix dev-boisestateai-v2 \\
        --model-id us.anthropic.claude-haiku-5-5            # estimate only
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
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from apis.shared.costs.ledger_publish import EVAL_TRACKS  # noqa: E402
from apis.shared.evals.cases import (  # noqa: E402
    ADVISORY_FAMILIES,
    DEFAULT_FAMILIES,
    FAMILIES,
    OPT_IN_FAMILIES,
    CaseError,
    EPHEMERAL_PROJECT,
    EvalCase,
    fixture_key,
    fixture_keys,
    load_cases,
    required_grants,
)
from eval_fixtures.build import GENERATED  # noqa: E402
from eval_fixtures.build import build as build_fixture  # noqa: E402
from eval_fixtures.delivery import estimated_tokens as fixture_tokens  # noqa: E402
from eval_fixtures.references import reference_for  # noqa: E402
from apis.shared.evals.judges import DEFAULT_JUDGE_MODEL, same_base_model  # noqa: E402
from apis.shared.evals.report import (  # noqa: E402
    ReportError,
    any_gate_failed,
    baseline_filename,
    build_baseline,
    case_ledger_entries,
    case_outcomes,
    compare_judges,
    compare_run,
    explain_family,
    format_judge_table,
    format_table,
    load_baseline,
    merge_family_baseline,
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
#: Sessions created, and large documents uploaded, before their samples ran.
#: Cleanup reads it too, so a run stopped between staging and running leaves nothing behind.
STAGED_FILE = "staged.jsonl"
#: The seed the large-document quality readouts used, so the documents are the
#: ones those readouts measured (``large_document_quality/fixtures.py``).
LARGE_DOCUMENT_SEED = 7
#: A judge call carries the scrubbed session plus the evaluator's own prompt.
#: Sized from strands-evals' built-in prompts; refine from the first run.
EST_JUDGE_INPUT_TOKENS = 2_500
EST_JUDGE_OUTPUT_TOKENS = 300

#: Per-MTok input/output, Regional (us.*) rates, only to size the pre-flight
#: estimate. The catalog (curated-models.ts) is the source of truth.
RATES: Dict[str, Tuple[float, float]] = {
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": (1.10, 5.50),
    "us.anthropic.claude-haiku-5-5": (0.11, 0.55),
    "us.anthropic.claude-sonnet-4-6": (3.30, 16.50),
    "us.anthropic.claude-sonnet-5": (2.20, 11.00),
}
FALLBACK_RATE_MODEL = "us.anthropic.claude-sonnet-4-6"
#: Base-rate-equivalent tokens per turn, measured: the first full k=3 run on dev
#: (Haiku 4.5, 2026-10-30, 219 turns) averaged ~2.1k uncached input, ~0.5k cache
#: write (1.25x), ~1.9k cache read (0.1x) and ~130 output tokens per turn, about
#: $0.86 in all. These figures add a third on top, so the estimate stays high.
EST_INPUT_TOKENS_PER_TURN = 4_000
#: Cache write and read multiples of the input rate, for sized attachments.
EST_CACHE_WRITE = 1.25
EST_CACHE_READ = 0.1
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
    # Sized attachments (the delivery family's): written to the cache on the
    # first turn and read on each later one. Others are inside the per-turn figure.
    for case, _ in todo:
        attached = sum(fixture_tokens(name) for name in case.files)
        dollars += attached * rate_in * (EST_CACHE_WRITE + EST_CACHE_READ * (len(case.turns) - 1)) / 1_000_000
    note = "" if rate_model == model_id else f" (priced at {rate_model} rates; no table entry for {model_id})"
    return turns, dollars, note


class CachedBearerAuth:
    """Reuse a minted bearer for many turns instead of a Cognito exchange per turn.

    One bearer per user: a project-memory case runs turns as the second eval
    member too, and a single cached token would send that member's turns as
    the eval user, which is exactly the boundary those cases test.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._tokens: Dict[str, Tuple[str, float]] = {}
        self._lock = asyncio.Lock()

    async def mint_bearer_for_user(self, user_id: str) -> str:
        async with self._lock:
            cached = self._tokens.get(user_id)
            if cached is None or time.monotonic() - cached[1] > BEARER_REUSE_SECONDS:
                cached = (await self._inner.mint_bearer_for_user(user_id), time.monotonic())
                self._tokens[user_id] = cached
            return cached[0]


def _eval_credentials(prefix: str, region: str, account: Any = None) -> Tuple[str, str]:
    """An eval account's username and password (the eval user unless ``account`` names another).

    For the eval user, ``EVAL_USERNAME`` / ``EVAL_PASSWORD`` win when set; else
    the SSM SecureString ``seed_eval_user.py`` wrote. Never logged.
    """
    from seed_eval_user import MAIN, credentials_parameter

    account = account or MAIN
    username = os.environ.get("EVAL_USERNAME") if account is MAIN else None
    password = os.environ.get("EVAL_PASSWORD") if account is MAIN else None
    if username and password:
        return username, password
    import boto3

    ssm = boto3.client("ssm", region_name=region)
    parameter = credentials_parameter(prefix, account)
    try:
        raw = ssm.get_parameter(Name=parameter, WithDecryption=True)["Parameter"]["Value"]
    except ssm.exceptions.ParameterNotFound as exc:
        raise ReportError(
            f"no eval user credentials at {parameter}; run scripts/seed_eval_user.py for this environment first"
        ) from exc
    creds = json.loads(raw)
    return creds["username"], creds["password"]


async def sign_in_eval_user(
    base_url: str, prefix: str, region: str, *, resolve_fixtures: bool = False
) -> Tuple[str, Dict[str, str]]:
    """Sign in as the eval user, renew its headless grant, and return its user id.

    With ``resolve_fixtures``, also returns the seeded fixtures' ids
    (``seed_eval_fixtures.resolve``: ``"project:<name>"`` / ``"agent:<name>"``),
    read in the same session; otherwise an empty map.
    """
    from smoke_turns import AppApiTransport

    username, password = _eval_credentials(prefix, region)
    transport = AppApiTransport(base_url, label="eval")
    fixtures: Dict[str, str] = {}
    try:
        await transport.login_cognito(username, password, "")
        await transport.enable_headless_grant()
        if resolve_fixtures:
            from seed_eval_fixtures import resolve

            fixtures = await resolve(transport)
    finally:
        await transport.close()
    user_id = transport.user.get("user_id")
    if not user_id:
        raise ReportError("signed in, but GET /auth/session named no user_id")
    return str(user_id), fixtures


# --------------------------------------------------------------------------- cases


def cmd_cases(args: argparse.Namespace) -> int:
    try:
        cases = load_cases(Path(args.cases_dir), families=args.family, fixtures_dir=Path(args.fixtures_dir), generated=GENERATED)
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
            gate = "advisory" if family in ADVISORY_FAMILIES else "gating"
            if family in OPT_IN_FAMILIES:
                gate += ", opt-in (run only with --family)"
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


def _target_build(base_url: Optional[str], invocations_url: Optional[str]) -> Dict[str, str]:
    """The target's ``appVersion`` and ``build``, from app-api's ``/health`` or the
    Runtime's ``/ping`` (release-ledger spec §5.1). Best effort: {} when neither answers."""
    import urllib.request

    for url in (f"{base_url.rstrip('/')}/health" if base_url else None,
                f"{invocations_url.rstrip('/')}/ping" if invocations_url else None):
        if not url:
            continue
        try:
            with urllib.request.urlopen(url, timeout=10) as response:  # noqa: S310 — our own target
                body = json.loads(response.read().decode("utf-8"))
        except Exception:  # noqa: BLE001 — metadata only, never blocks a run
            continue
        if isinstance(body, dict) and body.get("version"):
            return {"appVersion": str(body["version"]), "build": str(body.get("build") or "unknown")}
    return {}


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
        cases = load_cases(Path(args.cases_dir), families=args.family or list(DEFAULT_FAMILIES),
                           fixtures_dir=Path(args.fixtures_dir), generated=GENERATED)
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

    from apis.shared.evals.execute import EVAL_TRIGGER, OAuthRequiredError, QuotaExceededError, run_sample
    from apis.shared.evals.fixtures import (
        FixtureError,
        load_fixture,
        mime_type_for,
        register_upload,
        upload_via_presign,
    )
    from apis.shared.harness.auth import CognitoRefreshBearerAuth, HeadlessAuthError
    from apis.shared.harness.runner import run_agent_headless

    resolve_runtime_env(args.prefix, args.region)
    needed = fixture_keys(cases)
    fixture_ids: Dict[str, str] = {}
    ephemeral = any(c.run.project == EPHEMERAL_PROJECT for c in cases)
    accounts = None
    if (needed or ephemeral) and args.auth != "cognito":
        print("cases run against seeded or per-sample projects or agents, which need --auth cognito", file=sys.stderr)
        return 2
    if ephemeral:
        # Memory cases create, join and read back projects for the whole run, as
        # both eval users, so their sessions stay open until the run ends.
        from eval_project_memory import EvalAccounts, ProjectMemoryError

        try:
            accounts = await EvalAccounts.open(
                args.base_url, args.prefix, args.region,
                member2=any(c.run.member2_role for c in cases),
            )
            if needed:
                from seed_eval_fixtures import resolve

                fixture_ids = await resolve(accounts.main)
        except (ReportError, RuntimeError, ProjectMemoryError) as exc:
            print(f"could not sign in the eval users: {exc}", file=sys.stderr)
            return 2
        args.user_id = accounts.main_user_id
        print("signed in as the eval users; headless grants renewed")
        missing = [key for key in needed if key not in fixture_ids]
        if missing:
            print(f"fixtures not seeded: {', '.join(missing)}; run scripts/seed_eval_fixtures.py first",
                  file=sys.stderr)
            await accounts.close()
            return 2
    elif args.auth == "cognito":
        try:
            args.user_id, fixture_ids = await sign_in_eval_user(
                args.base_url, args.prefix, args.region, resolve_fixtures=bool(needed)
            )
        except (ReportError, RuntimeError) as exc:
            print(f"could not sign in as the eval user: {exc}", file=sys.stderr)
            return 2
        print("signed in as the eval user; headless grant renewed")
        missing = [key for key in needed if key not in fixture_ids]
        if missing:
            print(f"fixtures not seeded: {', '.join(missing)}; run scripts/seed_eval_fixtures.py first",
                  file=sys.stderr)
            return 2
    uploader = None
    if args.upload_via == "presign" and any(case.files for case in cases):
        from smoke_turns import AppApiTransport

        uploader = AppApiTransport(args.base_url, label="eval:uploads")
        try:
            await uploader.login_cognito(*_eval_credentials(args.prefix, args.region), "")
        except RuntimeError as exc:
            print(f"could not sign in to upload through presign: {exc}", file=sys.stderr)
            await uploader.close()
            return 2
        print("uploads go through presign → PUT → complete")
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
    if "appVersion" not in meta:
        meta.update(_target_build(args.base_url, args.invocations_url))
        (out / META_FILE).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    auth = CachedBearerAuth(CognitoRefreshBearerAuth())
    fixtures_dir = Path(args.fixtures_dir)

    async def run_turn(as_user: str = "eval", **kwargs: Any) -> Any:
        return await run_agent_headless(
            user_id=accounts.user_id(as_user) if accounts is not None else args.user_id,
            auth=auth,
            invocations_base_url=args.invocations_url,
            trigger=EVAL_TRIGGER,
            timeout_seconds=args.timeout,
            **kwargs,
        )

    async def register_fixture(session_id: str, filename: str) -> str:
        if filename in GENERATED:
            data, mime_type = build_fixture(filename), mime_type_for(filename)
        else:
            data, mime_type = load_fixture(fixtures_dir, filename)
        if uploader is not None:
            return await upload_via_presign(uploader, session_id, filename, mime_type, data)
        return await register_upload(args.user_id, session_id, filename, mime_type, data)

    try:
        staged = await _stage_large_documents(out, todo, args)
    except FixtureError as exc:
        print(f"could not stage large documents: {exc}", file=sys.stderr)
        return 2

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
            project = None
            try:
                if case.run.project == EPHEMERAL_PROJECT:
                    from eval_project_memory import EphemeralProject

                    project = await EphemeralProject.create(accounts, case, sample)
                row = await run_sample(
                    case,
                    sample=sample,
                    model_id=args.model_id,
                    run_turn=run_turn,
                    register_fixture=register_fixture,
                    fixtures_dir=fixtures_dir,
                    staged=staged.get((case.id, sample)),
                    assistant_id=project.harness_agent_id if project else fixture_ids.get(fixture_key(case) or ""),
                    inspect=project.inspect if project else None,
                )
            except (OAuthRequiredError, QuotaExceededError, HeadlessAuthError, FixtureError) as exc:
                fatal.append(f"{type(exc).__name__}: {exc}")
                return
            except Exception as exc:  # noqa: BLE001 — a project that cannot be set up stops the run
                if project is None and case.run.project == EPHEMERAL_PROJECT:
                    fatal.append(f"could not set up {case.id}'s project: {exc}")
                    return
                raise
            finally:
                if project is not None:
                    await project.purge()
            with results_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
            new_rows.append(row)
            verdict = "PASS" if row["passed"] else "FAIL"
            detail = "" if row["passed"] else "  " + "; ".join(row["failures"])
            print(f"{verdict}  {case.id} [{sample}]{detail}", flush=True)

    await asyncio.gather(*(worker() for _ in range(max(1, args.workers))))

    if not args.keep:
        unrun = _unrun_staged(out, read_rows(results_path))
        await _cleanup(new_rows + unrun, args.user_id,
                       member2_user_id=accounts.member2_user_id if accounts is not None else None)
        _forget_staged(out, unrun)
    if uploader is not None:
        await uploader.close()
    if accounts is not None:
        if accounts.member2_user_id and meta.get("member2_user_id") != accounts.member2_user_id:
            meta["member2_user_id"] = accounts.member2_user_id
            (out / META_FILE).write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        await accounts.close()

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


# --------------------------------------------------------------------------- large documents


def _unrun_staged(out: Path, rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Staged sessions whose sample never wrote a row, shaped like rows for ``_cleanup``."""
    ran = {row.get("session_id") for row in rows}
    return [entry for entry in read_rows(out / STAGED_FILE) if entry["session_id"] not in ran]


def _forget_staged(out: Path, deleted: Sequence[Dict[str, Any]]) -> None:
    """Drop cleaned-up entries from ``staged.jsonl``, so a resumed run stages those samples afresh
    instead of reusing uploads that no longer exist."""
    if not deleted:
        return
    gone = {entry["session_id"] for entry in deleted}
    keep = [entry for entry in read_rows(out / STAGED_FILE) if entry["session_id"] not in gone]
    (out / STAGED_FILE).write_text("".join(json.dumps(entry) + "\n" for entry in keep), encoding="utf-8")


def _echo_documents(states: Dict[str, Dict[str, Any]]) -> None:
    counts = Counter(
        "ready to ask" if s["settled"] else (s["preparation"] if s["preparation"] != "ready" else "indexing")
        for s in states.values()
    )
    waited = max((s["waitedSeconds"] for s in states.values()), default=0)
    print(f"  documents after {waited}s: " + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())), flush=True)


async def _stage_large_documents(
    out: Path, todo: Sequence[Tuple[EvalCase, int]], args: argparse.Namespace
) -> Dict[Tuple[str, int], Any]:
    """Upload every large document the pending samples name, then wait for all of them at once.

    Each sample gets its own session and its own copy of each document,
    because attachment search is scoped to one user in one conversation. A
    staged sample from an earlier, interrupted invocation is reused rather
    than uploaded again.
    """
    from apis.shared.evals.execute import StagedSample, new_session_id
    from apis.shared.evals.fixtures import FixtureError, register_reference_upload, wait_for_documents

    wanted = [(case, sample) for case, sample in todo if case.large_documents]
    if not wanted:
        return {}
    if not os.environ.get("LARGE_DOCUMENT_PREPARE_QUEUE_URL"):
        import boto3

        try:
            os.environ["LARGE_DOCUMENT_PREPARE_QUEUE_URL"] = boto3.client("sqs", region_name=args.region).get_queue_url(
                QueueName=f"{args.prefix}-large-document-prepare"
            )["QueueUrl"]
        except Exception as exc:  # noqa: BLE001 — reported as a setup error
            raise FixtureError(f"no large-document prepare queue for {args.prefix}: {exc}") from exc

    from large_document_quality.fixtures import build_all

    names = sorted({name for case, _ in wanted for name in case.large_documents})
    documents = {fixture.name: fixture for fixture in build_all(LARGE_DOCUMENT_SEED, names)}
    staged_path = out / STAGED_FILE
    earlier = {(e["case_id"], int(e["sample"])): e for e in read_rows(staged_path)}

    async def stage(case: EvalCase, sample: int) -> Dict[str, Any]:
        if (case.id, sample) in earlier:
            return earlier[(case.id, sample)]
        session_id = new_session_id()
        upload_ids = []
        for name in case.large_documents:
            doc = documents[name]
            upload_ids.append(
                await register_reference_upload(args.user_id, session_id, doc.filename, doc.mime_type, doc.data)
            )
        entry = {"case_id": case.id, "sample": sample, "session_id": session_id,
                 "upload_ids": upload_ids, "documents": list(case.large_documents)}
        with staged_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
        return entry

    megabytes = sum(len(documents[n].data) for c, _ in wanted for n in c.large_documents) / 1e6
    print(f"staging {len(wanted)} samples with large documents ({megabytes:.0f} MB to upload)", flush=True)
    entries = await asyncio.gather(*(stage(case, sample) for case, sample in wanted))
    states = await wait_for_documents(
        args.user_id,
        [u for e in entries for u in e["upload_ids"]],
        timeout=args.document_timeout,
        echo=_echo_documents,
    )
    unsettled = sum(1 for s in states.values() if not s["settled"])
    if unsettled:
        print(f"  {unsettled} document(s) not searchable after {args.document_timeout:.0f}s; "
              "their samples run anyway, as a user asking early would", flush=True)
    return {
        (e["case_id"], int(e["sample"])): StagedSample(
            session_id=e["session_id"],
            upload_ids=tuple(e["upload_ids"]),
            documents=tuple({"name": n, **states[u]} for n, u in zip(e["documents"], e["upload_ids"])),
        )
        for e in entries
    }


# --------------------------------------------------------------------------- cleanup


async def _cleanup(
    rows: Sequence[Dict[str, Any]], user_id: str, *, member2_user_id: Optional[str] = None
) -> Tuple[int, int]:
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
        # A memory case's turns span sessions, some of them the second member's.
        owned = [(s["session_id"], member2_user_id if s.get("as_user") == "member2" else user_id)
                 for s in row.get("sessions") or [{"session_id": row["session_id"]}]]
        for session_id, owner in owned:
            if not owner:
                logger.warning("no user to delete session %s as", session_id)
                continue
            try:
                if await service.delete_session(user_id=owner, session_id=session_id):
                    sessions += 1
            except Exception as exc:  # noqa: BLE001
                logger.warning("could not delete session %s: %s", session_id, exc)
    print(f"cleanup: deleted {sessions} session(s) and {uploads} fixture upload(s)")
    return sessions, uploads


async def cmd_cleanup(args: argparse.Namespace) -> int:
    from smoke_turns import resolve_runtime_env

    if args.orphans:
        return await _sweep_orphans(args)
    out = Path(args.out)
    try:
        meta = _read_meta(out)
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    resolve_runtime_env(meta["prefix"], meta.get("region") or args.region)
    rows = read_rows(out / RESULTS_FILE)
    unrun = _unrun_staged(out, rows)
    await _cleanup(rows + unrun, meta["user_id"], member2_user_id=meta.get("member2_user_id"))
    _forget_staged(out, unrun)
    return 0


def _parse_iso(value: str) -> datetime:
    """An ISO timestamp as an aware datetime (``Z`` or an offset); naive values are UTC."""
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def _sweep_orphans(args: argparse.Namespace) -> int:
    """Delete what runs left behind under the eval user: uploads and ``eval-`` sessions older than ``--older-than``.

    A run cleans up after itself, except when it is killed: a cancelled CI job
    never reaches its cleanup, and its ``staged.jsonl`` goes with the runner,
    so the large documents it staged stay in the bucket and the attachments KB.
    The eval user is the harness's own account (spec D7), so everything under
    it belongs to some run; the age cutoff keeps the sweep off a run still in
    progress. Lists only, unless ``--yes``.
    """
    from datetime import timedelta

    from seed_eval_user import eval_user_id
    from smoke_turns import _looks_like_prod, resolve_runtime_env

    from apis.app_api.sessions.services.session_service import SessionService
    from apis.shared.evals.execute import EVAL_SESSION_PREFIX
    from apis.shared.evals.fixtures import delete_objects_under, delete_upload, stale_uploads
    from apis.shared.sessions.metadata import list_user_sessions

    if not args.prefix:
        print("--orphans needs --prefix", file=sys.stderr)
        return 2
    if _looks_like_prod(None, args.prefix):
        print("refusing: this looks like production (spec D8)", file=sys.stderr)
        return 2
    user_id = eval_user_id(args.prefix, args.region)
    if not user_id:
        print(f"no eval user on {args.prefix}; nothing to sweep")
        return 0
    resolve_runtime_env(args.prefix, args.region)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=args.older_than)

    import boto3

    bucket = os.environ["S3_USER_FILES_BUCKET_NAME"]
    listing = [
        obj
        for page in boto3.client("s3", region_name=args.region).get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=f"user-files/{user_id}/"
        )
        for obj in page.get("Contents", [])
    ]
    uploads = stale_uploads(listing, user_id, cutoff)

    sessions = []
    token = None
    while True:
        page, token = await list_user_sessions(user_id, limit=100, next_token=token)
        sessions.extend(
            m.session_id for m in page
            if m.session_id.startswith(EVAL_SESSION_PREFIX) and _parse_iso(m.last_message_at) < cutoff
        )
        if not token:
            break

    print(f"eval user on {args.prefix}: {len(uploads)} upload(s) and {len(sessions)} session(s) "
          f"older than {args.older_than:g} min")
    if not args.yes:
        for session_id, upload_id in uploads:
            print(f"  upload  {session_id}/{upload_id}")
        for session_id in sessions:
            print(f"  session {session_id}")
        print("dry run: pass --yes to delete")
        return 0

    removed = 0
    for session_id, upload_id in uploads:
        try:
            if not await delete_upload(user_id, upload_id):
                # No row left (an earlier, partial cleanup): the objects are all that remain.
                await delete_objects_under(bucket, f"user-files/{user_id}/{session_id}/{upload_id}/")
            removed += 1
        except Exception as exc:  # noqa: BLE001 — a sweep reports, never aborts
            logger.warning("could not delete upload %s: %s", upload_id, exc)
    service = SessionService()
    ended = 0
    for session_id in sessions:
        try:
            ended += 1 if await service.delete_session(user_id=user_id, session_id=session_id) else 0
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not delete session %s: %s", session_id, exc)
    print(f"swept {removed} upload(s) and {ended} session(s)")
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
                reference=reference_for(case, Path(args.fixtures_dir)) if "grounded" in case.judge.evaluators else None,
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
    session_ids = [str(r.get("session_id")) for r in rows if r.get("family") == family and r.get("session_id")]
    found = await _session_calls(meta, session_ids)
    if found is not None:
        print(f"cost rows: read {len(found)} of {len(set(session_ids))} sessions in {family}", file=sys.stderr)
    return found


async def _session_calls(
    meta: Dict[str, Any], session_ids: Sequence[str]
) -> Optional[Dict[str, Sequence[Dict[str, Any]]]]:
    """``{session_id: C# rows}`` for ``session_ids``; ``None`` without a prefix, ``{}`` without access."""
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
    try:
        storage = DynamoDBStorage()
    except Exception as exc:  # noqa: BLE001 — a diagnostic never fails score
        print(f"cost rows: unavailable ({exc})", file=sys.stderr)
        return {}
    return await fetch_session_calls(session_ids, storage)


def _publish_eval(
    meta: Dict[str, Any],
    rows: Sequence[Dict[str, Any]],
    comparisons: Sequence[Any],
    judge_comparisons: Sequence[Any],
    judge_model: Optional[str],
    *,
    baseline: Mapping[str, Any],
    track: str,
) -> bool:
    """Write the comparison as a ``LEDGER#EVAL`` row (release-ledger spec §5.2). Best-effort.

    The row carries each case's outcome as ids and failure codes
    (``case_ledger_entries``, eval-harness spec §16 D20) and the run's
    ``track``, so the Evaluations page can draw trends and a flip matrix.

    The row is keyed by the build that served the turns: the Runtime's, read
    from the run's own ``C#`` rows, else ``imageTags["inference-api"]``. Never
    the meta's top-level ``build``, which is app-api's (``/health``).
    """
    from smoke_turns import _looks_like_prod

    from apis.shared.costs import ledger_publish as lp

    prefix, region = meta.get("prefix"), meta.get("region")
    if not prefix or _looks_like_prod(None, str(prefix)):
        print(f"ledger: not published (prefix {prefix!r} is missing or looks like production)", file=sys.stderr)
        return False
    ids = lp.session_ids(rows)
    found = asyncio.run(_session_calls(meta, ids)) or {}
    calls = [c for sid in ids for c in found.get(sid, ())]
    image_tag = lp.runtime_image_tag(meta.get("imageTags"))
    served = lp.served_build(calls, image_tag=image_tag, app_version=meta.get("appVersion"))
    if served is None:
        print("ledger: not published (no C# row carries a build and meta.json has no inference-api image tag)",
              file=sys.stderr)
        return False
    if served.source == lp.BUILD_FROM_COST_ROWS and image_tag and image_tag != served.build:
        print(f"ledger: the Runtime's image tag at run start was {image_tag}; the turns ran on {served.build}",
              file=sys.stderr)
    try:
        row = lp.eval_ledger_row(
            meta, comparisons, served=served, result_rows=rows, judge_comparisons=judge_comparisons,
            judge_model=judge_model, call_rows=calls,
            cases=case_ledger_entries(rows, baseline.get("cases")), track=track,
        )
        lp.assert_content_free(row, lp.EVAL_ROW_FIELDS, forbidden=[*ids, meta.get("user_id") or ""])
    except lp.PublishError as exc:
        print(f"ledger: not published ({exc})", file=sys.stderr)
        return False
    if not lp.put_ledger_row(row, prefix=str(prefix), region=region):
        print("ledger: publish failed; see the log line above. The score stands.", file=sys.stderr)
        return False
    print(f"ledger: published {lp.describe(row)}")
    return True


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
    models: List[str] = []
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
                    "run": {key: meta.get(key) for key in ("startedAt", "target", "gitCommit", "imageTags", "appVersion", "build")},
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
    if args.publish:
        print()
        _publish_eval(meta, rows, comparisons, judge_comparisons, models[0] if len(models) == 1 else None,
                      baseline=baseline, track=args.track)
    failed = any_gate_failed(comparisons)
    print("\nGATE: " + ("FAIL — a gating family regressed; run with --explain <family>" if failed else "pass"))
    return 1 if failed else 0


def cmd_baseline(args: argparse.Namespace) -> int:
    out = Path(args.out)
    try:
        meta = _read_meta(out)
        wanted = args.family or list(DEFAULT_FAMILIES)
        cases = load_cases(Path(args.cases_dir), families=wanted, fixtures_dir=Path(args.fixtures_dir),
                           generated=GENERATED)
        rows = [r for r in read_rows(out / RESULTS_FILE) if r.get("family") in wanted]
        judgements = [j for j in read_rows(out / JUDGEMENTS_FILE) if j.get("family") in wanted]
        baseline = build_baseline(rows, cases=cases, meta=meta, judgements=judgements)
    except (ReportError, CaseError) as exc:
        print(f"not promoting: {exc}", file=sys.stderr)
        return 2

    path = _baseline_path(args, meta["model_id"])
    if args.family and path.is_file():
        try:
            baseline = merge_family_baseline(json.loads(path.read_text(encoding="utf-8")), baseline)
        except ReportError as exc:
            print(f"not promoting: {exc}", file=sys.stderr)
            return 2
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


def cmd_readout(args: argparse.Namespace) -> int:
    from apis.shared.evals.delivery_readout import delivery_readout, format_delivery_readout

    out = Path(args.out)
    try:
        meta = _read_meta(out)
    except ReportError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    rows = [r for r in read_rows(out / RESULTS_FILE) if not args.family or r.get("family") in args.family]
    if not rows:
        print(f"no results in {out / RESULTS_FILE}" + (f" for {', '.join(args.family)}" if args.family else ""),
              file=sys.stderr)
        return 2
    calls = None
    if not args.no_cost_rows:
        calls = asyncio.run(_session_calls(meta, [str(r["session_id"]) for r in rows if r.get("session_id")]))
    readouts = delivery_readout(rows, calls)
    print(format_delivery_readout(readouts, title=f"{meta['model_id']}, k={meta['k']}"))
    if args.json:
        Path(args.json).write_text(json.dumps({
            "modelId": meta["model_id"], "k": meta["k"],
            "run": {key: meta.get(key) for key in ("startedAt", "target", "gitCommit", "imageTags", "appVersion")},
            "cases": [r.to_dict() for r in readouts],
        }, indent=2) + "\n", encoding="utf-8")
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
    s.add_argument("--document-timeout", type=float, default=900.0,
                   help="seconds to wait for staged large documents to become searchable (default 900)")
    s.add_argument("--upload-via", choices=["direct", "presign"], default="direct",
                   help="direct: write each fixture's S3 object and READY row (default); presign: upload as the "
                        "SPA does, POST /files/presign → PUT → complete, so app-api's admission runs "
                        "(needs --auth cognito)")
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
    s.add_argument("--publish", action="store_true",
                   help="also write the comparison as a LEDGER#EVAL row (release-ledger spec §5.2; needs AWS "
                        "credentials for the run's account); best-effort, never changes the exit code")
    s.add_argument("--track", choices=EVAL_TRACKS, default="manual",
                   help="with --publish, which pass this run was; the Evaluations page draws one trend per "
                        "track (eval-harness spec §16)")

    s = sub.add_parser("baseline", help="promote a complete run to baselines/<model>.json")
    s.add_argument("--out", required=True)
    s.add_argument("--baseline", help="write here instead of baselines/<model>.json")
    s.add_argument("--family", type=families,
                   help="promote only these families' cases, keeping every other family's entries in the file")

    s = sub.add_parser("readout", help="per-case quality, cost and speed of a run (unified-attachment-delivery §8.5)")
    s.add_argument("--out", required=True)
    s.add_argument("--family", type=families, help="comma-separated families (default: every family in the run)")
    s.add_argument("--json", help="also write the readout as JSON to this path")
    s.add_argument("--no-cost-rows", action="store_true", help="skip reading the samples' C# rows (no AWS)")

    s = sub.add_parser("cleanup", help="delete the sessions and fixture uploads a run recorded")
    s.add_argument("--out", help="the run directory whose sessions and uploads to delete")
    s.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    s.add_argument("--orphans", action="store_true",
                   help="instead of --out: sweep the eval user's leftover uploads and eval- sessions on --prefix")
    s.add_argument("--prefix", help="with --orphans: the deployment to sweep")
    s.add_argument("--older-than", type=float, default=60.0,
                   help="with --orphans: only what is older than this many minutes (default 60)")
    s.add_argument("--yes", action="store_true", help="with --orphans: delete; without it, list only")

    args = p.parse_args(argv)
    if args.command == "cleanup" and not (args.out or args.orphans):
        p.error("cleanup needs --out (one run) or --orphans (a sweep)")
    if getattr(args, "k", 1) < 1:
        p.error("--k must be at least 1")
    if args.command == "run":
        if args.auth == "cognito" and not args.base_url:
            p.error("--auth cognito needs --base-url (the app-api origin it signs in through)")
        if args.auth == "headless-grant" and not args.user_id:
            p.error("--auth headless-grant needs --user-id")
        if args.upload_via == "presign" and args.auth != "cognito":
            p.error("--upload-via presign needs --auth cognito (an app-api session to upload as)")
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
    if args.command == "readout":
        return cmd_readout(args)
    return asyncio.run(cmd_cleanup(args))


if __name__ == "__main__":
    sys.exit(main())
