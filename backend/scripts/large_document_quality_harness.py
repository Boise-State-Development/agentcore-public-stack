"""Large-document quality harness: does semantic search (Tier 2) beat read-on-demand (Tier 1)?

Spec: docs/specs/large-document-attachments.md §6 ("Quality, before promoting
Tier 2"). Tier 1 needs no knowledge base and always works; Tier 2 is a paid
managed KB. This compares the two on the same documents, the same questions and
the same model. The arms differ only in whether ``document_read`` has ``query``.
"Ship Tier 2 if arm 2 measurably beats arm 1 on the scanned and layout classes."

Steps, each reading the previous one's output from ``--out``:

  fixtures  Build the four synthetic documents (text, scanned, layout, docx)
            and their questions. Deterministic per --seed. No AWS.
  seed      Upload them to a deployed environment as a synthetic user's
            reference attachments and queue their preparation, as app-api does
            after an upload. Needs --yes.
  wait      Poll the rows until every document is prepared and indexed
            (--text-indexed accepts text-searchable).
  probe     The free check: can each tier reach the planted fact at all?
            The real ``pattern`` read and the real ``Retrieve`` (~$0.001 each).
  ask       (spends) One fresh agent turn per (arm, question, sample). Prints the
            estimate and stops unless --yes. Resumable: rows already in
            results.jsonl are skipped.
  score     Per-class comparison (majority of k, exact McNemar, n on every row)
            and the operational table (tool calls, query use, tokens, $, s).
  cleanup   Delete the seeded rows and objects (the delete events take them out
            of the knowledge base). Needs --yes.

Usage:
    cd backend
    uv run python scripts/large_document_quality_harness.py fixtures --out /tmp/ldq
    AWS_PROFILE=dev-ai uv run python scripts/large_document_quality_harness.py seed --out /tmp/ldq \\
        --prefix dev-boisestateai-v2 --yes
    AWS_PROFILE=dev-ai uv run python scripts/large_document_quality_harness.py wait --out /tmp/ldq
    AWS_PROFILE=dev-ai uv run python scripts/large_document_quality_harness.py probe --out /tmp/ldq
    AWS_PROFILE=dev-ai uv run python scripts/large_document_quality_harness.py ask --out /tmp/ldq --k 3   # estimate
    AWS_PROFILE=dev-ai uv run python scripts/large_document_quality_harness.py ask --out /tmp/ldq --k 3 --yes
    uv run python scripts/large_document_quality_harness.py score --out /tmp/ldq
    AWS_PROFILE=dev-ai uv run python scripts/large_document_quality_harness.py cleanup --out /tmp/ldq --yes

The fixtures are synthetic: no user content, no real people or organisations.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from large_document_quality import fixtures as fx  # noqa: E402
from large_document_quality.fixtures import Question  # noqa: E402

DEFAULT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
DEFAULT_REGION = "us-west-2"


def _git_sha() -> Optional[str]:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return None


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _questions(out: Path) -> List[Question]:
    rows = _read_jsonl(out / "questions.jsonl")
    return [Question(**{**r, "expected": tuple(r["expected"]), "forbidden": tuple(r["forbidden"])}) for r in rows]


def _seeded(out: Path) -> Dict[str, Dict[str, Any]]:
    path = out / "seeded.json"
    if not path.exists():
        raise SystemExit(f"{path} not found: run `seed` first")
    return {doc["name"]: doc for doc in json.loads(path.read_text(encoding="utf-8"))["documents"]}


def _environment(out: Path, args: argparse.Namespace) -> Dict[str, str]:
    from large_document_quality.dev import configure_environment

    manifest_path = out / "seeded.json"
    prefix = args.prefix
    if not prefix and manifest_path.exists():
        prefix = json.loads(manifest_path.read_text(encoding="utf-8"))["prefix"]
    if not prefix:
        raise SystemExit("--prefix is required (e.g. dev-boisestateai-v2)")
    return configure_environment(prefix, args.region)


# ---------------------------------------------------------------------------


def cmd_fixtures(args: argparse.Namespace) -> int:
    out = Path(args.out)
    (out / "documents").mkdir(parents=True, exist_ok=True)
    names = [n.strip() for n in args.docs.split(",") if n.strip()]
    documents = []
    questions: List[Dict[str, Any]] = []
    for fixture in fx.build_all(args.seed, names):
        (out / "documents" / fixture.filename).write_bytes(fixture.data)
        documents.append({
            "name": fixture.name, "filename": fixture.filename, "docClass": fixture.doc_class,
            "sizeBytes": len(fixture.data), "sha256": hashlib.sha256(fixture.data).hexdigest(),
            "questions": len(fixture.questions),
        })
        questions.extend(q.to_dict() for q in fixture.questions)
        print(f"  {fixture.doc_class:8} {fixture.filename:38} {len(fixture.data) / 1e6:5.1f} MB  "
              f"{len(fixture.questions)} questions")
    (out / "questions.jsonl").write_text("".join(json.dumps(q) + "\n" for q in questions), encoding="utf-8")
    (out / "fixtures.json").write_text(json.dumps({"seed": args.seed, "gitSha": _git_sha(), "documents": documents},
                                                  indent=2), encoding="utf-8")
    print(f"wrote {len(documents)} documents and {len(questions)} questions to {out}")
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    from large_document_quality.dev import seed

    out = Path(args.out)
    if (out / "seeded.json").exists():
        print(f"{out / 'seeded.json'} exists: this run is already seeded (clean up first to re-seed)", file=sys.stderr)
        return 2
    manifest = json.loads((out / "fixtures.json").read_text(encoding="utf-8"))
    fixtures = fx.build_all(manifest["seed"], [d["name"] for d in manifest["documents"]])
    for fixture, recorded in zip(fixtures, manifest["documents"]):
        if hashlib.sha256(fixture.data).hexdigest() != recorded["sha256"]:
            raise SystemExit(f"{fixture.filename} no longer matches fixtures.json; rebuild the fixtures")
    resources = _environment(out, args)
    total = sum(len(f.data) for f in fixtures) / 1e6
    print(f"seed {len(fixtures)} documents ({total:.1f} MB) into {args.prefix}: bucket {resources['bucket']}, "
          f"queue {resources['queue']}")
    if not args.yes:
        print("dry run: pass --yes to upload")
        return 0
    run_id = uuid.uuid4().hex[:8]
    seeded = asyncio.run(seed(fixtures, run_id, args.region, resources))
    (out / "seeded.json").write_text(json.dumps({"prefix": args.prefix, "region": args.region, "runId": run_id,
                                                 "documents": seeded}, indent=2), encoding="utf-8")
    print(f"seeded run {run_id}; next: wait")
    return 0


def _echo_status(rows: List[Dict[str, Any]]) -> None:
    line = "  ".join(f"{r['name']}={r['preparation']}/{r['kbIndex'] or '-'}{'(text)' if r['textIndexed'] else ''}"
                     for r in rows)
    print(f"[{time.strftime('%H:%M:%S')}] {line}", flush=True)


def cmd_wait(args: argparse.Namespace) -> int:
    from large_document_quality.dev import is_settled, wait

    out = Path(args.out)
    _environment(out, args)
    seeded = list(_seeded(out).values())
    rows = asyncio.run(wait(seeded, full=not args.text_indexed, timeout=args.timeout, poll=args.poll, echo=_echo_status))
    (out / "status.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    pending = [r["name"] for r in rows if not is_settled(r, full=not args.text_indexed)]
    if pending:
        print(f"timed out waiting for {pending}", file=sys.stderr)
        return 1
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    from large_document_quality.probe import probe

    out = Path(args.out)
    _environment(out, args)
    rows = asyncio.run(probe(_questions(out), _seeded(out)))
    (out / "probe.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    print(f"{'question':18} {'wording':10}  tier1 located/value   tier2 located/value  pages")
    for r in rows:
        t1, t2 = r["tier1"], r["tier2"]
        print(f"{r['qid']:18} {r['wording']:10}  {str(t1['located']):>7}/{str(t1['value']):5}       "
              f"{str(t2.get('located')):>7}/{str(t2.get('value', t2.get('error'))):5}  {t2.get('pages', '')}")
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    from large_document_quality.ask import ARMS, estimate_usd, jobs, run_all

    out = Path(args.out)
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        print(f"unknown arm(s) {unknown}; known: {list(ARMS)}", file=sys.stderr)
        return 2
    questions = _questions(out)
    if args.only:
        wanted = {q.strip() for q in args.only.split(",")}
        questions = [q for q in questions if q.qid in wanted or q.doc in wanted]
    results_path = out / "results.jsonl"
    done = {r["key"] for r in _read_jsonl(results_path) if not r.get("error")}
    pending = jobs(questions, arms, args.k, done)
    estimate = estimate_usd(len(pending), args.model_id)
    print(f"{len(pending)} turns pending ({len(done)} done) on {args.model_id}; "
          f"estimate {'$%.2f' % estimate if estimate is not None else 'unknown (no rate)'}")
    if not args.yes or not pending:
        if pending:
            print("dry run: pass --yes to spend")
        return 0
    _environment(out, args)
    lock = threading.Lock()
    # Errored rows are retried on a re-run; keep the file to rows that count.
    kept = [r for r in _read_jsonl(results_path) if not r.get("error")]
    results_path.write_text("".join(json.dumps(r) + "\n" for r in kept), encoding="utf-8")

    def on_row(row: Dict[str, Any]) -> None:
        with lock:
            with results_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row) + "\n")
        mark = "ERR" if row["error"] else ("ok " if row["correct"] else "no ")
        print(f"  {mark} {row['arm']:5} {row['qid']:16} #{row['sample']} {'/'.join(row['modes']) or '-':40} "
              f"{row['seconds']:5.1f}s", flush=True)

    asyncio.run(run_all(pending, _seeded(out), model_id=args.model_id, region=args.region,
                        temperature=args.temperature, concurrency=args.concurrency, on_row=on_row))
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    from large_document_quality.score import compare, operations

    out = Path(args.out)
    rows = [r for r in _read_jsonl(out / "results.jsonl") if not r.get("error")]
    if not rows:
        print("no results yet", file=sys.stderr)
        return 1
    table = compare(rows)
    print(f"{'class':8} {'wording':10} {'n':>3}  {'tier1':>5} {'tier2':>5}  {'only1':>5} {'only2':>5}  {'p':>6}")
    for r in table:
        print(f"{r['class']:8} {r['wording']:10} {r['n']:3d}  {r['tier1']:5d} {r['tier2']:5d}  "
              f"{r['onlyControl']:5d} {r['onlyTreatment']:5d}  {r['p']:6.3f}")
    ops = operations(rows)
    print()
    print(f"{'arm':5} {'class':8} {'turns':>5} {'err':>3} {'noAns':>5} {'calls':>5} {'query':>5} {'pages':>5} "
          f"{'in tok':>7} {'$/turn':>7} {'sec':>5}")
    for r in ops:
        usd = f"{r['usdPerTurn']:.4f}" if r["usdPerTurn"] is not None else "   —"
        print(f"{r['arm']:5} {r['class']:8} {r['turns']:5d} {r['errors']:3d} {r['noAnswerLine']:5d} {r['medianToolCalls']:5.1f} "
              f"{r['queryTurns']:5d} {r['pageReadTurns']:5d} {r['medianInputTokens']:7.0f} {usd:>7} "
              f"{r['medianSeconds']:5.1f}")
    (out / "score.json").write_text(json.dumps({"compare": table, "operations": ops}, indent=2), encoding="utf-8")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    from large_document_quality.dev import cleanup

    out = Path(args.out)
    seeded = list(_seeded(out).values())
    print(f"delete {len(seeded)} seeded uploads: {[d['uploadId'] for d in seeded]}")
    if not args.yes:
        print("dry run: pass --yes to delete")
        return 0
    _environment(out, args)
    deleted = asyncio.run(cleanup(seeded, args.region))
    (out / "seeded.json").rename(out / f"seeded.cleaned-{int(time.time())}.json")
    print(f"deleted {len(seeded)} rows and {deleted} objects")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, aws: bool = True) -> None:
        p.add_argument("--out", required=True)
        if aws:
            p.add_argument("--prefix", default="", help="stack prefix, e.g. dev-boisestateai-v2 (seed records it)")
            p.add_argument("--region", default=DEFAULT_REGION)

    p = sub.add_parser("fixtures")
    common(p, aws=False)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--docs", default=",".join(fx.BUILDERS))
    p.set_defaults(fn=cmd_fixtures)

    p = sub.add_parser("seed")
    common(p)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_seed)

    p = sub.add_parser("wait")
    common(p)
    p.add_argument("--text-indexed", action="store_true", help="settle on text-searchable, not fully indexed")
    p.add_argument("--timeout", type=float, default=3600)
    p.add_argument("--poll", type=float, default=30)
    p.set_defaults(fn=cmd_wait)

    p = sub.add_parser("probe")
    common(p)
    p.set_defaults(fn=cmd_probe)

    p = sub.add_parser("ask")
    common(p)
    p.add_argument("--model-id", default=DEFAULT_MODEL)
    p.add_argument("--arms", default="tier1,tier2")
    p.add_argument("--k", type=int, default=3)
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--only", default="", help="comma-separated question ids or document names")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("score")
    common(p, aws=False)
    p.set_defaults(fn=cmd_score)

    p = sub.add_parser("cleanup")
    common(p)
    p.add_argument("--yes", action="store_true")
    p.set_defaults(fn=cmd_cleanup)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
