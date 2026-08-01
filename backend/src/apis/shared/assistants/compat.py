"""Agent Designer Phase 1 — legacy-Assistant → Agent compat mapping (D2).

The Agent contract evolves the ``rag-assistants`` store IN PLACE: there is no
parallel table and no migration. A legacy Assistant row (one with no ``bindings`` /
``modelConfig`` attributes) is projected into the Agent shape *on read* by the pure
functions here — nothing is ever backfilled.

Key grounding facts (verified against the live schema):

- **No per-assistant model exists today.** The model is resolved per invocation
  (request → user default → system default). So an absent ``modelConfig`` maps to
  ``None`` meaning "resolve exactly as today" — we do NOT fabricate a model id (R1).
- **The KB is not first-class yet (F4 deferred).** ``vector_index_id`` is a *shared*
  index name, "not user-configurable". The only stable per-Assistant KB identity is
  the assistant id itself (retrieval filters vectors by ``assistant_id``). So the
  synthesized ``knowledge_base`` binding uses ``ref == assistant_id`` (R4). When F4
  lands, this ref becomes a real KB id with no shape change here.

NOTE: bindings carry *refs only*, never bodies — the METADATA item is 400 KB-capped
and already holds the full instructions. Any future kind needing a per-binding payload
must go to a child row (``AST#{id}/BINDING#…``), not inline here.
"""

from typing import List

from apis.shared.assistants.icons import icon_url
from apis.shared.assistants.models import AgentBinding, Assistant


def effective_bindings(assistant: Assistant) -> List[AgentBinding]:
    """Return the Agent's bindings, synthesizing the legacy KB binding when absent.

    - Stored bindings present  → returned verbatim (unknown ``kind`` values survive).
    - Stored bindings absent    → a single ``knowledge_base`` binding whose ``ref`` is
      the assistant id (the KB's only stable identity today).
    """
    if assistant.bindings is not None:
        return assistant.bindings

    return [
        AgentBinding(
            kind="knowledge_base",
            ref=assistant.assistant_id,
            config={"vectorIndexId": assistant.vector_index_id},
        )
    ]


def to_agent_view(assistant: Assistant) -> dict:
    """Project an Assistant into the resolved Agent read-shape.

    Returns a plain dict (camelCase keys) — the HTTP response model is a route concern
    (Phase 3). ``agentId`` aliases ``assistantId``; legacy ids remain valid. ``owner_id``
    is deliberately omitted (never returned to clients), matching ``AssistantResponse``.

    ``instructions`` is projected here and **dropped by the route** for anyone who is not
    an owner or editor (Marketplace Phase 3). The gate lives at the route because that is
    where the caller's permission is known; this function stays a pure record projection.
    """
    return {
        "agentId": assistant.assistant_id,
        "ownerName": assistant.owner_name,
        "name": assistant.name,
        "description": assistant.description,
        "instructions": assistant.instructions,
        "modelConfig": assistant.model_settings.model_dump(by_alias=True) if assistant.model_settings else None,
        "bindings": [b.model_dump(by_alias=True) for b in effective_bindings(assistant)],
        "visibility": assistant.visibility,
        "tags": assistant.tags or [],
        "starters": assistant.starters or [],
        "emoji": assistant.emoji,
        "imageUrl": assistant.image_url,
        "usageCount": assistant.usage_count,
        "status": assistant.status,
        "createdAt": assistant.created_at,
        "updatedAt": assistant.updated_at,
        # Marketplace (Phase 1 fields, projected in Phase 3 when the detail page first
        # needed them). All three are ``None`` on an agent that was never submitted —
        # the D3 backfill default — and every route serving ``AgentResponse`` uses
        # ``response_model_exclude_none``, so that payload is unchanged.
        "tagline": assistant.tagline,
        "iconKey": assistant.icon_key,
        # Derived, never stored (Phase 4): the record holds the S3 key, the read shape
        # holds where to fetch it from. ``None`` when unset → the SPA's generated gradient.
        "iconUrl": icon_url(assistant.assistant_id, assistant.icon_key),
        "listing": assistant.listing.model_dump(by_alias=True) if assistant.listing else None,
    }
