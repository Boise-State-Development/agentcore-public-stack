"""Recognizing the one DynamoDB error that a user-facing read should absorb.

**A missing GSI is a deploy state, not a bug.** An index the code queries can be absent
for several ordinary reasons, none of which mean the request is malformed:

* CloudFormation reports ``CREATE_COMPLETE`` while a new GSI is still ``CREATING`` — the
  stack is done, the index is not yet queryable.
* A CloudFormation deploy rolls back for an unrelated reason, taking the new indexes with
  it while the already-built container images ship anyway.
* ``platform.yml`` (infrastructure) and ``backend.yml`` (application) are separate
  workflows. Nothing orders them, so backend code can reach production first.

All three happened at once on 2026-08-01: release 1.12.0's CloudFormation deploy rolled
back on an unrelated DynamoDB limit, the backend and frontend shipped regardless, and the
agent store — whose navigation gate had just opened to everyone in that same release —
returned a 500 to every user until the infrastructure was repaired two releases later. An
empty shelf would have been a bad day; an error page for the feature's GA was a worse one.

**The match is deliberately narrow.** Two failure modes are worth naming:

* Swallowing every ``ValidationException`` would hide real query bugs — a malformed key
  condition, a reserved word used unescaped, a bad ``ExclusiveStartKey`` — behind a
  permanently empty result set that nobody would ever debug.
* Swallowing every ``ClientError`` would turn throttling (``ProvisionedThroughputExceeded``)
  into "there is nothing here", which is a *lie about the data* rather than a degradation.

So both the error code and the message shape have to line up before this returns True.

**Two error codes, because the two DynamoDB implementations disagree.** Real DynamoDB
raises ``ValidationException`` ("The table does not have the specified index: X"); moto,
which every test in this repo runs against, raises ``ResourceNotFoundException``
("Invalid index: X for table: Y"). Matching only the production spelling would leave the
degradation path untestable, and matching only the code would swallow the genuinely
distinct "table does not exist" ``ResourceNotFoundException`` — hence the message check,
which is what actually distinguishes them.

⚠️ **For reads only.** A write path or an admin mutation that cannot find its index should
fail loudly: pretending a write succeeded is unrecoverable in a way that pretending a shelf
is empty is not.
"""

import logging
from typing import Any

__all__ = ["is_missing_index_error", "log_missing_index"]

logger = logging.getLogger(__name__)

# Only these two can mean "the index isn't there". Anything else — throttling, access
# denied, a conditional check — is a real failure and must propagate.
_MISSING_INDEX_CODES = ("ValidationException", "ResourceNotFoundException")

# Lowercased fragments of the two known messages. The message is what separates a missing
# *index* from a missing *table* (which shares ``ResourceNotFoundException`` but reads
# "Requested resource not found") and from every other ``ValidationException``.
_MISSING_INDEX_MARKERS = (
    "does not have the specified index",  # real DynamoDB
    "invalid index",  # moto
)


def is_missing_index_error(error: Any) -> bool:
    """True when ``error`` is a botocore ``ClientError`` meaning "that index isn't there".

    Takes ``Any`` rather than ``ClientError`` so callers need not import botocore just to
    type the parameter; a non-``ClientError`` simply has no ``response`` dict and returns
    False.
    """
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False

    err = response.get("Error") or {}
    if err.get("Code") not in _MISSING_INDEX_CODES:
        return False

    message = str(err.get("Message", "")).lower()
    return any(marker in message for marker in _MISSING_INDEX_MARKERS)


def log_missing_index(index_name: str, surface: str) -> None:
    """Record a degraded read at WARNING, naming the index so it is actionable.

    WARNING rather than ERROR: the request was served, and paging someone at 3am for a
    deploy that is still settling would train them to ignore the alert. WARNING rather
    than INFO because an index that is *permanently* missing means a surface is silently
    serving nothing, and that has to be visible in the logs without knowing to look.
    """
    logger.warning(
        f"⚠️ DynamoDB index '{index_name}' does not exist — serving an empty result for "
        f"{surface}. Expected transiently while a GSI is CREATING or a deploy is "
        f"incomplete; if this persists, the index is missing and {surface} is showing "
        f"nothing."
    )
