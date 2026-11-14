"""Probe Bedrock's PDF page ceiling and request payload ceiling per model (unified-attachment-delivery §2.1).

The spec found two documented limits that disagree:

* **PDF pages per request:** 100 (the AWS Bedrock documentation) against 600
  for 1M-context models (Anthropic's documentation).
* **Request payload:** 32 MB (AWS) against 20 MB (Anthropic's Bedrock page).

Both decide where the planner's ``native`` mode stops (§4.3), so this asks
Bedrock rather than either page. For each model it sends Converse requests
that differ only in the dimension under test and records which are accepted
and the exact refusal for the rest.

* **Pages:** a PDF of N near-empty pages ("Page k of N"). Every page costs
  page-image tokens (PDFs are read visually on Bedrock with no citations
  config; ``docs/specs/document-citations-probe-findings.md``), and
  ``CountTokens`` refuses document input ("Document input not supported for
  token counting", 2026-11-09), so a 10-page calibration request measures the
  tokens a page costs and a count projected over ``--max-input-tokens`` is
  skipped rather than sent. The boundary between the last accepted and the
  first refused count is then bisected.
* **Payload:** one page whose bytes are an embedded noise image drawn at
  thumbnail size, so the request grows by megabytes while its tokens stay
  near one page. Sizes are chosen around 20 MB and 32 MB on the wire, which
  is the base64 body: about 4/3 of the raw bytes. The same total is also sent
  split across several PDFs, to tell a per-document limit from a
  per-request one.

Each accepted request bills its input tokens and one output token. With the
defaults a full run on one model is a few dollars at most, dominated by the
largest accepted page count.

Usage:
    cd backend
    AWS_PROFILE=dev-ai uv run python scripts/probe_bedrock_document_ceilings.py --dry-run
    AWS_PROFILE=dev-ai uv run python scripts/probe_bedrock_document_ceilings.py \\
        --models us.anthropic.claude-haiku-5-5,us.anthropic.claude-sonnet-4-6 --out probe.json

Synthetic content only: page numbers and random noise.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent))

from large_document_quality import pdf  # noqa: E402

logger = logging.getLogger("document-ceilings-probe")

REGION = "us-west-2"
DEFAULT_MODELS = ("us.anthropic.claude-haiku-5-5", "us.anthropic.claude-sonnet-4-6")
#: Page counts around both documented ceilings.
DEFAULT_PAGES = (100, 101, 600, 601)
#: Wire sizes in MB (base64 body) around both documented payload ceilings.
DEFAULT_WIRE_MB = (19.0, 21.0, 31.0, 33.0)
#: The split variant's documents stay under the 4.5 MB non-PDF document cap,
#: so a refusal there can only be the request's.
SPLIT_PART_MB = 4.0
MB = 1_000_000
QUESTION = "Reply with the single word OK."


@dataclass
class Attempt:
    model_id: str
    probe: str  # "pages" | "payload" | "payload_split"
    pages: int
    documents: int
    raw_bytes: int
    wire_bytes: int
    #: Projected from the calibration request's tokens per page.
    projected_tokens: Optional[int] = None
    sent: bool = False
    accepted: Optional[bool] = None
    input_tokens: Optional[int] = None
    error_code: Optional[str] = None
    error: Optional[str] = None
    seconds: Optional[float] = None
    skipped: Optional[str] = None


@dataclass
class ModelReadout:
    model_id: str
    attempts: List[Attempt] = field(default_factory=list)

    def ceiling(self, probe: str, key: Callable[[Attempt], int]) -> Dict[str, Optional[int]]:
        """The largest accepted and smallest refused value of ``key`` among sent attempts."""
        sent = [a for a in self.attempts if a.probe == probe and a.sent]
        accepted = [key(a) for a in sent if a.accepted]
        refused = [key(a) for a in sent if a.accepted is False]
        return {"maxAccepted": max(accepted) if accepted else None, "minRefused": min(refused) if refused else None}


# --------------------------------------------------------------------------- documents


def pages_pdf(count: int) -> bytes:
    pages = []
    for number in range(1, count + 1):
        page = pdf.Page()
        page.text(72, 720, f"Page {number} of {count}.", size=12)
        pages.append(page)
    return pdf.build(pages)


def padded_pdf(raw_bytes: int, seed: int = 0) -> bytes:
    """One page whose file is about ``raw_bytes`` long: noise in a thumbnail-sized image."""
    overhead = len(pdf.build([pdf.Page()])) + 600
    side = max(1, int(math.sqrt(max(raw_bytes - overhead, 3) / 3)))
    page = pdf.Page()
    page.text(72, 720, "Payload probe page.", size=12)
    page.image(pdf.Image(side, side, random.Random(seed).randbytes(side * side * 3)), 560, 20, 24, 24)
    return pdf.build([page])


def wire_bytes(raw: Sequence[int]) -> int:
    """The JSON body Converse sends: each document base64-encoded, plus a little framing."""
    return sum(4 * math.ceil(n / 3) for n in raw) + 400 * len(raw) + 400


def raw_for_wire(wire_mb: float, documents: int = 1) -> int:
    return int((wire_mb * MB - 400 - 400 * documents) * 3 / 4)


def _content(documents: Sequence[bytes]) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = [
        {"document": {"format": "pdf", "name": f"probe-{i + 1}", "source": {"bytes": data}}}
        for i, data in enumerate(documents)
    ]
    blocks.append({"text": QUESTION})
    return blocks


# --------------------------------------------------------------------------- calls


CALIBRATION_PAGES = 10


def _error(exc: Exception) -> tuple:
    response = getattr(exc, "response", None) or {}
    code = (response.get("Error") or {}).get("Code") or type(exc).__name__
    message = (response.get("Error") or {}).get("Message") or str(exc)
    return str(code), str(message)[:400]


class Prober:
    def __init__(self, client: Any, *, max_input_tokens: int, dry_run: bool) -> None:
        self.client = client
        self.max_input_tokens = max_input_tokens
        self.dry_run = dry_run

    def attempt(
        self, model_id: str, probe: str, documents: Sequence[bytes], pages: int,
        *, tokens_per_page: Optional[float] = None,
    ) -> Attempt:
        raw = [len(d) for d in documents]
        attempt = Attempt(model_id=model_id, probe=probe, pages=pages, documents=len(documents),
                          raw_bytes=sum(raw), wire_bytes=wire_bytes(raw))
        messages = [{"role": "user", "content": _content(documents)}]
        if tokens_per_page:
            attempt.projected_tokens = int(tokens_per_page * pages)
            if attempt.projected_tokens > self.max_input_tokens:
                attempt.skipped = f"~{attempt.projected_tokens:,} projected tokens is over --max-input-tokens"
                return attempt
        if self.dry_run:
            attempt.skipped = "dry run"
            return attempt
        started = time.monotonic()
        attempt.sent = True
        try:
            response = self.client.converse(
                modelId=model_id, messages=messages, inferenceConfig={"maxTokens": 1},
            )
            attempt.accepted = True
            attempt.input_tokens = int((response.get("usage") or {}).get("inputTokens") or 0)
        except Exception as exc:  # noqa: BLE001
            attempt.error_code, attempt.error = _error(exc)
            # Throttling or a timeout says nothing about the ceiling.
            attempt.accepted = None if attempt.error_code in ("ThrottlingException", "ReadTimeoutError") else False
        attempt.seconds = round(time.monotonic() - started, 2)
        return attempt


def probe_pages(prober: Prober, model_id: str, counts: Sequence[int], *, bisect_steps: int) -> List[Attempt]:
    calibration = prober.attempt(model_id, "pages", [pages_pdf(CALIBRATION_PAGES)], CALIBRATION_PAGES)
    per_page = (calibration.input_tokens / CALIBRATION_PAGES) if calibration.input_tokens else None
    if not prober.dry_run and per_page is None:
        logger.warning("%s: the %d-page calibration was not accepted (%s); page counts are sent unprojected",
                       model_id, CALIBRATION_PAGES, calibration.error)
    attempts = [calibration] + [
        prober.attempt(model_id, "pages", [pages_pdf(n)], n, tokens_per_page=per_page) for n in sorted(set(counts))
    ]
    for _ in range(bisect_steps):
        sent = sorted((a for a in attempts if a.sent and a.accepted is not None), key=lambda a: a.pages)
        gap = next(((lo, hi) for lo, hi in zip(sent, sent[1:]) if lo.accepted and not hi.accepted), None)
        if gap is None or gap[1].pages - gap[0].pages <= 1:
            break
        middle = (gap[0].pages + gap[1].pages) // 2
        attempts.append(prober.attempt(model_id, "pages", [pages_pdf(middle)], middle, tokens_per_page=per_page))
    return sorted(attempts, key=lambda a: a.pages)


def probe_payload(prober: Prober, model_id: str, wire_mb: Sequence[float]) -> List[Attempt]:
    attempts = []
    for size in sorted(wire_mb):
        attempts.append(prober.attempt(model_id, "payload", [padded_pdf(raw_for_wire(size))], 1))
        parts = max(1, math.ceil(size / (SPLIT_PART_MB * 4 / 3)))
        each = raw_for_wire(size, parts) // parts
        attempts.append(prober.attempt(
            model_id, "payload_split", [padded_pdf(each, seed=i) for i in range(parts)], parts))
    return attempts


# --------------------------------------------------------------------------- report


def _mb(n: Optional[int]) -> str:
    return "—" if n is None else f"{n / MB:.1f} MB"


def format_report(readouts: Sequence[ModelReadout]) -> str:
    lines = []
    for r in readouts:
        lines += [f"### {r.model_id}", "",
                  "| Probe | Pages | Docs | Raw | Wire | Projected tokens | Result |", "|---|---|---|---|---|---|---|"]
        for a in r.attempts:
            if a.skipped:
                result = f"skipped ({a.skipped})"
            elif a.accepted:
                result = f"accepted ({a.input_tokens:,} input tokens, {a.seconds}s)"
            elif a.accepted is False:
                result = f"refused: {a.error_code}: {a.error}"
            else:
                result = f"inconclusive: {a.error_code}: {a.error}"
            counted = f"~{a.projected_tokens:,}" if a.projected_tokens is not None else "—"
            lines.append(f"| {a.probe} | {a.pages} | {a.documents} | {_mb(a.raw_bytes)} | {_mb(a.wire_bytes)} | "
                         f"{counted} | {result} |")
        pages = r.ceiling("pages", lambda a: a.pages)
        payload = r.ceiling("payload", lambda a: a.wire_bytes)
        split = r.ceiling("payload_split", lambda a: a.wire_bytes)
        lines += ["",
                  f"Pages: largest accepted {pages['maxAccepted']}, smallest refused {pages['minRefused']}. "
                  f"Payload (one PDF): largest accepted {_mb(payload['maxAccepted'])}, smallest refused "
                  f"{_mb(payload['minRefused'])}. Payload (split): largest accepted {_mb(split['maxAccepted'])}, "
                  f"smallest refused {_mb(split['minRefused'])}.", ""]
    return "\n".join(lines)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--models", default=",".join(DEFAULT_MODELS), help="comma-separated Converse model ids")
    p.add_argument("--region", default=REGION)
    p.add_argument("--pages", default=",".join(map(str, DEFAULT_PAGES)), help="page counts to try")
    p.add_argument("--wire-mb", default=",".join(map(str, DEFAULT_WIRE_MB)), help="request sizes (MB on the wire)")
    p.add_argument("--bisect-steps", type=int, default=6, help="bisection steps between accepted and refused pages")
    p.add_argument("--max-input-tokens", type=int, default=1_100_000,
                   help="skip a page count projected above this many input tokens (default 1.1M)")
    p.add_argument("--skip", choices=["pages", "payload"], action="append", default=[])
    p.add_argument("--dry-run", action="store_true", help="build the requests and print their sizes; send nothing")
    p.add_argument("--out", help="write every attempt as JSON here")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    import boto3
    from botocore.config import Config

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = parse_args(argv)
    client = boto3.client("bedrock-runtime", region_name=args.region,
                          config=Config(read_timeout=600, retries={"max_attempts": 2, "mode": "standard"}))
    prober = Prober(client, max_input_tokens=args.max_input_tokens, dry_run=args.dry_run)
    readouts = []
    for model_id in [m.strip() for m in args.models.split(",") if m.strip()]:
        readout = ModelReadout(model_id)
        if "pages" not in args.skip:
            logger.info("%s: pages", model_id)
            readout.attempts += probe_pages(prober, model_id, [int(n) for n in args.pages.split(",")],
                                            bisect_steps=args.bisect_steps)
        if "payload" not in args.skip:
            logger.info("%s: payload", model_id)
            readout.attempts += probe_payload(prober, model_id, [float(n) for n in args.wire_mb.split(",")])
        readouts.append(readout)
    print(format_report(readouts))
    if args.out:
        Path(args.out).write_text(json.dumps([
            {"modelId": r.model_id, "attempts": [asdict(a) for a in r.attempts]} for r in readouts
        ], indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
