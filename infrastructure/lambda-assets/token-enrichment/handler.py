"""Cognito Pre-Token-Generation (v2) trigger — access-token claim enrichment.

WHY THIS EXISTS
---------------
Personalized MCP tools need to identify the logged-in user, but the only token
forwarded end-to-end to MCP servers is the Cognito *access* token, which carries
`sub` and little else. The identity-rich claims (e.g. an employee number mapped
to ``custom:provider_sub``) live on the user's pool attributes. This trigger
copies configured attributes into named claims on the access token so the
existing SPA -> app-api -> inference-api -> MCP forwarding path works unchanged.

See docs/specs/MCP_USER_IDENTITY_FORWARDING_SPEC.md.

CONFIGURATION
-------------
``ACCESS_TOKEN_CLAIMS`` env var — a JSON object ``{claimName: sourceAttribute}``.
For each entry, if the user has ``sourceAttribute`` populated, its value is
written to ``claimName`` on the access token. Missing attributes are skipped
(native Cognito users lack federated attributes, and that is fine — personalized
tools should degrade gracefully to "no identity" rather than error). Claim names
SHOULD be namespaced (full reverse-DNS form, e.g.
``https://example.com/employee_number``) to avoid colliding with reserved claims.

Example:
    ACCESS_TOKEN_CLAIMS = {"https://boisestate.edu/employee_number": "custom:provider_sub"}

FAIL-OPEN CONTRACT (critical)
-----------------------------
This trigger runs on EVERY token issuance for the entire user pool. A raised
exception here blocks login pool-wide. Therefore the handler is dependency-free,
does no I/O, and wraps its whole body in a catch-all that returns the event
UNCHANGED on any error. The worst-case failure mode is "the claim is not added"
(tools report no identity), never "the user cannot log in".

V2 EVENT SHAPE (relevant subset)
--------------------------------
    {
      "version": "2",
      "triggerSource": "TokenGeneration_...",
      "request": { "userAttributes": { "sub": "...", "custom:provider_sub": "..." } },
      "response": {
        "claimsAndScopeOverrideDetails": {
          "accessTokenGeneration": {
            "claimsToAddOrOverride": { "<claimName>": "<value>" }
          }
        }
      }
    }

Cognito reads ``response.claimsAndScopeOverrideDetails.accessTokenGeneration
.claimsToAddOrOverride`` and merges those claims into the issued access token.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _load_claim_map() -> dict[str, str]:
    """Parse the ACCESS_TOKEN_CLAIMS env var into a {claim: attribute} map.

    Returns an empty map (no-op enrichment) on any malformed/absent value.
    Only string->string entries are kept; anything else is ignored.
    """
    raw = os.environ.get("ACCESS_TOKEN_CLAIMS", "").strip()
    if not raw:
        return {}
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        return {}
    return {
        str(claim): str(attr)
        for claim, attr in parsed.items()
        if isinstance(claim, str) and isinstance(attr, str)
    }


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Enrich the access token with configured identity claims (fail-open).

    On ANY exception the original event is returned unchanged so that a bug or
    unexpected payload can never block authentication for the whole pool.
    """
    try:
        claim_map = _load_claim_map()
        if not claim_map:
            return event

        user_attributes = (event.get("request") or {}).get("userAttributes") or {}

        # Collect the claims we can actually populate (attribute present + truthy).
        claims_to_add: dict[str, str] = {}
        for claim_name, source_attribute in claim_map.items():
            value = user_attributes.get(source_attribute)
            if value is not None and value != "":
                claims_to_add[claim_name] = value

        if not claims_to_add:
            return event

        # Merge into response.claimsAndScopeOverrideDetails.accessTokenGeneration
        # .claimsToAddOrOverride, creating the nested structure as needed and
        # preserving anything Cognito or another step already populated.
        response = event.setdefault("response", {})
        details = response.setdefault("claimsAndScopeOverrideDetails", {})
        if details is None:  # Cognito may send an explicit null.
            details = {}
            response["claimsAndScopeOverrideDetails"] = details
        access_token_gen = details.setdefault("accessTokenGeneration", {})
        existing = access_token_gen.setdefault("claimsToAddOrOverride", {})
        existing.update(claims_to_add)

        return event
    except Exception:  # noqa: BLE001 — fail-open is the whole point.
        # Never block login. Log for diagnosis and return the event untouched.
        logger.exception("token-enrichment trigger failed; returning event unchanged")
        return event
