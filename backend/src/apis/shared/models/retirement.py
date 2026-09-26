"""Model retirement: resolve a requested model id to the one that actually runs.

docs/specs/model-retirement.md §7. The runtime model check
(``AppRoleService.can_access_model``) reads role grants only, never the catalog,
so a retired model has to be caught *before* that check, at every entry point
that turns an id into a model call. This module is the one place that decision
is made:

* ``active`` / ``deprecated`` / no catalog row → the id runs as requested.
  Deprecation is a picker concern; the runtime never sees it.
* ``retired`` with ``replacedBy`` → the successor runs instead, following a
  chain of retired rows (A → B, later B → C).
* ``retired`` with no successor → denied, for everyone — including ``*``
  holders, which is the one thing revoking grants cannot reach.

Callers access-check the **effective** id, so a redirect never grants a model
the invoker could not otherwise use, and cost is priced on the model invoked.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, Optional

from .managed_models import list_all_managed_models
from .models import ManagedModel, ModelStatus

logger = logging.getLogger(__name__)

# A redirect chain longer than this is an admin mistake, not a lineage.
_MAX_REDIRECT_DEPTH = 5


@dataclass(frozen=True)
class EffectiveModel:
    """The outcome of resolving a requested model id."""

    requested_id: str
    model_id: str
    """The id to invoke. Equal to ``requested_id`` unless redirected."""
    record: Optional[ManagedModel] = None
    """Catalog row of ``model_id``, when there is one."""
    retired: Optional[ManagedModel] = None
    """Catalog row of the retired model that was redirected or denied."""
    denied: bool = False

    @property
    def redirected(self) -> bool:
        return self.model_id != self.requested_id

    @property
    def provider(self) -> Optional[str]:
        """The effective model's registered provider.

        A redirect must not keep the caller's provider: it described the retired
        model, and a successor on another transport (``bedrock`` →
        ``bedrock-responses``) misroutes with it.
        """
        return self.record.provider if self.record is not None else None


def resolve_from_catalog(model_id: str, catalog: Iterable[ManagedModel]) -> EffectiveModel:
    """Pure resolution against an already-loaded catalog."""
    by_id: Dict[str, ManagedModel] = {m.model_id: m for m in catalog}
    record = by_id.get(model_id)
    if record is None or record.status != ModelStatus.RETIRED:
        return EffectiveModel(requested_id=model_id, model_id=model_id, record=record)

    seen = {model_id}
    current = record
    for _ in range(_MAX_REDIRECT_DEPTH):
        successor_id = current.replaced_by
        if not successor_id or successor_id in seen:
            break
        seen.add(successor_id)
        successor = by_id.get(successor_id)
        if successor is None or successor.status != ModelStatus.RETIRED:
            # A successor with no row still runs: the write path validates it
            # exists, so a missing row is a later deletion — which behaves like
            # any other id with no row, rather than failing a turn here.
            return EffectiveModel(
                requested_id=model_id, model_id=successor_id, record=successor, retired=record
            )
        current = successor

    if current.replaced_by:
        logger.warning("Model redirect chain from a retired model does not terminate; denying")
    return EffectiveModel(requested_id=model_id, model_id=model_id, retired=record, denied=True)


async def resolve_effective_model(model_id: Optional[str]) -> Optional[EffectiveModel]:
    """Resolve ``model_id`` against the managed-model catalog (cached 60 s).

    ``None`` in, ``None`` out — the caller's own default chain applies. Fails
    open: if the catalog cannot be read, the id runs as requested, exactly as it
    would have before retirement existed.
    """
    if not model_id:
        return None
    try:
        catalog = await list_all_managed_models()
    except Exception:
        logger.warning("Model catalog unavailable; skipping retirement resolution", exc_info=True)
        return EffectiveModel(requested_id=model_id, model_id=model_id)
    effective = resolve_from_catalog(model_id, catalog)
    if effective.redirected:
        logger.info("Redirecting retired model to its successor")
    elif effective.denied:
        logger.info("Denying retired model with no successor")
    return effective


def format_retires_on(retires_on: Optional[str]) -> Optional[str]:
    """``2026-10-31`` → ``October 31, 2026``. Plain date parsing: no timezone to
    shift the day, which is the trap the SPA has to guard against."""
    if not retires_on:
        return None
    try:
        parsed = datetime.strptime(retires_on, "%Y-%m-%d")
    except ValueError:
        return None
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}"


def retired_model_message(retired: ManagedModel, *, agent: bool = False) -> str:
    """The conversational message for a turn denied on a retired model."""
    name = retired.model_name or retired.model_id
    if agent:
        lead = (
            f"This agent runs on **{name}**, which has been retired. "
            "Ask the agent's owner to choose another model."
        )
    else:
        lead = f"**{name}** has been retired and is no longer available. Choose another model to continue."
    if retired.retirement_note:
        return f"{lead}\n\n{retired.retirement_note}"
    return lead


def validate_lifecycle(
    *,
    model_id: str,
    status: ModelStatus,
    replaced_by: Optional[str],
    is_default: bool,
    catalog: Iterable[ManagedModel],
) -> None:
    """Admin write-time rules for the lifecycle fields. Raises ``ValueError``.

    * A non-active model cannot be the default — the SPA picks ``isDefault`` for
      every new chat, which is exactly the new adoption deprecation stops.
    * ``replacedBy`` must name another model that exists, is ``active`` and is
      enabled: it is invoked, so it has to be something that can run — and the
      SPA only follows a redirect to a model it offers, so a disabled successor
      would leave the picker and the runtime disagreeing about what answers.
    """
    if status != ModelStatus.ACTIVE and is_default:
        raise ValueError(
            "A deprecated or retired model cannot be the default. Make another model the default first."
        )
    if not replaced_by:
        return
    if replaced_by == model_id:
        raise ValueError("A model cannot be replaced by itself.")
    successor = next((m for m in catalog if m.model_id == replaced_by), None)
    if successor is None:
        raise ValueError(f"Replacement model '{replaced_by}' is not in the model catalog.")
    if successor.status != ModelStatus.ACTIVE or not successor.enabled:
        raise ValueError(f"Replacement model '{replaced_by}' must be active and enabled.")
