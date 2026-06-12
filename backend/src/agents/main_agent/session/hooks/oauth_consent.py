"""OAuth consent gate for external MCP tools.

Fires on every `BeforeToolCallEvent`. If the tool about to run is backed by
an MCP server that requires user-federated OAuth (per the tool catalog),
the hook ensures we have an access token in the in-process cache. If we
don't, it calls `event.interrupt(...)` to pause the agent mid-turn and
hand the authorization URL back to the caller.

Tools can be OAuth-gated through indirection too: in skills mode a bound
external MCP tool runs behind the `skill_executor` meta-tool, so the
selected tool alone doesn't identify the MCP server. The optional
`tool_use_provider_lookup` resolves the provider from the raw tool_use
(name + input) in that case — same gate, same interrupt, same resume.

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
    BeforeInvocationEvent,
    BeforeToolCallEvent,
    HookProvider,
    HookRegistry,
)

from agents.main_agent.integrations import oauth_token_cache
from apis.shared.oauth.agentcore_identity import (
    CallbackUrlUnavailableError,
    WorkloadTokenUnavailableError,
    custom_parameters_for,
    get_agentcore_identity_client,
)

logger = logging.getLogger(__name__)


# Markers that indicate an OAuth-style auth failure in a tool result.
# A false positive triggers an unnecessary OAuth popup — far more
# disruptive than a missed match (which surfaces the underlying error to
# the user). So we err on the side of high-confidence signals only.
#
# Tiers:
#   1. HTTP 401 with negative lookarounds for path segments / adjacent
#      digits. Bare "401" in MCP error text is almost always an HTTP
#      status code in practice.
#   2. "Unauthorized" only when paired with an HTTP/status/code keyword.
#      The bare word fires on prose like "you are not authorized to view
#      this calendar" — which is application-level, not OAuth.
#   3. Unambiguous OAuth/token signals stand alone — `invalid_token`,
#      `invalid_grant` (refresh-token revocation), Google API's
#      `UNAUTHENTICATED` and `invalid authentication credentials`.
#
# We only run this on results whose `status == "error"`
# (see `_looks_like_auth_failure`), so even the broader patterns above
# are gated by an explicit failure signal from the MCP framework.
_AUTH_FAILURE_PATTERN = re.compile(
    r"(?<![\w/])401(?![\w/])"
    r"|\b(?:http|status|response|code)\b[^\n]{0,20}\bunauthoriz(?:ed|e)\b"
    r"|\bunauthoriz(?:ed|e)\b[^\n]{0,20}\b(?:http|status|response|code|401)\b"
    r"|\binvalid[_\s-]?token\b"
    r"|\bexpired[_\s-]?token\b"
    r"|\btoken[_\s-]?expired\b"
    r"|\brejected the oauth token\b"
    r"|\boauth token (?:has )?expired\b"
    r"|\binvalid[_\s-]?grant\b"
    r"|\binvalid[_\s-]?authentication[_\s-]?credentials\b"
    r"|\bUNAUTHENTICATED\b",
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

# Second-chance provider resolution from the raw `tool_use` dict (name +
# input) for tools that dispatch indirectly — SkillAgent's `skill_executor`
# meta-tool runs folded external MCP tools, so `selected_tool` is the
# executor itself and `ProviderLookup` can't map it. Consulted only when
# `ProviderLookup` returns None; return None for anything that isn't an
# indirect OAuth-gated dispatch.
ToolUseProviderLookup = Callable[[dict], Optional[str]]

# Returns OAuth scopes for a provider_id. May be sync or async; the hook
# awaits the result either way so we can read from an async repository
# without forcing a sync wrapper.
ScopesLookup = Callable[[str], Union[list[str], Awaitable[list[str]]]]

# Returns the provider's vendor type (e.g. "google", "microsoft") for a
# provider_id, or None if unknown / no per-vendor params needed. Optional —
# omitted in older tests; without it AgentCore Identity gets no
# `customParameters`, which means Google won't issue a refresh token and
# the vault entry expires after ~1 hour.
ProviderTypeLookup = Callable[[str], Union[Optional[str], Awaitable[Optional[str]]]]

# Returns admin-supplied OAuth params (e.g. `hd=mycorp.com` for Google
# Workspace domain restriction) for a provider_id. Merged with the
# vendor baseline by `custom_parameters_for`; baseline wins on conflict.
CustomParametersLookup = Callable[
    [str], Union[Optional[dict[str, str]], Awaitable[Optional[dict[str, str]]]]
]

# Returns whether the caller has been marked disconnected from this provider
# (set by the /disconnect route or by a prior 401 retry). When True, the
# hook bypasses the local token cache and asks AgentCore Identity for a
# fresh consent URL with `force_authentication=True`.
DisconnectedLookup = Callable[[str], Union[bool, Awaitable[bool]]]


class OAuthConsentHook(HookProvider):
    """Pause the agent if a tool needs OAuth and we don't have a token yet."""

    def __init__(
        self,
        user_id: str,
        provider_lookup: ProviderLookup,
        scopes_lookup: ScopesLookup,
        provider_type_lookup: Optional[ProviderTypeLookup] = None,
        custom_parameters_lookup: Optional[CustomParametersLookup] = None,
        disconnected_lookup: Optional[DisconnectedLookup] = None,
        tool_use_provider_lookup: Optional[ToolUseProviderLookup] = None,
    ):
        """Initialize.

        Args:
            user_id: User the agent is running for. Used as cache key and
                passed to AgentCore Identity for the local-dev workload-token
                fallback (no-op in production).
            provider_lookup: See `ProviderLookup`.
            scopes_lookup: See `ScopesLookup`.
            provider_type_lookup: See `ProviderTypeLookup`. Optional. When
                provided, the hook forwards vendor-specific OAuth params
                (e.g. Google's `access_type=offline`) to AgentCore Identity.
            custom_parameters_lookup: See `CustomParametersLookup`.
                Optional. Admin-supplied extras to merge with the vendor
                baseline.
            disconnected_lookup: See `DisconnectedLookup`. Optional. When
                omitted, the hook never bypasses the local token cache —
                effectively assumes the user has not disconnected. Wire
                this to the durable disconnect repository in production so
                a /disconnect on one replica is visible from any other.
            tool_use_provider_lookup: See `ToolUseProviderLookup`. Optional.
                When omitted, only `provider_lookup` is consulted and
                indirectly-dispatched tools (skill meta-tools) are not
                OAuth-gated.
        """
        self._user_id = user_id
        self._provider_lookup = provider_lookup
        self._scopes_lookup = scopes_lookup
        self._provider_type_lookup = provider_type_lookup
        self._custom_parameters_lookup = custom_parameters_lookup
        self._disconnected_lookup = disconnected_lookup
        self._tool_use_provider_lookup = tool_use_provider_lookup
        # Cache scopes per provider for the lifetime of this hook (one agent
        # invocation). Avoids repeated DB hits if the same provider is used
        # across multiple tool calls in a single turn.
        self._scopes_cache: dict[str, list[str]] = {}
        # Same cache shape for provider_type. `None` is a legitimate value
        # (vendor without extra params), so we use a separate sentinel set
        # to distinguish "unknown" from "looked up, no extras needed".
        self._provider_type_cache: dict[str, Optional[str]] = {}
        self._provider_type_cache_keys: set[str] = set()
        self._custom_parameters_cache: dict[str, Optional[dict[str, str]]] = {}
        self._custom_parameters_cache_keys: set[str] = set()
        # Providers that already burned their one 401-retry in the current
        # turn. The agent instance is cached across turns by `get_agent`, so
        # this set must be reset on `BeforeInvocationEvent`. Without the cap,
        # a misconfigured provider (wrong scope, perma-401) would loop:
        # the cache is cleared, AgentCore returns the same expired/invalid
        # token from the vault, the tool 401s again, and so on.
        self._reauth_attempted_providers: set[str] = set()

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(BeforeInvocationEvent, self._on_invocation_start)
        registry.add_callback(BeforeToolCallEvent, self._gate)
        registry.add_callback(AfterToolCallEvent, self._handle_auth_failure)

    def _on_invocation_start(self, event: BeforeInvocationEvent) -> None:
        """Reset per-turn state at the start of each agent invocation.

        Both fresh turns and resumes (with `interrupt_responses`) trigger
        BeforeInvocationEvent. Resetting on resume is intentional: the user
        just took an action (consent), so they've signaled they want to
        keep trying — start their retry budget fresh.
        """
        self._reauth_attempted_providers.clear()

    def _resolve_provider_id(
        self, selected_tool: Any, tool_use: Any
    ) -> Optional[str]:
        """Map a tool call to its OAuth provider, seeing through indirection.

        `provider_lookup` handles directly-selected MCP tools. When it can't
        map the tool (e.g. SkillAgent's `skill_executor` meta-tool, whose
        folded target only appears in the tool_use input), the optional
        `tool_use_provider_lookup` gets a second chance with the raw
        tool_use dict.
        """
        provider_id = self._provider_lookup(selected_tool)
        if provider_id:
            return provider_id
        if self._tool_use_provider_lookup is None or not isinstance(tool_use, dict):
            return None
        return self._tool_use_provider_lookup(tool_use)

    async def _gate(self, event: BeforeToolCallEvent) -> None:
        provider_id = self._resolve_provider_id(event.selected_tool, event.tool_use)
        if not provider_id:
            return  # Not an OAuth-gated tool

        force_reauth = await self._is_disconnected(provider_id)

        # Fast path: token already in cache (from a prior call this process,
        # or warmed by a previous turn). Skipped when the durable disconnect
        # repository says this user wants a fresh consent — either because
        # they pressed "Disconnect" (possibly on a different replica) or
        # because a prior tool call returned 401.
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

        # Consent required: pause the agent. The interrupt name is
        # provider-scoped, but Strands' BeforeToolCallEvent._interrupt_id
        # also folds in `tool_use.toolUseId` (see strands/hooks/events.py),
        # so two parallel tool calls to the same provider in one turn
        # produce distinct interrupt ids and surface as separate
        # `oauth_required` events. If Strands ever changes that ID scheme
        # we'd need to incorporate toolUseId here ourselves —
        # `test_parallel_tool_calls_same_provider_produce_distinct_interrupts`
        # is the regression guard.
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
        provider_type = await self._resolve_provider_type(provider_id)
        admin_extras = await self._resolve_custom_parameters(provider_id)
        identity_client = get_agentcore_identity_client()

        try:
            result = await identity_client.get_token_for_user(
                provider_name=provider_id,
                scopes=scopes,
                user_id=self._user_id,
                force_authentication=force_authentication,
                custom_parameters=custom_parameters_for(
                    provider_type,
                    admin_extras,
                    force_authentication=force_authentication,
                ),
            )
        except WorkloadTokenUnavailableError:
            logger.error(
                "No workload token on context for provider=%s — "
                "AgentCoreContextMiddleware may be misconfigured",
                provider_id,
            )
            return None
        except CallbackUrlUnavailableError as err:
            logger.error(
                "No OAuth2 callback URL for provider=%s: %s",
                provider_id,
                err,
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
        """Detect a 401 from an OAuth-gated MCP tool and retry.

        We clear the local hot-path cache and set `event.retry = True`.
        Strands' tool executor then re-fires `BeforeToolCallEvent`, our
        `_gate` callback misses the cache, and re-asks AgentCore Identity.
        AgentCore handles refresh transparently:
          * If the access_token was just expired, it uses the vault's
            refresh_token to mint a new one and returns it — the user
            never sees a prompt.
          * If the refresh_token itself is dead (user revoked our app at
            the provider, or it lapsed), AgentCore returns an
            authorization URL instead, which `_gate` surfaces as the
            standard `oauth_required` interrupt.

        We deliberately do NOT write the durable disconnect flag here. A
        401 is most commonly an expired access token, and writing the
        flag would force `_gate` to call AgentCore with
        `force_authentication=True` — which bypasses the vault entirely,
        ignores the still-valid refresh_token, and prompts the user to
        re-consent unnecessarily. The disconnect flag is reserved for
        explicit user intent (the "Disconnect" button in the settings
        page).
        """
        provider_id = self._resolve_provider_id(event.selected_tool, event.tool_use)
        if not provider_id:
            return

        if not _looks_like_auth_failure(event.result):
            return

        # Avoid both an infinite retry loop within a single tool call and a
        # consent-prompt storm across multiple tool calls in the same turn:
        # retry at most once per provider per turn. The set is reset on
        # `BeforeInvocationEvent`, so a fresh turn (or a resume after the
        # user re-consented) gets a fresh budget.
        if provider_id in self._reauth_attempted_providers:
            logger.warning(
                "OAuth re-auth already attempted this turn for provider=%s "
                "(tool=%s); not retrying again",
                provider_id,
                event.tool_use.get("name"),
            )
            return
        self._reauth_attempted_providers.add(provider_id)

        logger.info(
            "Detected OAuth 401 for tool=%s provider=%s; clearing token cache and retrying",
            event.tool_use.get("name"),
            provider_id,
        )
        # Drop the local hot-path token so the BeforeToolCallEvent retry
        # doesn't short-circuit to it. The retry will re-fetch from
        # AgentCore Identity, which handles refresh internally.
        oauth_token_cache.clear_user_provider(self._user_id, provider_id)
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

    async def _resolve_provider_type(self, provider_id: str) -> Optional[str]:
        if self._provider_type_lookup is None:
            return None
        if provider_id in self._provider_type_cache_keys:
            return self._provider_type_cache.get(provider_id)
        result = self._provider_type_lookup(provider_id)
        if inspect.isawaitable(result):
            provider_type = await result
        else:
            provider_type = result
        self._provider_type_cache[provider_id] = provider_type
        self._provider_type_cache_keys.add(provider_id)
        return provider_type

    async def _resolve_custom_parameters(
        self, provider_id: str
    ) -> Optional[dict[str, str]]:
        if self._custom_parameters_lookup is None:
            return None
        if provider_id in self._custom_parameters_cache_keys:
            return self._custom_parameters_cache.get(provider_id)
        result = self._custom_parameters_lookup(provider_id)
        if inspect.isawaitable(result):
            extras = await result
        else:
            extras = result
        self._custom_parameters_cache[provider_id] = extras
        self._custom_parameters_cache_keys.add(provider_id)
        return extras

    async def _is_disconnected(self, provider_id: str) -> bool:
        """Read the durable disconnect flag (DDB-backed in production).

        Not memoized: a disconnect request can land on this replica between
        two tool calls in the same turn, and we want the second tool call
        to honor it. The DDB read is a single GetItem keyed on
        `(user_id, provider_id)`, so the cost is negligible.
        """
        if self._disconnected_lookup is None:
            return False
        result = self._disconnected_lookup(provider_id)
        if inspect.isawaitable(result):
            return bool(await result)
        return bool(result)

