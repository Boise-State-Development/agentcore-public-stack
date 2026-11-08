"""Projects of a sample's own, for the project-memory cases (eval-harness spec D14, D16, EH-6).

A ``project_memory`` sample runs in a project nobody else's sample touches:
created before its first turn, its memory seeded from the case, the second eval
member added with the case's role, and purged after the read-back. A persistent
project would carry one sample's saves into the next, and k samples in parallel
would write into each other.

``EvalAccounts`` holds both eval users' app-api sessions open for the run, so
each project can be created, joined and read back as the right person.
``EphemeralProject.inspect`` reads the memory a case's ``expect.memory`` scores:
each scope's entries and its MEMORY.md index, as one text, plus the pending
proposals.

An empty project provisions no knowledge base (born-managed knowledge bases are
created by the first upload), so a project per sample costs rows and two memory
spaces. A run stopped before its ``finally`` leaves ``[eval-tmp] `` projects
behind; ``seed_eval_fixtures.py`` purges those over an hour old.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

logger = logging.getLogger("eval_project_memory")

TMP_PREFIX = "[eval-tmp] "


class ProjectMemoryError(RuntimeError):
    pass


def _ok(status: int, body: Any, *codes: int, what: str) -> Any:
    if status not in codes:
        raise ProjectMemoryError(f"{what} -> {status}: {str(body)[:300]}")
    return body


@dataclass
class EvalAccounts:
    """The eval user's (and, when a case needs one, the second member's) live app-api sessions."""

    main: Any
    main_user_id: str
    member2: Optional[Any] = None
    member2_user_id: Optional[str] = None
    member2_email: Optional[str] = None

    @classmethod
    async def open(cls, base_url: str, prefix: str, region: str, *, member2: bool) -> "EvalAccounts":
        from eval_harness import _eval_credentials
        from seed_eval_user import MEMBER2
        from smoke_turns import AppApiTransport

        async def sign_in(account) -> Any:
            username, password = _eval_credentials(prefix, region, account)
            transport = AppApiTransport(base_url, label=f"eval:{username}")
            await transport.login_cognito(username, password, "")
            # Each user's turns go through the Runtime on its own headless grant.
            await transport.enable_headless_grant()
            return transport

        main = await sign_in(None)
        accounts = cls(main=main, main_user_id=str(main.user.get("user_id") or ""))
        if member2:
            second = await sign_in(MEMBER2)
            accounts.member2 = second
            accounts.member2_user_id = str(second.user.get("user_id") or "")
            accounts.member2_email = str(second.user.get("email") or MEMBER2.email)
        if not accounts.main_user_id or (member2 and not accounts.member2_user_id):
            raise ProjectMemoryError("signed in, but GET /auth/session named no user_id")
        return accounts

    def user_id(self, as_user: str) -> str:
        if as_user == "member2":
            if not self.member2_user_id:
                raise ProjectMemoryError("a turn as member2, but member2 is not signed in")
            return self.member2_user_id
        return self.main_user_id

    async def close(self) -> None:
        for transport in (self.main, self.member2):
            if transport is not None:
                await transport.close()


@dataclass
class EphemeralProject:
    accounts: EvalAccounts
    project_id: str
    harness_agent_id: str
    spaces: Dict[str, str] = field(default_factory=dict)
    before: Dict[str, str] = field(default_factory=dict)

    @classmethod
    async def create(cls, accounts: EvalAccounts, case: Any, sample: int) -> "EphemeralProject":
        main = accounts.main
        name = f"{TMP_PREFIX}{case.id.split('.', 1)[1]}-{sample}-{uuid.uuid4().hex[:6]}"[:80]
        status, body = await main.call("POST", "/projects", json_body={"name": name, "description": "eval ephemeral"})
        project = _ok(status, body, 201, what="create the sample's project")
        self = cls(accounts, project["projectId"], project["harnessAgentId"])
        try:
            await self._prepare(case)
        except Exception:
            await self.purge()
            raise
        return self

    async def _prepare(self, case: Any) -> None:
        main, pid = self.accounts.main, self.project_id
        if case.run.member2_role:
            if self.accounts.member2 is None:
                raise ProjectMemoryError(f"{case.id} needs member2, who is not signed in")
            status, body = await main.call(
                "POST", f"/projects/{pid}/members",
                json_body={"emails": [self.accounts.member2_email], "role": case.run.member2_role},
            )
            _ok(status, body, 200, 201, what="add member2")
            # The second member's own space, as the SPA makes it on first open.
            status, body = await self.accounts.member2.call("POST", f"/projects/{pid}/memory/mine")
            _ok(status, body, 200, 201, what="member2's personal space")
        status, body = await main.call("GET", f"/projects/{pid}/memory")
        memory = _ok(status, body, 200, what="the project's memory spaces")
        status, body = await main.call("POST", f"/projects/{pid}/memory/mine")
        mine = _ok(status, body, 200, 201, what="the eval user's personal space")
        self.spaces = {"project": memory["sharedSpaceId"], "mine": mine["spaceId"]}
        for scope, entries in (case.run.project_memory or {}).items():
            for slug, entry in entries.items():
                status, body = await main.call(
                    "PUT", f"/memory/spaces/{self.spaces[scope]}/entries/{slug}",
                    json_body={"body": entry["body"], "description": entry.get("description", "")},
                )
                _ok(status, body, 200, 201, what=f"seed {scope}/{slug}")
        self.before = {scope: await self._read(scope) for scope in self.spaces}

    async def _read(self, scope: str) -> str:
        """A scope's entries and index as one text, in slug order, so the same memory reads the same."""
        main, space = self.accounts.main, self.spaces[scope]
        status, body = await main.call("GET", f"/memory/spaces/{space}/entries")
        entries = _ok(status, body, 200, what=f"list {scope} entries").get("entries", [])
        parts = []
        for ref in sorted(entries, key=lambda e: e["slug"]):
            status, body = await main.call("GET", f"/memory/spaces/{space}/entries/{ref['slug']}")
            content = _ok(status, body, 200, what=f"read {scope}/{ref['slug']}").get("content", "")
            parts.append(f"## {ref['slug']}\n{ref.get('description', '')}\n{content}")
        status, body = await main.call("GET", f"/memory/spaces/{space}/index")
        index = (body or {}).get("content", "") if status == 200 else ""
        return "\n\n".join(parts + ([f"## MEMORY.md\n{index}"] if index else []))

    async def inspect(self) -> Dict[str, Any]:
        """What ``score_sample`` checks: each scope's text, the state before the turns, pending proposals."""
        state: Dict[str, Any] = {scope: await self._read(scope) for scope in self.spaces}
        status, body = await self.accounts.main.call(
            "GET", f"/projects/{self.project_id}/memory/proposals", params={"state": "pending"}
        )
        state["proposals"] = len(_ok(status, body, 200, what="list proposals").get("proposals", []))
        state["before"] = dict(self.before)
        return state

    async def purge(self) -> None:
        """Archive, then purge (owner only). Best-effort: a leftover is swept by the next fixture seed."""
        main, pid = self.accounts.main, self.project_id
        try:
            status, body = await main.call("PATCH", f"/projects/{pid}", json_body={"status": "archived"})
            _ok(status, body, 200, what="archive")
            status, body = await main.call("DELETE", f"/projects/{pid}")
            _ok(status, body, 204, what="purge")
        except Exception as exc:  # noqa: BLE001 — cleanup reports, never aborts
            logger.warning("could not purge the sample's project %s: %s", pid, exc)
