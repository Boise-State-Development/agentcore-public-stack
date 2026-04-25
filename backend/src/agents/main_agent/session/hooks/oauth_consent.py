"""OAuth consent gate for external MCP tools.

Fires on every `BeforeToolCallEvent`. If the tool about to run is backed by
an MCP server that requires user-federated OAuth (per the tool catalog),
the hook ensures we have an access token in the in-process cache. If we
don't, it calls `event.interrupt(...)` to pause the agent mid-turn and
hand the authorization URL back to the caller.

When the user completes consent in the popup and the frontend resumes the
turn, the hook fires a second time and `event.interrupt(...)` returns the
user's response (instead of raising). At that point AgentCore Identity has
the new token in its vault, so we re-fetch and warm the cache; the
`OAuthBearerAuth` token provider then injects it on the next MCP request.

The hook never aborts the turn on its own — `cancel_tool` is reserved for
genuine refusal (e.g. consent declined). If the user closes the popup we
don't reach that path; the agent simply remains paused until a resume
arrives or the session times out.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from typing import Any, Awaitable, Callable, Optional, Union

from strands.hooks import (
    AfterToolCallEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from agents.main_agent.integrations import oauth_token_cache
from agents.main_agent.integrations.agentcore_identity import (
    WorkloadTokenUnavailableError,
    get_agentcore_identity_client,
)
from apis.shared.oauth import session_cache

logger = logging.getLogger(__name__)


# String markers that indicate an OAuth-style auth failure in a tool
# result. MCP servers vary in how they format errors, so we match a small
# set of unambiguous signals: the literal HTTP code, "Unauthorized", and
# explicit OAuth/token-rejected language.
#
# Every alternative is word-bounded. `401` additionally excludes adjacent
# `/` so path segments like `/v1/401/...` in an error message do not
# trigger a false-positive reauth. We only run the pattern on results
# whose `status == "error"` (see `_looks_like_auth_failure`), so this
# plus `\b` on every other clause is tight enough in practice.
_AUTH_FAILURE_PATTERN = re.compile(
    r"(?<![\w/])401(?![\w/])"
    r"|\bunauthorized\b"
    r"|\binvalid[_\s-]token\b"
    r"|\bexpired[_\s-]token\b"
    r"|\btoken[_\s-]expired\b"
    r"|\brejected the oauth token\b"
    r"|\boauth token (?:has )?expired\b",
    re.IGNORECASE,
)


def _looks_like_auth_failure(tool_result: Any) -> bool:
    """Heuristic: does this tool result look like an OAuth 401?

    Inspects the result's status and content for one of the markers above.
    False positives here just trigger a wasted retry; false negatives
    leave the user stuck with a stale token, so we err on the side of
    matching.
    """
    if not isinstance(tool_result, dict):
        return False
    if tool_result.get("status") != "error":
        return False
    for block in tool_result.get("content", []) or []:
        if not isinstance(block, dict):
            continue
        text = block.get("text") or ""
        if isinstance(text, str) and _AUTH_FAILURE_PATTERN.search(text):
            return True
    return False


# Returns provider_id for a Strands `selected_tool`, or None if the tool
# isn't OAuth-gated. Encapsulates the MCPClient -> provider mapping.
ProviderLookup = Callable[[Any], Optional[str]]

# Returns OAuth scopes for a provider_id. May be sync or async; the hook
# awaits the result either way so we can read from an async repository
# without forcing a sync wrapper.
ScopesLookup = Callable[[str], Union[list[str], Awaitable[list[str]]]]


class OAuthConsentHook(HookProvider):
    """Pause the agent if a tool needs OAuth and we don't have a token yet."""

    def __init__(
        self,
        user_id: str,
        provider_lookup: ProviderLookup,
        scopes_lookup: ScopesLookup,
    ):
        """Initialize.

        Args:
            user_id: User the agent is running for. Used as cache key and
                passed to AgentCore Identity for the local-dev workload-token
                fallback (no-op in production).
            provider_lookup: See `ProviderLookup`.
            scopes_lookup: See `ScopesLookup`.
        """
        self._user_id = user_id
        self._provider_lookup = provider_lookup
        self._scopes_lookup = scopes_lookup
        # Cache scopes per provider for the lifetime of this hook (one agent
        # invocation). Avoids repeated DB hits if the same provider is used
        # across multiple tool calls in a single turn.
        self._scopes_cache: dict[str, list[str]] = {}

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeToolCallEvent, self._gate)
        registry.add_callback(AfterToolCallEvent, self._handle_auth_failure)

    async def _gate(self, event: BeforeToolCallEvent) -> None:
        provider_id = self._provider_lookup(event.selected_tool)
        if not provider_id:
            return  # Not an OAuth-gated tool

        force_reauth = oauth_token_cache.needs_force_reauth(self._user_id, provider_id)

        # Fast path: token already in cache (from a prior call this process,
        # or warmed by a previous turn). Skipped when a prior tool call
        # surfaced a 401 and asked us to bypass the cache.
        if not force_reauth and oauth_token_cache.get(self._user_id, provider_id):
            return

        # Slow path: ask AgentCore Identity. Either we get a token (vault
        # hit, cache it and proceed) or a consent URL (interrupt the turn).
        # `force_reauth` makes us bypass AgentCore's vault entirely so a
        # stale post-revocation token doesn't get re-served.
        token_or_url = await self._fetch_token_or_url(
            provider_id, force_authentication=force_reauth
        )
        if token_or_url is None:
            # Couldn't resolve — let the tool run; the MCP server will return
            # 401 and the resulting tool_error surfaces conversationally.
            return

        if token_or_url["token"]:
            oauth_token_cache.set(self._user_id, provider_id, token_or_url["token"])
            return

        # Remember the session_uri so `complete_consent` can verify this
        # user initiated the flow. Without this, the popup's finalize call
        # is rejected 403 — the settings-page `initiate_consent` path
        # remembers its own session, and this tool-triggered path must do
        # the same. Soft-fails on extraction: AgentCore's userIdentifier
        # binding still protects completion if we can't track locally.
        session_uri = session_cache.extract_session_uri(token_or_url["url"] or "")
        if session_uri:
            session_cache.remember(self._user_id, session_uri)
        else:
            logger.warning(
                "Could not extract session_uri from tool-triggered consent URL "
                "for user=%s provider=%s; popup finalize may be rejected",
                self._user_id,
                provider_id,
            )

        # Consent required: pause the agent. The interrupt name is namespaced
        # by provider so the SDK generates a stable interrupt id we can
        # correlate with the user's response.
        response = event.interrupt(
            name=f"oauth:{provider_id}",
            reason={
                "type": "oauth_required",
                "providerId": provider_id,
                "authorizationUrl": token_or_url["url"],
            },
        )

        # We're past the interrupt — the user resumed. Re-fetch from the
        # vault (AgentCore Identity should now have the token after consent
        # completion) and warm the cache. We ignore `response` content —
        # successful resumption is itself the signal that consent happened.
        del response
        refreshed = await self._fetch_token_or_url(provider_id)
        if refreshed and refreshed["token"]:
            oauth_token_cache.set(self._user_id, provider_id, refreshed["token"])
            return

        # Resumed but still no token — treat as declined. cancel_tool emits a
        # tool_error to the model so it can apologize/replan.
        event.cancel_tool = (
            f"User did not complete authorization for {provider_id}; "
            "the tool cannot run."
        )

    async def _fetch_token_or_url(
        self, provider_id: str, *, force_authentication: bool = False
    ) -> Optional[dict]:
        """Return {'token': str|None, 'url': str|None} or None on hard error."""
        scopes = await self._resolve_scopes(provider_id)
        identity_client = get_agentcore_identity_client()

        try:
            result = await identity_client.get_token_for_user(
                provider_name=provider_id,
                scopes=scopes,
                user_id=self._user_id,
                force_authentication=force_authentication,
            )
        except WorkloadTokenUnavailableError:
            logger.error(
                "No workload token on context for provider=%s — "
                "AgentCoreContextMiddleware may be misconfigured",
                provider_id,
            )
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(
                "Failed to fetch OAuth token for provider=%s", provider_id
            )
            return None

        return {
            "token": result.access_token,
            "url": result.authorization_url,
        }

    async def _handle_auth_failure(self, event: AfterToolCallEvent) -> None:
        """Detect a 401 from an OAuth-gated MCP tool and retry with fresh consent.

        AgentCore Identity has no revoke API, so when the user revokes our
        app at the provider (or the refresh token expires), AgentCore's
        vault keeps serving the now-stale token. The MCP server rejects it
        with a 401 — and that's where the staleness first becomes visible.

        We detect the 401 in the tool result, mark the (user, provider)
        for forced re-consent in the cache, and set `event.retry = True`.
        Strands' tool executor then re-fires `BeforeToolCallEvent`, our
        `_gate` callback sees the force-reauth flag, asks AgentCore for a
        fresh consent URL with `force_authentication=True`, and raises an
        interrupt — same path as a first-time consent.
        """
        provider_id = self._provider_lookup(event.selected_tool)
        if not provider_id:
            return

        if not _looks_like_auth_failure(event.result):
            return

        # Avoid an infinite retry loop if the refreshed token also fails:
        # retry once per (toolUseId, provider) per turn. We piggyback on
        # invocation_state, which Strands carries across the retry inside
        # the same BeforeToolCallEvent → AfterToolCallEvent cycle.
        attempted = event.invocation_state.setdefault("_oauth_reauth_attempted", set())
        key = (event.tool_use.get("toolUseId"), provider_id)
        if key in attempted:
            logger.warning(
                "OAuth re-auth already attempted for tool=%s provider=%s; not retrying again",
                event.tool_use.get("name"),
                provider_id,
            )
            return
        attempted.add(key)

        logger.info(
            "Detected OAuth 401 for tool=%s provider=%s; clearing token cache and retrying",
            event.tool_use.get("name"),
            provider_id,
        )
        oauth_token_cache.mark_force_reauth(self._user_id, provider_id)
        event.retry = True

    async def _resolve_scopes(self, provider_id: str) -> list[str]:
        if provider_id in self._scopes_cache:
            return self._scopes_cache[provider_id]
        result = self._scopes_lookup(provider_id)
        if inspect.isawaitable(result):
            scopes = await result
        else:
            scopes = result
        scopes = list(scopes or [])
        self._scopes_cache[provider_id] = scopes
        return scopes
