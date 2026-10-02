"""Smoke / regression matrix for the chat turn path — every response code path.

Drives real agent turns through the same `POST /chat/stream` the SPA uses and
asserts the response contract on the wire: SSE frame order (head → body → tail),
the interrupt/resume round trip, the single-flight 409, Stop + recovery, the
preview session, attachments, and that every turn restores from `GET /messages`
with the same content. Prompt-cache stability on the second turn is read back
from `GET /admin/costs/sessions/{id}/calls`.

The canonical matrix, what each row proves, and the in-app-browser rows this
script cannot cover live in `docs/testing/smoke-regression.md`. Keep the two
in step: a row added here gets a line there, and vice versa.

Targets (pick one):

    # Local stack — app-api :8000 with the SKIP_AUTH bypass on, proxying to
    # inference-api :8001. No credentials. SKIP_AUTH_ROLES=system_admin is
    # what makes the cache-stability row readable (admin endpoint).
    cd backend
    uv run python scripts/smoke_turns.py --base-url http://127.0.0.1:8000

    # Deployed environment through app-api, as a Cognito password user
    # (Hosted UI scripted login, same mechanism as tests/load). Needs a user
    # with a permanent password; the nightly pipeline's E2E users qualify.
    SMOKE_USERNAME=... SMOKE_PASSWORD=... uv run python scripts/smoke_turns.py \
        --base-url https://<alb-or-cloudfront>/api --auth cognito \
        --cognito-domain https://<pool-domain>.auth.<region>.amazoncognito.com

    # Deployed environment straight at the AgentCore Runtime, as the owner of
    # an active headless grant (apis/shared/harness/grants.py). Bypasses
    # app-api, so the 409 / Stop / steer rows are skipped; persistence is read
    # and sessions are deleted in-process through the shared services, with
    # the AWS profile.
    AWS_PROFILE=dev-ai uv run python scripts/smoke_turns.py --auth headless-grant \
        --user-id <cognito-sub> --prefix dev-boisestateai-v2

Every run spends real model dollars and writes real session rows for the user
it runs as. Prompts are deliberately tiny, `--model-id` defaults to the user's
default model, and sessions are deleted at the end unless `--keep`. The script
refuses a `--prefix` or `--base-url` that looks like production unless
`--allow-prod` is passed — this is a dev/local tool.

Exit status: 0 when no row FAILED (SKIP and OBSERVED do not fail a run), 1
otherwise, 2 for a setup error before any turn ran.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Tuple
from urllib.parse import urljoin

import httpx

from apis.shared.harness.sse import iter_sse_events

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("smoke")

# ============================================================
# Contract constants — mirror CLAUDE.md "SSE Event Types" + turn-path-ttft §6A
# ============================================================

# Frames that may legitimately precede `message_start` (turn-path-ttft I1 head).
# `agent_status` (preparing/prepared/thinking) and `session_title` are side
# channels that can land anywhere; the raw Strands passthroughs are harmless.
HEAD_ALLOWED = frozenset(
    {
        "quota_warning",
        "quota_session_notice",
        "agent_notice",
        "citation",
        "agent_status",
        "session_title",
        "model_retry",
        "event",
        "init_event_loop",
        "start_event_loop",
    }
)

# Frames allowed after the final `message_stop` (I1 tail). `done` must be last.
# `event` and `message` are Strands passthroughs the coordinator relays as bare
# `data:` frames (the parser names them from their single top-level key); the
# SPA ignores both.
TAIL_ALLOWED = frozenset(
    {
        "oauth_required",
        "tool_approval_required",
        "user_question_required",
        "browser_login_required",
        "metadata",
        "metadata_summary",
        "compaction",
        "session_title",
        "tool_group_summary",
        "agent_status",
        "artifact",
        "ui_resource",
        "event",
        "message",
        "result",
        "complete",
        "done",
    }
)

# Every event name the backend is known to emit. Anything else is reported as
# a WARN (a new event type that CLAUDE.md's table does not list yet), not a FAIL.
KNOWN_EVENTS = (
    HEAD_ALLOWED
    | TAIL_ALLOWED
    | frozenset(
        {
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "message_stop",
            "tool_use",
            "tool_result",
            "tool_error",
            "tool_stream_event",
            "ui_tool_input_partial",
            "steering_applied",
            "stream_error",
            "error",
            "quota_exceeded",
            "citation_start",
            "citation_end",
            "reasoning",
            "message",
        }
    )
)

INTERRUPT_EVENTS = frozenset(
    {
        "oauth_required",
        "tool_approval_required",
        "user_question_required",
        "browser_login_required",
    }
)

# Observed-only: deterministic triggering is not possible from outside, so the
# report counts them but never fails on their absence.
OBSERVED_ONLY = ("model_retry", "compaction", "tool_group_summary", "steering_applied")

PLACEHOLDER_TITLE = "New Conversation"
PREVIEW_PREFIX = "preview-"
SESSION_PREFIX = "smoke-"

PROD_MARKERS = ("prod", "production", "boisestate.ai/api")


# ============================================================
# Result types
# ============================================================


@dataclass
class Frame:
    name: str
    data: Dict[str, Any]
    t_ms: float


@dataclass
class TurnResult:
    status_code: int
    frames: List[Frame] = field(default_factory=list)
    error: Optional[str] = None
    aborted: bool = False
    elapsed_ms: float = 0.0

    @property
    def names(self) -> List[str]:
        return [f.name for f in self.frames]

    def first(self, name: str) -> Optional[Frame]:
        return next((f for f in self.frames if f.name == name), None)

    def all(self, name: str) -> List[Frame]:
        return [f for f in self.frames if f.name == name]

    @property
    def final_text(self) -> str:
        """Text of the last assistant message (same rule as the harness accumulator)."""
        messages: List[str] = []
        current: List[str] = []
        for f in self.frames:
            if f.name == "message_start":
                if "".join(current).strip():
                    messages.append("".join(current))
                current = []
            elif f.name == "content_block_delta":
                if f.data.get("type") == "text" and f.data.get("text"):
                    current.append(str(f.data["text"]))
            elif f.name == "message_stop":
                if "".join(current).strip():
                    messages.append("".join(current))
                current = []
        if "".join(current).strip():
            messages.append("".join(current))
        return messages[-1] if messages else ""

    @property
    def first_token_ms(self) -> Optional[float]:
        f = self.first("content_block_delta")
        return f.t_ms if f else None


@dataclass
class RowResult:
    row: str
    status: str  # PASS | FAIL | SKIP | OBSERVED
    detail: str = ""
    warnings: List[str] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)


class RowFailure(Exception):
    """A row's assertion failed. The message is the whole detail."""


class RowSkip(Exception):
    """A row cannot run here (fixture or transport missing). Not a failure."""


# ============================================================
# Transports
# ============================================================

OnFrame = Callable[[Frame], Awaitable[None]]


class Transport(Protocol):
    label: str
    via_app_api: bool

    async def stream_turn(
        self,
        payload: Dict[str, Any],
        *,
        on_frame: Optional[OnFrame] = None,
        abort_after_deltas: Optional[int] = None,
        timeout_s: float = 300.0,
    ) -> TurnResult: ...

    async def get_messages(self, session_id: str) -> Tuple[int, Optional[Dict[str, Any]]]: ...

    async def get_calls(self, session_id: str) -> Tuple[int, Optional[Dict[str, Any]]]: ...

    async def list_session_ids(self) -> Optional[List[str]]: ...

    async def delete_session(self, session_id: str) -> int: ...

    async def interrupt(self, session_id: str, reason: str) -> int: ...

    async def steer(self, session_id: str, text: str) -> int: ...

    async def close(self) -> None: ...


async def _drain_sse(
    response: httpx.Response,
    started: float,
    *,
    on_frame: Optional[OnFrame],
    abort_after_deltas: Optional[int],
) -> Tuple[List[Frame], bool]:
    frames: List[Frame] = []
    deltas = 0
    aborted = False
    async for name, data in iter_sse_events(response.aiter_lines()):
        frame = Frame(name=name, data=data, t_ms=(time.monotonic() - started) * 1000)
        frames.append(frame)
        if on_frame is not None:
            await on_frame(frame)
        if name == "content_block_delta":
            deltas += 1
            if abort_after_deltas is not None and deltas >= abort_after_deltas:
                aborted = True
                await response.aclose()
                break
    return frames, aborted


class AppApiTransport:
    """Talks to app-api exactly as the SPA does: cookie session + CSRF header."""

    via_app_api = True

    def __init__(self, base_url: str, label: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.label = label
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(300.0, connect=30.0),
            follow_redirects=False,
        )
        self._csrf: Optional[str] = None

    # -- auth -------------------------------------------------------------

    async def ensure_anonymous_ok(self) -> None:
        """SKIP_AUTH local stack: /auth/session must answer 200 with no cookie."""
        r = await self._client.get("/auth/session")
        if r.status_code != 200:
            raise RuntimeError(
                f"GET {self.base_url}/auth/session -> {r.status_code}. For a local run the "
                "app-api needs SKIP_AUTH=true in backend/src/.env (and SKIP_AUTH_ROLES="
                "system_admin for the cache-stability row). For a deployed target pass "
                "--auth cognito or --auth headless-grant."
            )
        body = r.json()
        self._csrf = body.get("csrf_token") or body.get("csrfToken")
        user = body.get("user") or {}
        logger.info("signed in (bypass) as %s", user.get("email") or user.get("user_id") or "?")

    async def login_cognito(self, username: str, password: str, cognito_domain: str) -> None:
        """Hosted UI scripted login — the tests/load flow, ported to httpx."""
        r = await self._client.get("/auth/login")
        if r.status_code != 302 or "/oauth2/authorize" not in r.headers.get("location", ""):
            raise RuntimeError(f"GET /auth/login -> {r.status_code}; expected a 302 to /oauth2/authorize")
        authorize_url = r.headers["location"]
        if cognito_domain and not authorize_url.startswith(cognito_domain):
            logger.warning("authorize redirect host differs from --cognito-domain; following the redirect")
        page = await self._client.get(authorize_url, follow_redirects=True)
        if page.status_code != 200:
            raise RuntimeError(f"Cognito authorize page -> {page.status_code}")
        form = _find_login_form(page.text)
        payload = dict(form.fields)
        payload[_pick_username_field(form)] = username
        payload[form.password_field] = password  # type: ignore[index]
        action = urljoin(str(page.url), form.action or "")
        post = await self._client.post(action, data=payload)
        if post.status_code == 200:
            raise RuntimeError(
                "Cognito re-rendered the login form: bad credentials, unconfirmed user, or a "
                "forced password change (FORCE_CHANGE_PASSWORD blocks scripted login)."
            )
        if post.status_code != 302 or "code=" not in post.headers.get("location", ""):
            raise RuntimeError(f"Cognito login POST -> {post.status_code} without an authorization code")
        callback = post.headers["location"]
        cb = await self._client.get(callback)
        if cb.status_code not in (302, 303):
            raise RuntimeError(f"GET /auth/callback -> {cb.status_code}; the state cookie binding failed")
        s = await self._client.get("/auth/session")
        if s.status_code != 200:
            raise RuntimeError(f"GET /auth/session -> {s.status_code} right after callback")
        body = s.json()
        self._csrf = body.get("csrf_token") or body.get("csrfToken")
        if not self._csrf:
            raise RuntimeError("GET /auth/session returned no csrf_token")
        logger.info("signed in (cognito) as %s", (body.get("user") or {}).get("email", "?"))

    def _headers(self) -> Dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._csrf:
            h["X-CSRF-Token"] = self._csrf
        return h

    # -- turn path ----------------------------------------------------------

    async def stream_turn(
        self,
        payload: Dict[str, Any],
        *,
        on_frame: Optional[OnFrame] = None,
        abort_after_deltas: Optional[int] = None,
        timeout_s: float = 300.0,
    ) -> TurnResult:
        started = time.monotonic()
        try:
            async with self._client.stream("POST", "/chat/stream", json=payload, headers=self._headers(), timeout=timeout_s) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    return TurnResult(
                        status_code=response.status_code,
                        error=body[:500],
                        elapsed_ms=(time.monotonic() - started) * 1000,
                    )
                frames, aborted = await _drain_sse(response, started, on_frame=on_frame, abort_after_deltas=abort_after_deltas)
                return TurnResult(
                    status_code=response.status_code,
                    frames=frames,
                    aborted=aborted,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
        except httpx.TimeoutException:
            return TurnResult(status_code=0, error=f"stream exceeded {timeout_s:.0f}s")
        except httpx.HTTPError as exc:
            return TurnResult(status_code=0, error=f"transport: {type(exc).__name__}: {exc}")

    async def get_messages(self, session_id: str) -> Tuple[int, Optional[Dict[str, Any]]]:
        r = await self._client.get(f"/sessions/{session_id}/messages")
        return r.status_code, (r.json() if r.status_code == 200 else None)

    async def get_calls(self, session_id: str) -> Tuple[int, Optional[Dict[str, Any]]]:
        r = await self._client.get(f"/admin/costs/sessions/{session_id}/calls")
        return r.status_code, (r.json() if r.status_code == 200 else None)

    async def list_session_ids(self) -> Optional[List[str]]:
        r = await self._client.get("/sessions", params={"limit": 200})
        if r.status_code != 200:
            return None
        body = r.json()
        items = body.get("sessions") or body.get("items") or []
        return [str(s.get("sessionId") or s.get("session_id") or "") for s in items]

    async def delete_session(self, session_id: str) -> int:
        r = await self._client.delete(f"/sessions/{session_id}", headers=self._headers())
        return r.status_code

    async def interrupt(self, session_id: str, reason: str) -> int:
        r = await self._client.post(f"/sessions/{session_id}/interrupt", json={"reason": reason}, headers=self._headers())
        return r.status_code

    async def steer(self, session_id: str, text: str) -> int:
        r = await self._client.post(
            f"/sessions/{session_id}/steer",
            json={"text": text, "entryId": f"steer-{uuid.uuid4().hex[:8]}"},
            headers=self._headers(),
        )
        return r.status_code

    async def close(self) -> None:
        await self._client.aclose()


class RuntimeTransport:
    """Straight at the AgentCore Runtime data plane with a headless-grant bearer.

    Reuses the harness's URL builder, affinity header and auth strategy so the
    request is byte-for-byte what a scheduled run sends. Persistence is read
    in-process through the shared repositories (env resolved from SSM), which
    is also why this mode needs an AWS profile for the target account.
    """

    via_app_api = False

    def __init__(self, user_id: str, prefix: str, region: str) -> None:
        from apis.shared.harness.auth import CognitoRefreshBearerAuth
        from apis.shared.harness.runner import apply_runtime_session_header, build_invocations_url

        self.label = f"runtime:{prefix}"
        self.user_id = user_id
        self._apply_header = apply_runtime_session_header
        self._url = build_invocations_url(os.environ["INFERENCE_API_URL"])
        self._auth = CognitoRefreshBearerAuth()
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=30.0))
        self._bearer: Optional[str] = None
        self._bearer_at = 0.0

    async def _bearer_token(self) -> str:
        # Cognito access tokens live an hour; re-mint well inside that.
        if self._bearer is None or time.monotonic() - self._bearer_at > 20 * 60:
            self._bearer = await self._auth.mint_bearer_for_user(self.user_id)
            self._bearer_at = time.monotonic()
        return self._bearer

    async def stream_turn(
        self,
        payload: Dict[str, Any],
        *,
        on_frame: Optional[OnFrame] = None,
        abort_after_deltas: Optional[int] = None,
        timeout_s: float = 300.0,
    ) -> TurnResult:
        bearer = await self._bearer_token()
        headers = self._apply_header(
            {"Content-Type": "application/json", "Authorization": f"Bearer {bearer}"},
            payload.get("session_id"),
        )
        started = time.monotonic()
        try:
            async with self._client.stream("POST", self._url, json=payload, headers=headers, timeout=timeout_s) as response:
                if response.status_code >= 400:
                    body = (await response.aread()).decode("utf-8", errors="replace")
                    return TurnResult(status_code=response.status_code, error=body[:500])
                frames, aborted = await _drain_sse(response, started, on_frame=on_frame, abort_after_deltas=abort_after_deltas)
                return TurnResult(
                    status_code=response.status_code,
                    frames=frames,
                    aborted=aborted,
                    elapsed_ms=(time.monotonic() - started) * 1000,
                )
        except httpx.TimeoutException:
            return TurnResult(status_code=0, error=f"stream exceeded {timeout_s:.0f}s")
        except httpx.HTTPError as exc:
            return TurnResult(status_code=0, error=f"transport: {type(exc).__name__}: {exc}")

    async def get_messages(self, session_id: str) -> Tuple[int, Optional[Dict[str, Any]]]:
        try:
            from apis.shared.sessions.messages import get_messages

            resp = await get_messages(session_id, self.user_id)
            return 200, resp.model_dump(by_alias=True)
        except Exception as exc:  # noqa: BLE001 — reported, never raised past the row
            logger.warning("in-process get_messages failed: %s", exc)
            return 0, None

    async def get_calls(self, session_id: str) -> Tuple[int, Optional[Dict[str, Any]]]:
        try:
            from apis.app_api.admin.costs.service import AdminCostService

            anatomy = await AdminCostService().get_session_cost_anatomy(session_id)
            return 200, anatomy.model_dump(by_alias=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("in-process get_session_cost_anatomy failed: %s", exc)
            return 0, None

    async def list_session_ids(self) -> Optional[List[str]]:
        return None

    async def delete_session(self, session_id: str) -> int:
        # The same soft delete app-api's DELETE /sessions/{id} performs (S#ACTIVE#
        # -> S#DELETED#, cost rows kept), run in-process with the AWS profile.
        try:
            from apis.app_api.sessions.services.session_service import SessionService

            ok = await SessionService().delete_session(user_id=self.user_id, session_id=session_id)
            return 204 if ok else 404
        except Exception as exc:  # noqa: BLE001
            logger.warning("in-process delete_session(%s) failed: %s", session_id, exc)
            return 0

    async def interrupt(self, session_id: str, reason: str) -> int:
        return 0

    async def steer(self, session_id: str, text: str) -> int:
        return 0

    async def close(self) -> None:
        await self._client.aclose()


# -- Hosted UI form parsing (ported from tests/load/agentcore_load/auth.py) ---


class _HtmlForm:
    def __init__(self, action: Optional[str], method: str) -> None:
        self.action = action
        self.method = method.lower()
        self.fields: Dict[str, str] = {}
        self.password_field: Optional[str] = None
        self.text_fields: List[str] = []


class _FormParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.forms: List[_HtmlForm] = []
        self._current: Optional[_HtmlForm] = None

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        attr = {k.lower(): (v or "") for k, v in attrs}
        if tag == "form":
            self._current = _HtmlForm(attr.get("action"), attr.get("method", "get"))
            self.forms.append(self._current)
            return
        if tag != "input" or self._current is None or not attr.get("name"):
            return
        name = attr["name"]
        kind = attr.get("type", "text").lower()
        if kind == "password":
            self._current.password_field = name
            self._current.fields[name] = ""
        elif kind in {"hidden", "text", "email", "tel"}:
            self._current.fields[name] = attr.get("value", "")
            if kind != "hidden":
                self._current.text_fields.append(name)

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current = None


def _find_login_form(html: str) -> _HtmlForm:
    p = _FormParser()
    p.feed(html)
    forms = [f for f in p.forms if f.password_field]
    if not forms:
        raise RuntimeError(
            "No password form on the Cognito page. Managed login (branding v2) renders "
            "client-side and cannot be scripted — use --auth headless-grant instead."
        )
    return forms[0]


def _pick_username_field(form: _HtmlForm) -> str:
    for c in ("username", "email", "signInFormUsername"):
        if c in form.fields:
            return c
    if len(form.text_fields) == 1:
        return form.text_fields[0]
    raise RuntimeError(f"Could not pick the username field from {form.text_fields!r}")


# ============================================================
# Contract assertions
# ============================================================


RESUME_HEAD_EXTRA = frozenset({"tool_use", "tool_result", "tool_error", "message", "metadata", "tool_group_summary", "ui_resource"})


def assert_frame_contract(turn: TurnResult, *, allow_interrupt: bool = False, resume: bool = False) -> List[str]:
    """Invariant I1 (turn-path-ttft §6A) on one turn's frames. Returns warnings.

    ``resume=True`` widens the head: a resumed turn finishes the paused tool
    first, so its ``tool_result`` (and the passthroughs around it) arrive before
    the next model call's ``message_start``.
    """
    warnings: List[str] = []
    names = turn.names
    head_allowed = HEAD_ALLOWED | RESUME_HEAD_EXTRA if resume else HEAD_ALLOWED
    if turn.status_code != 200:
        raise RowFailure(f"HTTP {turn.status_code}: {turn.error}")
    if not names:
        raise RowFailure("stream carried no frames")
    if "message_start" not in names:
        raise RowFailure(f"no message_start; frames={names[:12]}")

    first_ms = names.index("message_start")
    head_bad = [n for n in names[:first_ms] if n not in head_allowed]
    if head_bad:
        raise RowFailure(f"frames before message_start that the head contract forbids: {head_bad}; head={names[:first_ms]}")

    first_delta = next((i for i, n in enumerate(names) if n == "content_block_delta"), None)
    if first_delta is not None and first_delta < first_ms:
        raise RowFailure("content_block_delta arrived before message_start")

    if names[-1] != "done":
        raise RowFailure(f"last frame is {names[-1]!r}, expected done")
    if names.count("done") != 1:
        raise RowFailure(f"done emitted {names.count('done')} times")

    if "message_stop" not in names:
        raise RowFailure("no message_stop before done")
    last_stop = len(names) - 1 - names[::-1].index("message_stop")
    tail_bad = [n for n in names[last_stop + 1 :] if n not in TAIL_ALLOWED]
    if tail_bad:
        raise RowFailure(f"frames after the final message_stop that the tail contract forbids: {tail_bad}; tail={names[last_stop:]}")

    if not any(n in ("metadata", "metadata_summary") for n in names[last_stop:]):
        warnings.append("no metadata/metadata_summary between the final message_stop and done")

    titles = turn.all("session_title")
    if len(titles) > 1:
        raise RowFailure(f"session_title emitted {len(titles)} times (max 1)")
    if titles and (titles[0].data.get("title") or "").strip() == PLACEHOLDER_TITLE:
        raise RowFailure("session_title carried the 'New Conversation' placeholder")

    errors = [f for f in turn.frames if f.name in ("stream_error", "error")]
    if errors:
        raise RowFailure(f"stream carried an error frame: {errors[0].data}")

    interrupts = [n for n in names if n in INTERRUPT_EVENTS]
    if interrupts and not allow_interrupt:
        raise RowFailure(f"unexpected interrupt event(s) {interrupts}")

    unknown = sorted({n for n in names if n not in KNOWN_EVENTS and not n.startswith("_")})
    if unknown:
        warnings.append(f"event names not in the known set (update CLAUDE.md's table?): {unknown}")
    unparseable = names.count("_unparseable")
    if unparseable:
        raise RowFailure(f"{unparseable} frame(s) carried unparseable JSON")
    return warnings


def _tools_used(turn: TurnResult) -> List[str]:
    return sorted({(f.data.get("tool_use") or f.data).get("name", "") for f in turn.all("tool_use")} - {""})


def _message_texts(messages_body: Dict[str, Any], role: str) -> List[str]:
    out: List[str] = []
    for m in messages_body.get("messages") or []:
        if m.get("role") != role:
            continue
        parts = [c.get("text") or "" for c in (m.get("content") or []) if isinstance(c, dict)]
        out.append("".join(parts))
    return out


def _restore_check(messages_body: Dict[str, Any], user_prompt: str, assistant_text: str) -> None:
    users = _message_texts(messages_body, "user")
    assistants = _message_texts(messages_body, "assistant")
    if not any(user_prompt in u for u in users):
        raise RowFailure(f"restored history lacks the user prompt {user_prompt[:40]!r}; users={[u[:40] for u in users]}")
    want = assistant_text.strip()
    if want and not any(want[:60] in a for a in assistants):
        raise RowFailure(f"restored assistant text differs from the streamed text; streamed={want[:60]!r} restored={[a[:60] for a in assistants]}")


# ============================================================
# Fixtures generated in-script
# ============================================================

PDF_TOKEN = "SMOKE-PDF-TOKEN-7731"


def minimal_pdf(text: str) -> bytes:
    """A valid single-page PDF whose only content is ``text`` (Helvetica 24pt)."""
    stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET".encode("latin-1")
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


CSV_ROWS = 7
CSV_TEXT = "id,city,temp_c\n" + "\n".join(f"{i},City{i},{10 + i}" for i in range(1, CSV_ROWS + 1)) + "\n"


# ============================================================
# The matrix
# ============================================================


@dataclass
class Context:
    transport: Transport
    args: argparse.Namespace
    created_sessions: List[str] = field(default_factory=list)
    observed: Dict[str, int] = field(default_factory=dict)
    first_token_ms: List[float] = field(default_factory=list)
    shared_session: str = ""
    shared_turns: List[Tuple[str, str]] = field(default_factory=list)  # (prompt, final_text)

    def new_session(self, prefix: str = SESSION_PREFIX) -> str:
        sid = f"{prefix}{uuid.uuid4().hex[:16]}"
        if prefix == SESSION_PREFIX:
            self.created_sessions.append(sid)
        return sid

    def base_payload(self, session_id: str, message: str, **extra: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"session_id": session_id, "message": message, "enabled_tools": []}
        if self.args.model_id:
            payload["model_id"] = self.args.model_id
        payload.update(extra)
        return payload

    def note(self, turn: TurnResult) -> None:
        for f in turn.frames:
            if f.name in OBSERVED_ONLY:
                self.observed[f.name] = self.observed.get(f.name, 0) + 1
            elif f.name == "agent_status":
                key = f"agent_status:{f.data.get('phase', '?')}"
                self.observed[key] = self.observed.get(key, 0) + 1
        if turn.first_token_ms is not None:
            self.first_token_ms.append(turn.first_token_ms)


async def row_plain_first_turn(ctx: Context) -> RowResult:
    sid = ctx.new_session()
    ctx.shared_session = sid
    prompt = "Reply with exactly the single word PONG and nothing else."
    turn = await ctx.transport.stream_turn(ctx.base_payload(sid, prompt))
    ctx.note(turn)
    warnings = assert_frame_contract(turn)
    text = turn.final_text
    if "PONG" not in text.upper():
        warnings.append(f"model did not answer PONG (got {text[:40]!r}); the contract checks still passed")
    ctx.shared_turns.append((prompt, text))
    title = turn.first("session_title")
    data = {
        "frames": len(turn.frames),
        "firstTokenMs": round(turn.first_token_ms or 0),
        "sessionTitle": (title.data.get("title") if title else None),
        "agentStatusPhases": sorted({f.data.get("phase", "") for f in turn.all("agent_status")}),
    }
    return RowResult("plain_first_turn", "PASS", f"{len(turn.frames)} frames, first token {data['firstTokenMs']}ms", warnings, data)


async def row_second_turn_cache(ctx: Context) -> RowResult:
    if not ctx.shared_session:
        raise RowSkip("first turn did not run")
    sid = ctx.shared_session
    prompt = "Reply with exactly the single word PING and nothing else."
    turn = await ctx.transport.stream_turn(ctx.base_payload(sid, prompt))
    ctx.note(turn)
    warnings = assert_frame_contract(turn)
    ctx.shared_turns.append((prompt, turn.final_text))
    prepared = next((f for f in turn.all("agent_status") if f.data.get("phase") == "prepared"), None)
    build_ms = int(prepared.data.get("durationMs") or 0) if prepared else 0
    if build_ms > 200:
        warnings.append(f"second turn rebuilt the agent in {build_ms}ms (cache miss on a warm session?)")

    # Cache stability (I2/I3): give the write path a moment to land the C# rows.
    calls_body: Optional[Dict[str, Any]] = None
    code = 0
    for _ in range(6):
        code, calls_body = await ctx.transport.get_calls(sid)
        if calls_body and len(calls_body.get("calls") or []) >= 2:
            break
        await asyncio.sleep(1.0)
    if code in (401, 403):
        return RowResult(
            "second_turn_cache",
            "PASS",
            "frame contract ok; cache rows unreadable (admin endpoint -> %d, run as system_admin to read them)" % code,
            warnings,
        )
    if not calls_body:
        return RowResult("second_turn_cache", "PASS", f"frame contract ok; cost anatomy unavailable (HTTP {code})", warnings)
    calls = calls_body.get("calls") or []
    if len(calls) < 2:
        raise RowFailure(f"expected >=2 call rows for two turns, found {len(calls)}")
    rows = sorted(calls, key=lambda c: c.get("timestamp") or "")
    prev, last = rows[-2], rows[-1]
    status = last.get("cacheStatus")
    if status == "partial_miss":
        raise RowFailure("second turn is a partial_miss: the history behind the tools+system prefix was re-written")
    fp_prev = prev.get("prefixFingerprints") or {}
    fp_last = last.get("prefixFingerprints") or {}
    for key in ("toolConfigHash", "systemPromptHash"):
        if fp_prev.get(key) and fp_last.get(key) and fp_prev[key] != fp_last[key]:
            raise RowFailure(f"{key} changed between consecutive turns of one session ({fp_prev[key][:10]} -> {fp_last[key][:10]})")
    if status not in ("hit", "uncached", None):
        warnings.append(f"second-turn cacheStatus={status!r} (short prompts sit under Bedrock's minimum cacheable prefix, so 'uncached' is normal)")
    data = {
        "cacheStatus": status,
        "cacheRead": last.get("cacheReadTokens"),
        "cacheWrite": last.get("cacheWriteTokens"),
        "calls": len(calls),
        "buildMs": build_ms,
    }
    return RowResult("second_turn_cache", "PASS", f"cacheStatus={status}, hashes stable across {len(calls)} calls", warnings, data)


async def row_restore(ctx: Context) -> RowResult:
    if not ctx.shared_turns:
        raise RowSkip("no turns to restore")
    code, body = await ctx.transport.get_messages(ctx.shared_session)
    if code != 200 or body is None:
        raise RowFailure(f"GET /messages -> {code}")
    for prompt, text in ctx.shared_turns:
        _restore_check(body, prompt, text)
    n = len(body.get("messages") or [])
    if n < 2 * len(ctx.shared_turns):
        raise RowFailure(f"restored {n} messages for {len(ctx.shared_turns)} turns")
    return RowResult("restore_after_reload", "PASS", f"{n} messages restored with matching text", data={"messages": n})


async def row_tool_turn(ctx: Context) -> RowResult:
    tool = ctx.args.tool
    sid = ctx.new_session()
    prompt = f"Use the {tool} tool to compute 1234*5678. Reply with only the number the tool returned."
    turn = await ctx.transport.stream_turn(ctx.base_payload(sid, prompt, enabled_tools=[tool]))
    ctx.note(turn)
    warnings = assert_frame_contract(turn)
    uses = turn.all("tool_use")
    if not uses:
        raise RowFailure(f"no tool_use frame — is {tool!r} granted to this user (RBAC) and enabled? final text: {turn.final_text[:80]!r}")
    if not turn.all("tool_result"):
        raise RowFailure("tool_use without a tool_result frame")
    phases = [f.data.get("phase") for f in turn.all("agent_status")]
    if turn.all("agent_status") and not {"tool_start", "tool_end"} <= set(phases):
        warnings.append(f"agent_status present but tool_start/tool_end missing: {sorted(set(phases))}")
    if "7006652" not in turn.final_text.replace(",", ""):
        warnings.append(f"answer did not contain 7006652: {turn.final_text[:60]!r}")
    names = sorted({(f.data.get("tool_use") or f.data).get("name", "") for f in uses} - {""})
    return RowResult("tool_turn", "PASS", f"tools used: {names}; {len(turn.all('tool_result'))} result(s)", warnings, {"tools": names})


ASK_PROMPT = (
    "Do not answer this message directly. Your first and only action is to call the ask_user_question "
    "tool once, with a single question: header 'Color', question 'Which color do you prefer?', options "
    "Red and Blue, multiSelect false. Only after my answer arrives, reply with exactly 'You picked "
    "<color>.' and nothing else."
)


async def row_ask_user_question(ctx: Context) -> RowResult:
    # Whether the model calls the tool is its decision, so a single attempt is a
    # coin toss on a weaker model. Three fresh sessions before calling it a failure.
    warnings: List[str] = []
    turn: Optional[TurnResult] = None
    q: Optional[Frame] = None
    sid = ""
    for attempt in range(1, 4):
        sid = ctx.new_session()
        turn = await ctx.transport.stream_turn(ctx.base_payload(sid, ASK_PROMPT, enabled_tools=["ask_user_question"]))
        ctx.note(turn)
        warnings += assert_frame_contract(turn, allow_interrupt=True)
        q = turn.first("user_question_required")
        if q is not None:
            if attempt > 1:
                warnings.append(f"model only called ask_user_question on attempt {attempt}")
            break
    assert turn is not None
    if q is None:
        raise RowFailure(
            "no user_question_required frame in 3 attempts — is ASK_USER_QUESTION_ENABLED on and the tool "
            f"granted, or does this model ignore the directive? last answer: {turn.final_text[:60]!r}"
        )
    names = turn.names
    if names.index("user_question_required") < len(names) - 1 - names[::-1].index("message_stop"):
        raise RowFailure("user_question_required arrived before the final message_stop")
    interrupt_id = q.data.get("interruptId")
    questions = q.data.get("questions") or []
    if not interrupt_id or not questions:
        raise RowFailure(f"user_question_required lacks interruptId/questions: {q.data}")
    header = questions[0].get("header") or "Color"
    options = [o.get("label") for o in (questions[0].get("options") or [])]
    pick = next((o for o in options if o and o.lower().startswith("red")), options[0] if options else "Red")

    resume = await ctx.transport.stream_turn(
        {
            "session_id": sid,
            "message": "",
            "enabled_tools": ["ask_user_question"],
            **({"model_id": ctx.args.model_id} if ctx.args.model_id else {}),
            "interrupt_responses": [{"interruptId": interrupt_id, "response": {"answers": {header: {"selected": [pick], "text": None}}}}],
        }
    )
    ctx.note(resume)
    warnings += assert_frame_contract(resume, resume=True)
    if resume.all("user_question_required"):
        raise RowFailure("the resumed turn re-raised user_question_required (a null/invalid answer re-asks forever)")
    if pick.lower() not in resume.final_text.lower():
        warnings.append(f"resumed answer did not echo {pick!r}: {resume.final_text[:60]!r}")
    code, body = await ctx.transport.get_messages(sid)
    if code == 200 and body is not None and body.get("pendingInterrupts"):
        raise RowFailure("pendingInterrupts still non-empty after a successful resume")
    return RowResult(
        "ask_user_question_resume",
        "PASS",
        f"paused with {len(questions)} question(s), resumed with {pick!r}, answer: {resume.final_text[:40]!r}",
        warnings,
        {"options": options},
    )


async def row_tool_approval(ctx: Context) -> RowResult:
    tool = ctx.args.approval_tool
    if not tool:
        raise RowSkip("pass --approval-tool <tool_id> (e.g. an MCP tool flagged needsApproval) to run")
    sid = ctx.new_session()
    prompt = ctx.args.approval_prompt or f"Call the {tool} tool now and tell me what it returned."
    turn = await ctx.transport.stream_turn(ctx.base_payload(sid, prompt, enabled_tools=[tool]))
    ctx.note(turn)
    warnings = assert_frame_contract(turn, allow_interrupt=True)
    ev = turn.first("tool_approval_required")
    if ev is None:
        raise RowFailure(f"no tool_approval_required frame; frames={sorted(set(turn.names))}")
    interrupt_id = ev.data.get("interruptId")
    resume = await ctx.transport.stream_turn(
        {
            "session_id": sid,
            "message": "",
            "enabled_tools": [tool],
            **({"model_id": ctx.args.model_id} if ctx.args.model_id else {}),
            "interrupt_responses": [{"interruptId": interrupt_id, "response": "approved"}],
        }
    )
    ctx.note(resume)
    warnings += assert_frame_contract(resume, resume=True)
    if not resume.all("tool_result"):
        raise RowFailure("approved resume produced no tool_result")
    return RowResult("tool_approval_resume", "PASS", f"approved {ev.data.get('toolName')} and the turn finished", warnings)


async def row_duplicate_and_stop(ctx: Context) -> List[RowResult]:
    if not ctx.transport.via_app_api:
        raise RowSkip("single-flight 409 and Stop live on app-api; not reachable in runtime-direct mode")
    sid = ctx.new_session()
    long_prompt = "Count from 1 to 150, one number per line, with no other text."
    started = asyncio.Event()

    async def on_frame(frame: Frame) -> None:
        if frame.name == "message_start":
            started.set()

    task = asyncio.create_task(ctx.transport.stream_turn(ctx.base_payload(sid, long_prompt), on_frame=on_frame, abort_after_deltas=12))
    results: List[RowResult] = []
    try:
        await asyncio.wait_for(started.wait(), timeout=120)
    except asyncio.TimeoutError:
        task.cancel()
        raise RowFailure("first stream never reached message_start")

    dup = await ctx.transport.stream_turn(ctx.base_payload(sid, "Say hi."), timeout_s=60)
    if dup.status_code != 409:
        results.append(
            RowResult("duplicate_send_409", "FAIL", f"second send while streaming -> HTTP {dup.status_code} (expected 409); {dup.error or ''}"[:200])
        )
    else:
        results.append(RowResult("duplicate_send_409", "PASS", f"409 while streaming: {(dup.error or '')[:80]}"))

    first = await task
    ctx.note(first)
    if not first.aborted:
        results.append(RowResult("stop_mid_answer", "FAIL", "the long turn finished before the client could abort it; lengthen the prompt"))
        return results
    code = await ctx.transport.interrupt(sid, "user_stopped")
    if code not in (204, 200):
        results.append(RowResult("stop_mid_answer", "FAIL", f"POST /interrupt user_stopped -> {code}"))
        return results

    # The lease is released when the server notices the drop (heartbeat ~10s).
    deadline = time.monotonic() + 45
    attempts = 0
    nxt: Optional[TurnResult] = None
    while time.monotonic() < deadline:
        attempts += 1
        nxt = await ctx.transport.stream_turn(ctx.base_payload(sid, "Reply with exactly the single word RESUMED."))
        if nxt.status_code != 409:
            break
        await asyncio.sleep(2.0)
    assert nxt is not None
    if nxt.status_code == 409:
        results.append(RowResult("stop_mid_answer", "FAIL", f"still 409 {attempts} tries / 45s after Stop — the lease leaked"))
        return results
    ctx.note(nxt)
    warnings = assert_frame_contract(nxt)
    code, body = await ctx.transport.get_messages(sid)
    detail = f"resend accepted after {attempts} try(ies)"
    if code == 200 and body is not None:
        assistants = _message_texts(body, "assistant")
        partial = any(("1" in a and "150" not in a) for a in assistants[:-1]) if len(assistants) > 1 else False
        detail += (
            "; partial answer persisted" if partial else "; partial answer not visible (a short turn can finish server-side and clear the marker)"
        )
    results.append(RowResult("stop_mid_answer", "PASS", detail, warnings, {"resendAttempts": attempts}))
    return results


async def row_preview_session(ctx: Context) -> RowResult:
    sid = ctx.new_session(PREVIEW_PREFIX)
    turn = await ctx.transport.stream_turn(ctx.base_payload(sid, "Reply with exactly the single word PREVIEW."))
    ctx.note(turn)
    warnings = assert_frame_contract(turn)
    code, body = await ctx.transport.get_messages(sid)
    if code == 200 and body and (body.get("messages") or []):
        raise RowFailure(f"preview session persisted {len(body['messages'])} messages")
    listed = await ctx.transport.list_session_ids()
    if listed is not None and sid in listed:
        raise RowFailure("preview session appeared in the sidebar list")
    return RowResult("preview_session", "PASS", f"turn completed; GET /messages -> {code}; not listed", warnings)


async def row_steer_observed(ctx: Context) -> RowResult:
    if not ctx.transport.via_app_api:
        raise RowSkip("steer endpoint lives on app-api")
    tool = ctx.args.tool
    sid = ctx.new_session()
    prompt = f"Use the {tool} tool three separate times, one call per step, to compute 11*11, then 12*12, then 13*13. Then list the three results."
    steer_code: Dict[str, int] = {}
    fired = asyncio.Event()

    async def on_frame(frame: Frame) -> None:
        if frame.name == "tool_use" and not fired.is_set():
            fired.set()
            steer_code["code"] = await ctx.transport.steer(sid, "Also compute 14*14 and include it.")

    turn = await ctx.transport.stream_turn(ctx.base_payload(sid, prompt, enabled_tools=[tool]), on_frame=on_frame)
    ctx.note(turn)
    warnings = assert_frame_contract(turn)
    code = steer_code.get("code")
    if code == 404:
        raise RowSkip("POST /steer -> 404 (MID_TURN_STEERING_ENABLED is off)")
    if code is None:
        raise RowSkip("the model made no tool call, so there was no boundary to steer at")
    if code not in (200, 201, 202):
        raise RowFailure(f"POST /steer -> {code}")
    applied = bool(turn.all("steering_applied"))
    detail = (
        "steering_applied landed at a tool boundary"
        if applied
        else "steer queued but lost the race with the turn's end (the SPA sends it as the next turn)"
    )
    return RowResult("steer_mid_turn", "OBSERVED", detail, warnings, {"applied": applied, "steerHttp": code})


async def row_attach_pdf(ctx: Context) -> RowResult:
    if not ctx.args.with_attachments:
        raise RowSkip("pass --with-attachments")
    sid = ctx.new_session()
    prompt = "What token is written in the attached PDF? Reply with just the token."
    files = [{"filename": "smoke.pdf", "content_type": "application/pdf", "bytes": base64.b64encode(minimal_pdf(PDF_TOKEN)).decode()}]
    turn = await ctx.transport.stream_turn(ctx.base_payload(sid, prompt, files=files, enabled_tools=None))
    ctx.note(turn)
    warnings = assert_frame_contract(turn)
    if PDF_TOKEN not in turn.final_text.upper():
        warnings.append(f"model did not read back the token: {turn.final_text[:60]!r}")
    code, body = await ctx.transport.get_messages(sid)
    if code != 200 or body is None:
        raise RowFailure(f"GET /messages -> {code}")
    users = body.get("messages") or []
    has_doc = any(
        c.get("type") == "document" or c.get("document") for m in users if m.get("role") == "user" for c in (m.get("content") or [])
    ) or any("smoke.pdf" in u for u in _message_texts(body, "user"))
    if not has_doc:
        raise RowFailure("restored user message carries neither a document block nor the attachment marker")
    return RowResult("attach_pdf", "PASS", f"inline PDF accepted, answer streamed, attachment restored; tools={_tools_used(turn)}", warnings)


async def row_attach_csv(ctx: Context) -> RowResult:
    if not ctx.args.with_attachments:
        raise RowSkip("pass --with-attachments")
    sid = ctx.new_session()
    prompt = (
        "Use your spreadsheet tools on the attached file smoke.csv (ignore anything you remember about "
        "other files). How many data rows does it have, excluding the header? Reply with just the number."
    )
    files = [{"filename": "smoke.csv", "content_type": "text/csv", "bytes": base64.b64encode(CSV_TEXT.encode()).decode()}]
    turn = await ctx.transport.stream_turn(ctx.base_payload(sid, prompt, files=files, enabled_tools=None))
    ctx.note(turn)
    warnings = assert_frame_contract(turn)
    if str(CSV_ROWS) not in turn.final_text:
        warnings.append(f"answer did not say {CSV_ROWS}: {turn.final_text[:60]!r}")
    code, body = await ctx.transport.get_messages(sid)
    if code != 200 or body is None:
        raise RowFailure(f"GET /messages -> {code}")
    marker = any("[Attached files:" in u or "smoke.csv" in u for u in _message_texts(body, "user"))
    if not marker:
        raise RowFailure("restored user message lacks the [Attached files: …] marker for the diverted CSV")
    return RowResult("attach_csv", "PASS", f"CSV diverted, marker persisted; tools={_tools_used(turn)}", warnings)


async def row_skill_invoke(ctx: Context) -> RowResult:
    skill = ctx.args.skill_id
    if not skill:
        raise RowSkip("pass --skill-id <id> for a skill granted to this user")
    sid = ctx.new_session()
    prompt = ctx.args.skill_prompt or "Follow the invoked skill and reply briefly."
    turn = await ctx.transport.stream_turn(ctx.base_payload(sid, prompt, enabled_skills=[skill], invoked_skills=[skill], enabled_tools=None))
    ctx.note(turn)
    warnings = assert_frame_contract(turn)
    names = {(f.data.get("tool_use") or f.data).get("name", "") for f in turn.all("tool_use")}
    if "skills" not in names:
        raise RowFailure(f"the skills tool was not called for an invoked skill; tools called: {sorted(names)}")
    return RowResult("skill_invoke", "PASS", "skills tool activated the invoked skill", warnings)


async def row_kb_agent(ctx: Context) -> RowResult:
    agent = ctx.args.agent_id
    if not agent:
        raise RowSkip("pass --agent-id <assistant id> for an agent with a knowledge base")
    sid = ctx.new_session()
    prompt = ctx.args.agent_prompt or "Using your knowledge base, summarize one document in two sentences."
    turn = await ctx.transport.stream_turn(ctx.base_payload(sid, prompt, rag_assistant_id=agent, enabled_tools=None))
    ctx.note(turn)
    warnings = assert_frame_contract(turn)
    cites = turn.all("citation")
    if cites:
        names = turn.names
        if names.index("citation") > names.index("message_start"):
            raise RowFailure("citation frames arrived after message_start")
    else:
        warnings.append("no citation frames (empty KB, show_citations off, or no retrieval hit)")
    return RowResult("kb_agent_first_turn", "PASS", f"{len(cites)} citation frame(s) before message_start", warnings)


async def row_quota_exceeded(ctx: Context) -> RowResult:
    if not ctx.args.expect_quota_exceeded:
        raise RowSkip("pass --expect-quota-exceeded when running as a user on an exhausted test tier")
    sid = ctx.new_session()
    turn = await ctx.transport.stream_turn(ctx.base_payload(sid, "Reply with exactly the single word PONG."))
    ctx.note(turn)
    warnings = assert_frame_contract(turn)
    stop = turn.all("message_stop")
    reason = stop[-1].data.get("stopReason") if stop else None
    if reason != "quota_exceeded" and not turn.all("quota_exceeded"):
        raise RowFailure(f"expected a quota_exceeded refusal, got stopReason={reason!r}")
    code, calls = await ctx.transport.get_calls(sid)
    if calls and (calls.get("calls") or []):
        raise RowFailure("a refused turn still produced a model call row")
    return RowResult("quota_exceeded", "PASS", "conversational refusal, no model call", warnings)


ROWS: List[Tuple[str, Callable[[Context], Awaitable[Any]]]] = [
    ("plain_first_turn", row_plain_first_turn),
    ("second_turn_cache", row_second_turn_cache),
    ("restore_after_reload", row_restore),
    ("tool_turn", row_tool_turn),
    ("ask_user_question_resume", row_ask_user_question),
    ("tool_approval_resume", row_tool_approval),
    ("duplicate_send_409+stop_mid_answer", row_duplicate_and_stop),
    ("preview_session", row_preview_session),
    ("steer_mid_turn", row_steer_observed),
    ("attach_pdf", row_attach_pdf),
    ("attach_csv", row_attach_csv),
    ("skill_invoke", row_skill_invoke),
    ("kb_agent_first_turn", row_kb_agent),
    ("quota_exceeded", row_quota_exceeded),
]


# ============================================================
# Runner
# ============================================================


def _looks_like_prod(*values: Optional[str]) -> bool:
    joined = " ".join(v for v in values if v).lower()
    return any(marker in joined for marker in PROD_MARKERS)


def resolve_runtime_env(prefix: str, region: str) -> None:
    """Export the env the harness + shared repositories expect, from SSM + conventions.

    One definition of how a deployed prefix maps onto env vars, extending the
    spike driver's resolver with the tables the in-process readers need.
    """
    import boto3

    ssm = boto3.client("ssm", region_name=region)
    sts = boto3.client("sts", region_name=region)
    account = sts.get_caller_identity()["Account"]

    def param(name: str, default: Optional[str] = None) -> str:
        try:
            return ssm.get_parameter(Name=f"/{prefix}/{name}")["Parameter"]["Value"]
        except Exception:  # noqa: BLE001
            if default is None:
                raise
            return default

    runtime_id = param("inference-api/runtime-id")
    runtime_arn = f"arn:aws:bedrock-agentcore:{region}:{account}:runtime/{runtime_id}"
    env = {
        "AWS_REGION": region,
        "INFERENCE_API_URL": f"https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{runtime_arn}",
        "BFF_SESSIONS_TABLE_NAME": f"{prefix}-bff-sessions",
        "COGNITO_BFF_APP_CLIENT_ID": param("auth/cognito/bff-app-client-id"),
        "COGNITO_BFF_APP_CLIENT_SECRET_ARN": f"{prefix}-cognito-bff-app-client-secret",
        "DYNAMODB_SESSIONS_METADATA_TABLE_NAME": param("cost-tracking/sessions-metadata-table-name", f"{prefix}-sessions-metadata"),
        "DYNAMODB_COST_SUMMARY_TABLE_NAME": param("cost-tracking/user-cost-summary-table-name", f"{prefix}-user-cost-summary"),
        "DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME": param("cost-tracking/system-cost-rollup-table-name", f"{prefix}-system-cost-rollup"),
        "DYNAMODB_ASSISTANTS_TABLE_NAME": param("rag/assistants-table-name", f"{prefix}-assistants"),
        "DYNAMODB_USER_SETTINGS_TABLE_NAME": param("settings/user-settings-table-name", f"{prefix}-user-settings"),
        "AGENTCORE_MEMORY_ID": param("inference-api/memory-id", ""),
    }
    for k, v in env.items():
        os.environ.setdefault(k, v)


async def build_transport(args: argparse.Namespace) -> Transport:
    if args.auth == "headless-grant":
        if not args.user_id or not args.prefix:
            raise SystemExit("--auth headless-grant needs --user-id and --prefix")
        resolve_runtime_env(args.prefix, args.region)
        return RuntimeTransport(args.user_id, args.prefix, args.region)
    if not args.base_url:
        raise SystemExit("--base-url is required unless --auth headless-grant")
    t = AppApiTransport(args.base_url, label=args.base_url)
    if args.auth == "cognito":
        user = os.environ.get("SMOKE_USERNAME")
        pw = os.environ.get("SMOKE_PASSWORD")
        if not user or not pw:
            raise SystemExit("--auth cognito reads SMOKE_USERNAME / SMOKE_PASSWORD from the environment")
        await t.login_cognito(user, pw, args.cognito_domain or "")
    else:
        await t.ensure_anonymous_ok()
    return t


def _print_report(results: List[RowResult], ctx: Context, label: str) -> None:
    width = max(len(r.row) for r in results) + 2
    print()
    print(f"smoke_turns against {label}")
    print("=" * (width + 70))
    for r in results:
        print(f"{r.row.ljust(width)} {r.status.ljust(8)} {r.detail}")
        for w in r.warnings:
            print(f"{''.ljust(width)} WARN     {w}")
    print("-" * (width + 70))
    counts = {s: sum(1 for r in results if r.status == s) for s in ("PASS", "FAIL", "SKIP", "OBSERVED")}
    print("  ".join(f"{k} {v}" for k, v in counts.items()))
    if ctx.first_token_ms:
        ms = sorted(ctx.first_token_ms)
        print(f"first token (client clock): min {ms[0]:.0f}ms  median {ms[len(ms) // 2]:.0f}ms  max {ms[-1]:.0f}ms over {len(ms)} turns")
    if ctx.observed:
        print("observed side-channel frames: " + ", ".join(f"{k}={v}" for k, v in sorted(ctx.observed.items())))
    print()


async def main_async(args: argparse.Namespace) -> int:
    if _looks_like_prod(args.base_url, args.prefix) and not args.allow_prod:
        print("refusing: target looks like production (pass --allow-prod if you really mean it)", file=sys.stderr)
        return 2
    try:
        transport = await build_transport(args)
    except (SystemExit, RuntimeError) as exc:
        print(f"setup: {exc}", file=sys.stderr)
        return 2

    if args.delete_sessions:
        codes = {sid: await transport.delete_session(sid) for sid in args.delete_sessions.split(",") if sid}
        await transport.close()
        for sid, code in codes.items():
            print(f"{sid} -> {code}")
        return 0 if all(c in (200, 204) for c in codes.values()) else 1

    ctx = Context(transport=transport, args=args)
    results: List[RowResult] = []
    only = set(args.only.split(",")) if args.only else None
    try:
        for name, fn in ROWS:
            if only and not any(part in only for part in name.split("+")):
                continue
            try:
                out = await fn(ctx)
                results.extend(out if isinstance(out, list) else [out])
            except RowSkip as skip:
                results.append(RowResult(name, "SKIP", str(skip)))
            except RowFailure as failure:
                results.append(RowResult(name, "FAIL", str(failure)))
                if args.fail_fast:
                    break
            except Exception as exc:  # noqa: BLE001 — a crashed row is a failed row, not a crashed run
                logger.exception("row %s crashed", name)
                results.append(RowResult(name, "FAIL", f"{type(exc).__name__}: {exc}"))
                if args.fail_fast:
                    break
    finally:
        if ctx.created_sessions and not args.keep:
            deleted = 0
            for sid in ctx.created_sessions:
                if await transport.delete_session(sid) in (200, 204):
                    deleted += 1
            logger.info("deleted %d/%d smoke sessions", deleted, len(ctx.created_sessions))
            if deleted < len(ctx.created_sessions):
                logger.info("not deleted: %s", ", ".join(ctx.created_sessions))
        elif ctx.created_sessions:
            logger.info("kept sessions: %s", ", ".join(ctx.created_sessions))
        await transport.close()

    _print_report(results, ctx, transport.label)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "target": transport.label,
                    "rows": [r.__dict__ for r in results],
                    "observed": ctx.observed,
                    "firstTokenMs": ctx.first_token_ms,
                    "sessions": ctx.created_sessions,
                },
                fh,
                indent=2,
                default=str,
            )
    return 1 if any(r.status == "FAIL" for r in results) else 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base-url", help="app-api origin, e.g. http://127.0.0.1:8000 or https://host/api")
    p.add_argument("--auth", choices=["none", "cognito", "headless-grant"], default="none")
    p.add_argument("--cognito-domain", help="Hosted UI origin (for --auth cognito; informational)")
    p.add_argument("--user-id", help="Cognito sub owning the headless grant (for --auth headless-grant)")
    p.add_argument("--prefix", help="CDK project prefix, e.g. dev-boisestateai-v2 (for --auth headless-grant)")
    p.add_argument("--region", default=os.environ.get("AWS_REGION", "us-west-2"))
    p.add_argument("--model-id", help="model for every turn; default = the user's default model")
    p.add_argument("--tool", default="calculator", help="a granted, deterministic tool for the tool rows")
    p.add_argument("--approval-tool", help="tool id flagged needsApproval (enables tool_approval_resume)")
    p.add_argument("--approval-prompt", help="prompt that makes the model call --approval-tool")
    p.add_argument("--skill-id", help="granted skill id (enables skill_invoke)")
    p.add_argument("--skill-prompt")
    p.add_argument("--agent-id", help="assistant/agent id with a knowledge base (enables kb_agent_first_turn)")
    p.add_argument("--agent-prompt")
    p.add_argument("--with-attachments", action="store_true", help="run the PDF and CSV rows")
    p.add_argument("--expect-quota-exceeded", action="store_true", help="the user is on an exhausted tier")
    p.add_argument("--only", help="comma-separated row names to run")
    p.add_argument("--fail-fast", action="store_true")
    p.add_argument("--keep", action="store_true", help="do not delete the sessions this run created")
    p.add_argument("--delete-sessions", help="comma-separated session ids to delete, then exit (no turns)")
    p.add_argument("--json", help="write the report to this path")
    p.add_argument("--allow-prod", action="store_true")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main_async(parse_args())))
