#!/usr/bin/env python3
"""Seed the eval harness's fixture projects and agents (docs/specs/eval-harness.md D13).

Signs in as the eval user (``seed_eval_user.py`` first) and, for every fixture in
``scripts/eval_fixtures/entities.py``, makes sure one exists named
``[eval] <name>`` whose description carries the current digest of its
definition and files. A fixture whose digest matches is left alone, so a re-run
costs a few list calls. One whose digest differs is deleted and created again,
and one that is missing is created. A project's or agent's files are uploaded
the way the SPA uploads them (presigned PUT), and the seed waits until every
file is ``complete``: a new project's first file waits for the dispatcher tick
and the knowledge base's ingestion, up to about 15 minutes, which is why the
fixtures persist instead of being created per run.

A ``superseded`` file is uploaded first, waited for, and deleted before its
replacement goes up, so the knowledge base's replace path runs and the old
value is a planted distractor.

Usage (from ``backend/``, with credentials for the stack's account):

    AWS_PROFILE=dev-ai uv run python scripts/seed_eval_fixtures.py \\
        --base-url https://dev.boisestate.ai/api --prefix dev-boisestateai-v2
    ... --dry-run    # report what would change, write nothing

Production is refused, as everywhere in the harness (spec D8).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from eval_fixtures import entities  # noqa: E402

logger = logging.getLogger("seed_eval_fixtures")

#: How long a fixture's files may take to become ``complete``, by default.
DEFAULT_TIMEOUT_S = 1800
POLL_S = 15
TERMINAL = {"complete", "failed"}


class SeedError(RuntimeError):
    pass


def _expect(status: int, body: Any, *ok: int, what: str) -> Any:
    if status not in ok:
        raise SeedError(f"{what} -> {status}: {str(body)[:300]}")
    return body


async def list_fixtures(transport) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """The eval user's ``[eval] ``-named projects and agents, by fixture name."""
    status, body = await transport.call("GET", "/projects")
    projects = {
        p["name"][len(entities.NAME_PREFIX):]: p
        for p in (_expect(status, body, 200, what="GET /projects") or {}).get("projects", [])
        if str(p.get("name", "")).startswith(entities.NAME_PREFIX) and p.get("status") == "active"
    }
    status, body = await transport.call("GET", "/assistants", params={"include_drafts": "true", "limit": 1000})
    agents = {
        a["name"][len(entities.NAME_PREFIX):]: a
        for a in (_expect(status, body, 200, what="GET /assistants") or {}).get("assistants", [])
        if str(a.get("name", "")).startswith(entities.NAME_PREFIX)
    }
    return projects, agents


async def resolve(transport) -> Dict[str, str]:
    """``{"project:<name>" | "agent:<name>": assistant id}`` for every seeded fixture.

    A project resolves to its harness Agent, which is what a turn sends as
    ``rag_assistant_id``. A fixture whose digest is stale resolves anyway, with
    a warning: the run measures what is there, and the seed is what fixes it.
    """
    projects, agents = await list_fixtures(transport)
    ids: Dict[str, str] = {}
    for name, p in projects.items():
        ids[f"project:{name}"] = p["harnessAgentId"]
        if name in entities.PROJECTS and p.get("description") != entities.description(entities.PROJECTS[name]):
            logger.warning("fixture project %s is stale; run seed_eval_fixtures.py", name)
    for name, a in agents.items():
        ids[f"agent:{name}"] = a["assistantId"]
        if name in entities.AGENTS and a.get("description") != entities.description(entities.AGENTS[name]):
            logger.warning("fixture agent %s is stale; run seed_eval_fixtures.py", name)
    return ids


async def _upload(transport, base: str, f: entities.FixtureFile) -> str:
    """Upload one file the way the SPA does; returns its document id."""
    import httpx

    from apis.shared.evals.fixtures import mime_type_for

    data, mime = f.build(), mime_type_for(f.name)
    status, body = await transport.call(
        "POST", f"{base}/upload-url", json_body={"filename": f.name, "contentType": mime, "sizeBytes": len(data)}
    )
    body = _expect(status, body, 200, 201, what=f"POST {base}/upload-url ({f.name})")
    async with httpx.AsyncClient(timeout=120) as s3:
        put = await s3.put(body["uploadUrl"], content=data, headers={"Content-Type": mime})
    if put.status_code not in (200, 204):
        raise SeedError(f"PUT {f.name} to its presigned URL -> {put.status_code}: {put.text[:200]}")
    return str(body["documentId"])


async def _wait(transport, base: str, ids: Sequence[str], timeout_s: float) -> None:
    """Until every document in ``ids`` is ``complete``; ``failed`` or the timeout is an error."""
    deadline = time.monotonic() + timeout_s
    pending = set(ids)
    while pending:
        status, body = await transport.call("GET", base, params={"limit": 1000})
        docs = {str(d["documentId"]): d for d in (_expect(status, body, 200, what=f"GET {base}") or {}).get("documents", [])}
        for doc_id in list(pending):
            state = (docs.get(doc_id) or {}).get("status")
            if state == "complete":
                pending.discard(doc_id)
            elif state == "failed":
                raise SeedError(f"{docs[doc_id].get('filename')} failed to ingest")
        if not pending:
            return
        if time.monotonic() > deadline:
            names = [(docs.get(d) or {}).get("filename", d) for d in pending]
            raise SeedError(f"files not complete after {timeout_s:.0f}s: {names}")
        logger.info("waiting for %d file(s) under %s", len(pending), base)
        await asyncio.sleep(POLL_S)


async def _delete_project(transport, project: Dict[str, Any]) -> None:
    pid = project["projectId"]
    status, body = await transport.call("PATCH", f"/projects/{pid}", json_body={"status": "archived"})
    _expect(status, body, 200, what=f"archive project {pid}")
    status, body = await transport.call("DELETE", f"/projects/{pid}")
    _expect(status, body, 204, what=f"purge project {pid}")


async def seed_project(transport, fixture: entities.ProjectFixture, timeout_s: float) -> str:
    status, body = await transport.call(
        "POST", "/projects",
        json_body={"name": entities.NAME_PREFIX + fixture.name, "description": entities.description(fixture)},
    )
    project = _expect(status, body, 201, what=f"create project {fixture.name}")
    pid = project["projectId"]
    status, body = await transport.call("PUT", f"/projects/{pid}/instructions", json_body={"instructions": fixture.instructions})
    _expect(status, body, 200, what=f"instructions for {fixture.name}")
    base = f"/projects/{pid}/knowledge"
    if fixture.superseded:
        old = [await _upload(transport, base, f) for f in fixture.superseded]
        await _wait(transport, base, old, timeout_s)
        for doc_id in old:
            status, body = await transport.call("DELETE", f"{base}/{doc_id}")
            _expect(status, body, 204, what=f"delete superseded {doc_id}")
    new = [await _upload(transport, base, f) for f in fixture.files]
    await _wait(transport, base, new, timeout_s)
    return pid


async def seed_agent(transport, fixture: entities.AgentFixture, timeout_s: float) -> str:
    bindings = [{"kind": "tool", "ref": t} for t in fixture.tools] + [{"kind": "skill", "ref": s} for s in fixture.skills]
    status, body = await transport.call("POST", "/assistants", json_body={
        "name": entities.NAME_PREFIX + fixture.name,
        "description": entities.description(fixture),
        "instructions": fixture.instructions,
        "visibility": "PRIVATE",
        "bindings": bindings or None,
    })
    agent = _expect(status, body, 200, 201, what=f"create agent {fixture.name}")
    aid = agent["assistantId"]
    if fixture.files:
        base = f"/assistants/{aid}/documents"
        ids = [await _upload(transport, base, f) for f in fixture.files]
        await _wait(transport, base, ids, timeout_s)
    return aid


async def seed(transport, *, dry_run: bool, timeout_s: float) -> List[str]:
    """Bring every fixture to its current definition; returns one line per fixture."""
    projects, agents = await list_fixtures(transport)
    report: List[str] = []
    work = []
    for name, fixture in entities.PROJECTS.items():
        have = projects.get(name)
        if have and have.get("description") == entities.description(fixture):
            report.append(f"project {name}: current")
            continue
        report.append(f"project {name}: {'replace' if have else 'create'}")
        work.append(("project", fixture, have))
    for name, fixture in entities.AGENTS.items():
        have = agents.get(name)
        if have and have.get("description") == entities.description(fixture):
            report.append(f"agent {name}: current")
            continue
        report.append(f"agent {name}: {'replace' if have else 'create'}")
        work.append(("agent", fixture, have))
    if dry_run or not work:
        return report

    async def one(kind: str, fixture, have) -> None:
        if kind == "project":
            if have:
                await _delete_project(transport, have)
            await seed_project(transport, fixture, timeout_s)
        else:
            if have:
                status, body = await transport.call("DELETE", f"/assistants/{have['assistantId']}")
                _expect(status, body, 204, what=f"delete agent {fixture.name}")
            await seed_agent(transport, fixture, timeout_s)
        logger.info("%s %s seeded", kind, fixture.name)

    # Concurrent, so the slow first-file waits overlap rather than add up.
    await asyncio.gather(*(one(*item) for item in work))
    return report


async def _main(args: argparse.Namespace) -> int:
    from eval_harness import _eval_credentials
    from smoke_turns import AppApiTransport, _looks_like_prod

    if _looks_like_prod(args.base_url, args.prefix):
        print("refusing: that target looks like production (spec D8)", file=sys.stderr)
        return 2
    username, password = _eval_credentials(args.prefix, args.region)
    transport = AppApiTransport(args.base_url, label="eval-fixtures")
    try:
        await transport.login_cognito(username, password, "")
        report = await seed(transport, dry_run=args.dry_run, timeout_s=args.timeout)
    except SeedError as exc:
        print(f"seed failed: {exc}", file=sys.stderr)
        return 1
    finally:
        await transport.close()
    for line in report:
        print(line)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--base-url", required=True, help="app-api origin, e.g. https://dev.boisestate.ai/api")
    p.add_argument("--prefix", required=True, help="CDK project prefix, e.g. dev-boisestateai-v2")
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="seconds to wait for files to be complete")
    p.add_argument("--dry-run", action="store_true", help="report what would change and write nothing")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
