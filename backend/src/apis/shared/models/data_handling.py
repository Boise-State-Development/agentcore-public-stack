"""How a model's provider handles the conversations sent to it.

Most catalog models are ``standard``: Bedrock does not keep prompts or
responses. A few are **covered models** that AWS serves only to an account
whose Bedrock data-retention mode is ``aws_review`` (or ``provider_data_share``
for Mythos). Bedrock keeps their traffic for up to 30 days for automated abuse
detection, and AWS may review flagged traffic by hand (Bedrock user guide,
"Abuse detection" and each model card's "Data Retention" section, read
2026-11-16). That is a different privacy model from the rest of the catalog,
so a row marks it as ``provider_review`` and the platform says so wherever the
model is enabled or chosen:

- **Admins** must acknowledge it to enable the row
  (``ENABLE_ACKNOWLEDGEMENT_REQUIRED``), and the enable is audited.
- **Users** confirm a notice the first time they pick the model, and again
  whenever the notice text changes (``notice_digest``).
- Such a model is never the catalog default and never a retirement successor,
  because both put users on a model they did not choose.

The marking has a floor: a row whose id names a covered model is
``provider_review`` whatever the request says (``requires_provider_review``).
An admin can also mark any other model, but cannot unmark a covered one.
"""

from __future__ import annotations

import hashlib
import re
from enum import Enum
from typing import Iterable, Optional, Protocol


class DataHandling(str, Enum):
    STANDARD = "standard"
    PROVIDER_REVIEW = "provider_review"


#: Shown to admins and users when a ``provider_review`` row has no notice of its
#: own. Governance owns the final wording (issue #1845); an admin sets it per
#: row, and changing it asks every user to confirm again.
DEFAULT_PROVIDER_REVIEW_NOTICE = (
    "Conversations with this model are handled differently. To offer it, AWS "
    "keeps everything sent to it and received from it for up to 30 days to "
    "detect abuse, and AWS staff may review conversations its safety systems "
    "flag. Before using it with sensitive or protected information, check that "
    "your organization allows it."
)

NOTICE_MAX_LENGTH = 1_000

# Every Fable 5.x model card requires `aws_review`; Mythos 5.1 and 5.2 require
# `aws_review` or `provider_data_share` (re-read 2026-11-16). A later release in
# either line is assumed to be covered until its card says otherwise: wrongly
# showing the notice costs a click, wrongly hiding it costs the disclosure.
_COVERED_MODEL_ID = re.compile(r"claude-(fable|mythos)-(\d+)")


def requires_provider_review(model_id: Optional[str]) -> bool:
    """Whether ``model_id`` names a covered model, in any Bedrock spelling."""
    match = _COVERED_MODEL_ID.search((model_id or "").lower())
    return match is not None and int(match.group(2)) >= 5


def resolve_data_handling(model_id: Optional[str], requested: Optional[DataHandling]) -> DataHandling:
    """The marking a row is stored with: the floor wins over the request."""
    if requires_provider_review(model_id):
        return DataHandling.PROVIDER_REVIEW
    return requested or DataHandling.STANDARD


def effective_notice(data_handling: DataHandling, notice: Optional[str]) -> Optional[str]:
    """The text users and admins see, or ``None`` for a standard model."""
    if data_handling != DataHandling.PROVIDER_REVIEW:
        return None
    return (notice or "").strip() or DEFAULT_PROVIDER_REVIEW_NOTICE


def notice_digest(text: Optional[str]) -> Optional[str]:
    """A short, stable id for a notice's wording. A user's acknowledgement
    records it, so a reworded notice is asked again."""
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class _CatalogRow(Protocol):
    model_id: str
    data_handling: DataHandling


def validate_data_handling(
    *,
    model_id: str,
    data_handling: DataHandling,
    is_default: bool,
    replaced_by: Optional[str],
    catalog: Iterable[_CatalogRow],
) -> None:
    """Admin write-time rules. Raises ``ValueError``.

    A ``provider_review`` model cannot be the catalog default, and no model can
    name one as its retirement successor: both move users onto it without them
    choosing it, so they would never see the notice.
    """
    if data_handling == DataHandling.PROVIDER_REVIEW and is_default:
        raise ValueError(
            "A model whose conversations are kept for provider review cannot be the "
            "default: users would be put on it without seeing its data-handling notice."
        )
    if not replaced_by:
        return
    successor = next((m for m in catalog if m.model_id == replaced_by), None)
    if successor is not None and resolve_data_handling(
        successor.model_id, successor.data_handling
    ) == DataHandling.PROVIDER_REVIEW:
        raise ValueError(
            f"Replacement model '{replaced_by}' keeps conversations for provider review, so "
            "users would be moved onto it without seeing its notice. Pick another successor."
        )


#: The error an enable without the admin's acknowledgement gets (HTTP 409).
ENABLE_ACKNOWLEDGEMENT_REQUIRED = (
    "This model's conversations are kept for provider review. Enabling it requires "
    "acknowledgeDataHandling: true, after reading its data-handling notice."
)


def enable_needs_acknowledgement(
    *,
    before_enabled: bool,
    before_handling: DataHandling,
    after_enabled: bool,
    after_handling: DataHandling,
) -> bool:
    """Whether a write puts users in reach of a ``provider_review`` model that
    they could not reach before: enabling one, or marking an enabled one."""
    if not after_enabled or after_handling != DataHandling.PROVIDER_REVIEW:
        return False
    return not before_enabled or before_handling != DataHandling.PROVIDER_REVIEW
