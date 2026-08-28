"""IAM-enforced retrieval on a shared managed knowledge base.

Requirements 25.6, 25.7. Resource policies are MANAGED-only and are the only
mechanism in this design that offers *infrastructure* isolation rather than
filter-level isolation. A policy attached to a knowledge base ARN restricts
``bedrock:Retrieve`` and ``bedrock:GetDocumentContent`` to the principals it
names, which matters because the platform's own identity grant
(``grantManagedKbRetrieval``) is written against ``knowledge-base/*`` — every
knowledge base in the account, present and future. Without a policy, any
principal in the account holding a similar grant can read a shared corpus.

What this is NOT
----------------
It is **not** per-user authorization, and the temptation to describe it that way
is the reason this paragraph exists. Every user of this platform retrieves through
the same infrastructure identity — the AgentCore runtime role — so no resource
policy can distinguish user A from user B. Per-user authorization is, and remains,
the application's job (Requirement 25.3, ``apis.shared.assistants.kb_access``).
What a policy buys is a narrower blast radius for a corpus that belongs to more
than one person: the set of *infrastructure* identities able to reach it shrinks
from "anything in the account with a wildcard grant" to an explicit list.

Applied only where a knowledge base is shared beyond its owner, because a policy
on a single-owner knowledge base would restrict nothing that the assistant's own
access check does not already restrict, while adding a control-plane call and a
piece of state to keep in step.

Why staleness is state rather than an event
-------------------------------------------
A policy attaches to the AWS knowledge base ARN, so any cycle producing a new
``awsKbId`` silently drops sharing — the call succeeds, the policy is simply on a
resource nobody reads any more. The obvious fix is to re-apply from wherever a new
identifier is created. That fix is only as good as the completeness of the list of
such places, and this phase already has two (fresh provisioning, resumed
provisioning) with dormancy/rehydration a known future third.

So the record stores the identifier the policy was last applied *to*, and
:func:`policy_is_stale` compares it against the current one. A path that produces
a new ``awsKbId`` and forgets to re-apply is then not a silent regression: the
next :func:`ensure_retrieve_policy` sees a mismatch and repairs it. The invariant
is checked by comparing two values, which no new code path can bypass by omission
(Requirement 24.12).

Import weight
-------------
``boto3`` and ``json`` usage stays inside functions where practical; the module
imports stdlib only, per this package's Lambda-image constraint.

Feature: managed-kb-migration
Requirements: 25.6, 25.7
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

#: The actions a shared knowledge base's readers need. ``GetDocumentContent`` is
#: included because a retrieval that returns a citation the caller cannot then
#: fetch is a half-share — the evaluation names both as what resource policies
#: cover.
RETRIEVE_ACTIONS: Tuple[str, ...] = ("bedrock:Retrieve", "bedrock:GetDocumentContent")

#: Statement id. Fixed so a re-application replaces the platform's own statement
#: rather than accumulating near-duplicates.
POLICY_SID = "PlatformSharedRetrieve"

POLICY_VERSION = "2012-10-17"

#: Comma-separated ARNs of the infrastructure identities that retrieve on users'
#: behalf — the AgentCore runtime role, and the App API task role for test-chat.
#: Read at call time, never captured in a default argument.
PRINCIPALS_ENV = "MANAGED_KB_RETRIEVAL_PRINCIPAL_ARNS"

#: Record attributes tracking what was applied where.
POLICY_KB_ID_ATTR = "policyAwsKbId"
POLICY_REVISION_ATTR = "policyRevisionId"


class ResourcePolicyError(RuntimeError):
    """A resource policy could not be applied or removed."""


def _region() -> str:
    return os.environ.get("AWS_REGION", "us-west-2")


def bedrock_agent_client():
    """Control-plane client. ``PutResourcePolicy`` lives on ``bedrock-agent``.

    Verified against the pinned botocore service model: ``PutResourcePolicy``
    takes ``resourceArn`` and ``policy`` (both required) plus an optional
    ``expectedRevisionId``, and returns ``resourceArn`` and ``revisionId``.
    """
    import boto3

    return boto3.client("bedrock-agent", region_name=_region())


def knowledge_base_arn(
    aws_kb_id: str,
    region: Optional[str] = None,
    account_id: Optional[str] = None,
) -> str:
    """The ARN a policy attaches to.

    Refuses to guess the account. A wrong account in an ARN does not fail loudly —
    ``PutResourcePolicy`` would target a resource this caller cannot see, and the
    error it raises names a resource the operator did not know existed. Better to
    say what is missing.
    """
    resolved_account = account_id or os.environ.get("AWS_ACCOUNT_ID")
    if not resolved_account:
        raise ResourcePolicyError(
            f"cannot build a knowledge base ARN for {aws_kb_id} without an account "
            f"id: pass account_id or set AWS_ACCOUNT_ID"
        )
    return f"arn:aws:bedrock:{region or _region()}:{resolved_account}:knowledge-base/{aws_kb_id}"


def retrieval_principals(explicit: Optional[Iterable[str]] = None) -> Tuple[str, ...]:
    """The infrastructure identities allowed to retrieve, in a stable order.

    Sorted and de-duplicated so the same configuration always produces the same
    policy document — otherwise every call looks like a change and nothing can be
    compared.
    """
    if explicit is not None:
        candidates: Sequence[str] = list(explicit)
    else:
        candidates = (os.environ.get(PRINCIPALS_ENV) or "").split(",")
    return tuple(sorted({arn.strip() for arn in candidates if arn and arn.strip()}))


def retrieve_policy_document(kb_arn: str, principals: Sequence[str]) -> Dict[str, Any]:
    """The policy granting exactly the shared-read actions to exactly ``principals``.

    No wildcard principal and no wildcard resource: a resource policy whose point
    is to narrow access is worse than no policy at all if it widens it instead.
    """
    if not principals:
        raise ResourcePolicyError(
            "refusing to write a resource policy with no principals: an empty "
            "principal list is not a narrower grant, it is an unparseable one"
        )
    return {
        "Version": POLICY_VERSION,
        "Statement": [
            {
                "Sid": POLICY_SID,
                "Effect": "Allow",
                "Principal": {"AWS": list(principals)},
                "Action": list(RETRIEVE_ACTIONS),
                "Resource": kb_arn,
            }
        ],
    }


def policy_is_stale(record: Optional[Mapping[str, Any]]) -> bool:
    """Whether the recorded policy target no longer matches the live ``awsKbId``.

    ``True`` when a knowledge base exists in AWS and either no policy target was
    ever recorded or the recorded one differs. ``False`` for a record with no
    ``awsKbId`` at all: nothing has been provisioned, so there is nothing to be
    stale against.
    """
    if not record:
        return False
    aws_kb_id = record.get("awsKbId")
    if not aws_kb_id:
        return False
    return record.get(POLICY_KB_ID_ATTR) != aws_kb_id


async def ensure_retrieve_policy(
    assistant_id: str,
    app_kb_id: str,
    *,
    shared: bool,
    record: Optional[Mapping[str, Any]] = None,
    principals: Optional[Iterable[str]] = None,
    client=None,
    region: Optional[str] = None,
    account_id: Optional[str] = None,
) -> Optional[str]:
    """Bring the knowledge base's resource policy in line with its sharing state.

    Returns the revision id of a policy that is now in place, or ``None`` when no
    policy is wanted or none could be applied.

    Four cases:

    * **Not shared, no policy recorded** — nothing to do.
    * **Not shared, policy recorded** — remove it, and forget the target. A
      knowledge base that stops being shared should stop carrying the statement
      that says it is.
    * **Shared, policy current** — nothing to do. This is the common path and it
      makes no AWS call, which is what allows callers to invoke this freely.
    * **Shared, policy missing or stale** — apply, then record the ``awsKbId`` it
      was applied to.

    ``shared`` is supplied by the caller rather than derived here: sharing is an
    application fact (visibility plus share records) that lives above this seam,
    and this package may not import the assistants package.
    """
    from apis.shared.kb_backend import records as r

    if record is None:
        import asyncio

        record = await asyncio.to_thread(r.get_kb_record, assistant_id, app_kb_id)

    if not record:
        return None

    aws_kb_id = record.get("awsKbId")
    recorded_target = record.get(POLICY_KB_ID_ATTR)

    if not shared:
        if recorded_target:
            await _remove(assistant_id, app_kb_id, recorded_target, client, region, account_id)
        return None

    if not aws_kb_id:
        # Shared, but nothing provisioned yet. Provisioning is lazy by design, so
        # this is ordinary, not an error: the next call after provisioning sees a
        # stale (unset) target and applies.
        return None

    if not policy_is_stale(record):
        return record.get(POLICY_REVISION_ATTR)

    resolved = retrieval_principals(principals)
    if not resolved:
        # Loud, and not repaired by guessing. A policy with no principals cannot
        # be written, and inventing one would either widen access or lock the
        # platform out of its own corpus.
        logger.error(
            f"knowledge base {app_kb_id} is shared but {PRINCIPALS_ENV} names no "
            f"principals; no resource policy applied (Requirement 25.6)"
        )
        return None

    arn = knowledge_base_arn(aws_kb_id, region, account_id)
    document = retrieve_policy_document(arn, resolved)
    api = client or bedrock_agent_client()

    try:
        response = api.put_resource_policy(resourceArn=arn, policy=json.dumps(document))
    except Exception as exc:
        raise ResourcePolicyError(
            f"failed to apply the retrieve policy for kb {app_kb_id} on {arn}: {exc}"
        ) from exc

    revision_id = response.get("revisionId")
    await _record(assistant_id, app_kb_id, aws_kb_id, revision_id)

    if recorded_target and recorded_target != aws_kb_id:
        logger.info(
            f"re-applied the retrieve policy for kb {app_kb_id}: it was attached "
            f"to {recorded_target}, which is no longer this knowledge base's id "
            f"(Requirement 25.7)"
        )
    return revision_id


async def _remove(
    assistant_id: str,
    app_kb_id: str,
    recorded_target: str,
    client,
    region: Optional[str],
    account_id: Optional[str],
) -> None:
    """Delete the policy and forget the target, tolerating an absent policy.

    A ``ResourceNotFoundException`` here means the policy or its knowledge base is
    already gone, which is the state being asked for. The record is cleared either
    way, so a knowledge base cannot be left claiming a policy that does not exist.
    """
    api = client or bedrock_agent_client()
    arn = knowledge_base_arn(recorded_target, region, account_id)
    try:
        api.delete_resource_policy(resourceArn=arn)
    except Exception as exc:
        if type(exc).__name__ not in ("ResourceNotFoundException", "ValidationException"):
            raise ResourcePolicyError(
                f"failed to remove the retrieve policy for kb {app_kb_id} on {arn}: {exc}"
            ) from exc
        logger.info(
            f"retrieve policy for kb {app_kb_id} was already absent on {arn}; "
            f"clearing the record anyway"
        )
    await _record(assistant_id, app_kb_id, None, None)


async def _record(
    assistant_id: str,
    app_kb_id: str,
    aws_kb_id: Optional[str],
    revision_id: Optional[str],
) -> None:
    import asyncio

    from apis.shared.kb_backend import records as r

    await asyncio.to_thread(
        r.set_resource_policy_state, assistant_id, app_kb_id, aws_kb_id, revision_id
    )


__all__ = [
    "POLICY_KB_ID_ATTR",
    "POLICY_REVISION_ATTR",
    "POLICY_SID",
    "PRINCIPALS_ENV",
    "RETRIEVE_ACTIONS",
    "ResourcePolicyError",
    "ensure_retrieve_policy",
    "knowledge_base_arn",
    "policy_is_stale",
    "retrieval_principals",
    "retrieve_policy_document",
]
