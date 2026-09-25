"""Compaction quality veto, slice 1 — does a compaction arm lose facts the full history keeps?

Scoping: docs/kaizen/scoping/2026-09-21-quality-veto-harness.md (slice 1).
Waiver it exists to close: docs/specs/compaction-model-relative-thresholds.md §5.

Four steps, each reading the previous one's output from ``--out``:

  cut      Build the seeded corpus and replay every transcript through the
           PRODUCTION compaction code under each arm. No model, no AWS, no cost
           (unless --summary-model). Writes each arm's probe-time history and a
           free "availability" table: is each planted value still anywhere in
           the context the model would get?
  records  (optional, spends) Generate LTM-shaped summary records for
           ``cut --summary records``. See compaction_quality/records.py.
  ask      (spends) One Converse call per (variant, arm, plant, sample).
           Prints the estimate and stops unless --yes.
  score    Exact-match scoring, paired per family against the control arm,
           exact McNemar, n on every row. No pooled score.

Arms (compaction_quality/arms.py): ``full`` (control, no compaction),
``model_relative`` (prod defaults), ``legacy`` (kill switch), ``floor_50``
(tuning). Paces: ``restore`` (default, worst case), ``cold``, ``warm``.

The corpus is ours and synthetic — no user content anywhere. Pointing this at
recorded production conversations is a separate decision (scoping §5).

Usage:
    cd backend
    uv run python scripts/compaction_quality_harness.py cut --out /tmp/cq
    AWS_PROFILE=dev-ai uv run python scripts/compaction_quality_harness.py ask --out /tmp/cq \\
        --model-id us.anthropic.claude-haiku-4-5-20251001-v1:0 --k 3          # prints the estimate
    AWS_PROFILE=dev-ai uv run python scripts/compaction_quality_harness.py ask --out /tmp/cq ... --yes
    uv run python scripts/compaction_quality_harness.py score --out /tmp/cq
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from compaction_quality.arms import (  # noqa: E402
    PACES, SUMMARY_MODES, default_arms, history_tokens, plant_availability, simulate,
)
from compaction_quality.corpus import BASE_SEED, CorpusConfig, Plant, build_corpus  # noqa: E402

DEFAULT_ARMS = "full,model_relative,legacy"


def _git_sha() -> Optional[str]:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:  # noqa: BLE001
        return None


def _write_jsonl(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _pct(num: int, den: int) -> str:
    return f"{100 * num / den:5.1f}%" if den else "   — "


def cmd_cut(args: argparse.Namespace) -> int:
    out = Path(args.out)
    (out / "runs").mkdir(parents=True, exist_ok=True)
    config = CorpusConfig(turns=args.turns)
    corpus = build_corpus(args.variants, config, args.seed)
    arms = default_arms(summary_model_enabled=args.summary_model)
    selected = [a.strip() for a in args.arms.split(",") if a.strip()]
    unknown = [a for a in selected if a not in arms]
    if unknown:
        print(f"unknown arm(s): {unknown}; known: {sorted(arms)}", file=sys.stderr)
        return 2

    records_for: Any = None
    if args.summary == "records":
        from compaction_quality.records import records_lookup
        records_for = records_lookup(out / "records.json")

    manifest = {
        "gitSha": _git_sha(), "seed": args.seed, "variants": args.variants, "corpus": config.to_dict(),
        "arms": {n: arms[n].description for n in selected}, "pace": args.pace,
        "contextWindow": args.window, "overheadTokens": args.overhead, "summaryMode": args.summary,
        "summaryModel": args.summary_model,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _write_jsonl(out / "plants.jsonl", [
        {"variant": t.variant, **p.to_dict()} for t in corpus for p in t.plants
    ])

    availability: List[Dict[str, Any]] = []
    summaries: Dict[str, Dict[str, List[int]]] = defaultdict(lambda: defaultdict(list))
    for transcript in corpus:
        for name in selected:
            run = simulate(
                transcript, arms[name], pace=args.pace, context_window=args.window,
                overhead_tokens=args.overhead, summary_mode=args.summary,
                records_for_turn=records_for(transcript.variant) if records_for else None,
            )
            (out / "runs" / f"{transcript.variant:03d}-{name}.json").write_text(
                json.dumps(run.to_dict()), encoding="utf-8",
            )
            availability.extend(plant_availability(run, transcript))
            s = summaries[name]
            s["cuts"].append(run.cuts)
            s["peak"].append(run.peak_input_tokens)
            s["probe"].append(run.probe_input_tokens)
            s["forced"].append(sum(1 for e in run.events if e["kind"] == "forced"))
            s["floorUnreachable"].append(sum(1 for e in run.events if e["kind"] == "floor_unreachable"))
            s["summaryTokens"].append(len(run.summary or "") // 4)
    _write_jsonl(out / "availability.jsonl", availability)

    families = sorted({r["family"] for r in availability})
    report: Dict[str, Any] = {"manifest": manifest, "arms": {}}
    print(f"\n{args.variants} transcripts x {args.turns} turns, pace={args.pace}, window={args.window:,}, "
          f"summary={args.summary}, overhead={args.overhead:,}\n")
    header = f"{'arm':<16}{'cuts/sess':>10}{'peak in':>10}{'probe in':>10}{'forced':>8}{'floor!':>8}{'summ tok':>9}"
    header += "".join(f"{f[:10]:>12}" for f in families) + f"{'retained':>10}"
    print(header)
    for name in selected:
        s = summaries[name]
        rows = [r for r in availability if r["arm"] == name]
        by_family = {
            f: (sum(1 for r in rows if r["family"] == f and r["inContext"]), sum(1 for r in rows if r["family"] == f))
            for f in families
        }
        retained = (sum(1 for r in rows if r["statementRetained"]), len(rows))
        n = len(s["cuts"])
        line = (
            f"{name:<16}{sum(s['cuts']) / n:>10.1f}{sum(s['peak']) // n:>10,}{sum(s['probe']) // n:>10,}"
            f"{sum(s['forced']):>8}{sum(s['floorUnreachable']):>8}{sum(s['summaryTokens']) // n:>9,}"
        )
        line += "".join(f"{_pct(*by_family[f]):>12}" for f in families) + f"{_pct(*retained):>10}"
        print(line)
        report["arms"][name] = {
            "sessions": n, "meanCuts": sum(s["cuts"]) / n, "meanPeakInputTokens": sum(s["peak"]) / n,
            "meanProbeInputTokens": sum(s["probe"]) / n, "forcedCuts": sum(s["forced"]),
            "floorUnreachable": sum(s["floorUnreachable"]), "meanSummaryTokens": sum(s["summaryTokens"]) / n,
            "inContextByFamily": {f: {"hits": h, "n": t} for f, (h, t) in by_family.items()},
            "statementRetained": {"hits": retained[0], "n": retained[1]},
        }
    print("\nFamily columns = share of planted values still anywhere in the probe-time context (free upper bound on")
    print("what a model can answer). 'retained' = share whose stating turn survived the slice.")
    (out / "cut_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {out}/cut_report.json, availability.jsonl, runs/")
    return 0


def _load_runs(out: Path) -> List[Dict[str, Any]]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted((out / "runs").glob("*.json"))]


def _plants_by_variant(out: Path) -> Dict[int, List[Plant]]:
    grouped: Dict[int, List[Plant]] = defaultdict(list)
    for row in _read_jsonl(out / "plants.jsonl"):
        variant = int(row.pop("variant"))
        grouped[variant].append(Plant.from_dict(row))
    return grouped


def cmd_ask(args: argparse.Namespace) -> int:
    from compaction_quality.ask import estimate_cost, load_done, plan_jobs, run_jobs

    out = Path(args.out)
    runs = _load_runs(out)
    if args.arms:
        wanted = {a.strip() for a in args.arms.split(",")}
        runs = [r for r in runs if r["arm"] in wanted]
    if args.limit_variants is not None:
        runs = [r for r in runs if int(r["variant"]) < args.limit_variants]
    plants = _plants_by_variant(out)
    availability = {
        (int(r["variant"]), r["arm"], r["plantId"]): r for r in _read_jsonl(out / "availability.jsonl")
    }
    per_group = args.k * max(len(v) for v in plants.values())
    estimate = estimate_cost(
        {(int(r["variant"]), r["arm"]): history_tokens(r["history"]) for r in runs}, per_group, args.model_id,
    )
    print(json.dumps(estimate, indent=2))
    if not args.yes:
        print("\nEstimate only. Re-run with --yes to spend.")
        return 0

    import boto3
    from botocore.config import Config

    client = boto3.client(
        "bedrock-runtime", region_name=args.region,
        config=Config(read_timeout=300, retries={"max_attempts": 2, "mode": "standard"}),
    )
    results_path = out / "results.jsonl"
    done = load_done(results_path)
    written = run_jobs(
        client, plan_jobs(runs, plants, availability, args.k), results_path,
        model_id=args.model_id, temperature=args.temperature, done=done,
    )
    print(f"wrote {written} rows to {results_path} ({len(done)} already present)")
    return 0


def cmd_records(args: argparse.Namespace) -> int:
    from compaction_quality.records import build_records

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    corpus = build_corpus(args.variants, CorpusConfig(turns=args.turns), args.seed)
    chunks = sum(-(-len(t.turns) // args.chunk_turns) for t in corpus)
    print(f"{chunks} summarization calls on {args.model_id}")
    if not args.yes:
        print("Estimate only. Re-run with --yes to spend.")
        return 0
    import boto3

    client = boto3.client("bedrock-runtime", region_name=args.region)
    data = {str(t.variant): build_records(client, t, model_id=args.model_id, chunk_turns=args.chunk_turns) for t in corpus}
    (out / "records.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"wrote {out}/records.json")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    from compaction_quality.score import report

    out = Path(args.out)
    results = _read_jsonl(out / "results.jsonl")
    result = report(results, baseline=args.baseline)
    (out / "score.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\npaired vs '{args.baseline}' (majority of k per task; exact McNemar)\n")
    print(f"{'arm':<16}{'family':<12}{'n':>5}{'arm acc':>9}{'base acc':>10}{'losses':>8}{'wins':>6}{'p':>8}")
    for row in result["byFamily"]:
        acc = f"{row['armAccuracy']:.2f}" if row["armAccuracy"] is not None else "—"
        base = f"{row['baselineAccuracy']:.2f}" if row["baselineAccuracy"] is not None else "—"
        print(f"{row['arm']:<16}{row['family']:<12}{row['n']:>5}{acc:>9}{base:>10}"
              f"{row['lossesVsBaseline']:>8}{row['winsVsBaseline']:>6}{row['pValue']:>8.3f}")
    print("\nby whether the stating turn survived the slice:")
    for row in result["byRetention"]:
        acc = f"{row['accuracy']:.2f}" if row["accuracy"] is not None else "—"
        print(f"  {row['arm']:<16} retained={str(row['statementRetained']):<6} n={row['n']:<5} acc={acc}")
    print(f"\nwrote {out}/score.json")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def corpus_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--out", required=True, help="Run directory (created).")
        p.add_argument("--variants", type=int, default=12, help="Transcripts (x9 plants each = tasks).")
        p.add_argument("--turns", type=int, default=48)
        p.add_argument("--seed", type=int, default=BASE_SEED)

    cut = sub.add_parser("cut", help="Replay the corpus through each arm (free).")
    corpus_args(cut)
    cut.add_argument("--arms", default=DEFAULT_ARMS)
    cut.add_argument("--pace", choices=PACES, default="restore")
    cut.add_argument("--window", type=int, default=200_000, help="maxInputTokens (200k Haiku 4.5; 1000000 Sonnet 5).")
    cut.add_argument("--overhead", type=int, default=15_000, help="System prompt + tool tokens counted toward the ceiling.")
    cut.add_argument("--summary", choices=SUMMARY_MODES, default="fallback")
    cut.add_argument("--summary-model", action="store_true",
                     help="Let bound_summary call Nova Micro when records exceed the budget (spends; needs AWS).")
    cut.set_defaults(func=cmd_cut)

    rec = sub.add_parser("records", help="Generate LTM-shaped summary records (spends).")
    corpus_args(rec)
    rec.add_argument("--model-id", default="us.anthropic.claude-haiku-4-5-20251001-v1:0")
    rec.add_argument("--chunk-turns", type=int, default=8)
    rec.add_argument("--region", default="us-west-2")
    rec.add_argument("--yes", action="store_true")
    rec.set_defaults(func=cmd_records)

    ask = sub.add_parser("ask", help="Ask every planted question on every arm (spends).")
    ask.add_argument("--out", required=True)
    ask.add_argument("--model-id", default="us.anthropic.claude-haiku-4-5-20251001-v1:0")
    ask.add_argument("--k", type=int, default=3)
    ask.add_argument("--temperature", type=float, default=None, help="Omit for the model default (production).")
    ask.add_argument("--arms", default=None, help="Subset of the arms `cut` produced.")
    ask.add_argument("--limit-variants", type=int, default=None, help="Smoke test on the first N variants.")
    ask.add_argument("--region", default="us-west-2")
    ask.add_argument("--yes", action="store_true")
    ask.set_defaults(func=cmd_ask)

    score = sub.add_parser("score", help="Score results.jsonl (free).")
    score.add_argument("--out", required=True)
    score.add_argument("--baseline", default="full")
    score.set_defaults(func=cmd_score)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
