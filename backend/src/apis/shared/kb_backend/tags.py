"""The managed knowledge base tag contract, in one place.

Requirement 20.11. Tags are not housekeeping here: a tag-filtered
``ListKnowledgeBases`` is how the reconciler tells this platform's knowledge bases
from everything else in the account, and how teardown scopes itself. An untagged —
or mistagged — knowledge base is invisible to both, which means it is never
reclaimed and never deleted, and it keeps billing at $5.00/GB-month with no
CloudFormation console to notice it in.

Why this module exists
----------------------
It did not, and the tags drifted three ways:

* ``provisioning.build_tags`` wrote keys ``prefix``/``env`` with values from
  ``PROJECT_PREFIX``/``ENVIRONMENT`` — neither of which the provisioning Lambda is
  given, so every knowledge base would have been tagged with the hardcoded
  defaults regardless of project or environment.
* ``tombstones.project_tag_filter`` was a hand-written *mirror* of that function,
  documented as such. A mirror is a second implementation, and the only thing
  keeping two implementations equal is that nobody has edited one of them yet.
* ``kb-migration-construct.ts`` declared a different set of key names entirely
  (``ManagedKbPrefix``, …) and exported them plus the correct values as env vars
  that **nothing read**.
* ``scripts/teardown/managed-kb.sh`` read a third pair of variables
  (``CDK_PROJECT_PREFIX``/``CDK_ENVIRONMENT``) and matched on ``prefix``/``env``.

Writer and reconciler agreed by luck — both used the same wrong defaults — so the
symptom was not a crash but a teardown that found nothing and reported success.

So: the keys live here as constants, the value resolution lives here as one
function, and every consumer in every language reads *these* names.
``tests/shared/test_kb_tag_contract.py`` parses the TypeScript and the shell script
and fails if they disagree, because agreement between three languages is not
something a type checker can hold.

Why the keys are namespaced
---------------------------
``ManagedKbPrefix`` rather than ``prefix``, and ``ManagedKbEnvironment`` rather
than ``env``. Generic keys collide: many accounts carry an organisation-wide
cost-allocation tag literally called ``env``, and if something else writes it our
filter compares against a value we did not set. The failure mode is a teardown
that skips a knowledge base it owns — the leak this whole contract exists to
prevent.

Feature: managed-kb-migration
Requirements: 20.11, 20.12, 20.8, 14.1
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger(__name__)

# ── Tag keys ─────────────────────────────────────────────────────────────────
#
# Mirrored by `MANAGED_KB_TAG_KEYS` in
# `infrastructure/lib/constructs/managed-kb/kb-migration-construct.ts`, and that
# mirroring is asserted by a test rather than trusted.
TAG_KEY_PREFIX = "ManagedKbPrefix"
TAG_KEY_ENVIRONMENT = "ManagedKbEnvironment"
TAG_KEY_APP_KB_ID = "ManagedKbAppKbId"
TAG_KEY_OWNER_USER_ID = "ManagedKbOwnerUserId"

#: The two keys that scope a destructive pass. Both are required to match: a
#: knowledge base carrying our project prefix but another environment's tag
#: belongs to that environment, and its name looks exactly like ours.
SCOPE_KEYS = (TAG_KEY_PREFIX, TAG_KEY_ENVIRONMENT)

# ── Environment variables carrying the values ────────────────────────────────
#
# Set by the CDK construct that owns the provisioning Lambdas, which is the only
# surface that calls `provision_managed_kb`. Named after the tag rather than after
# the project so it is obvious at the call site that changing one changes what
# gets written into AWS.
ENV_TAG_VALUE_PREFIX = "MANAGED_KB_TAG_VALUE_PREFIX"
ENV_TAG_VALUE_ENVIRONMENT = "MANAGED_KB_TAG_VALUE_ENVIRONMENT"

#: Fallbacks, in order, for a local run or a service that predates the vars above.
#: Deliberately the *same* chain for writing and for filtering — an asymmetric
#: fallback is how a writer and a reader disagree while both look correct.
FALLBACK_PREFIX_VARS = ("PROJECT_PREFIX", "CDK_PROJECT_PREFIX")
FALLBACK_ENVIRONMENT_VARS = ("ENVIRONMENT", "CDK_ENVIRONMENT")

#: Last resort. Kept so a local run works without configuration, and logged
#: loudly because two deployments that both fall back to it will claim each
#: other's knowledge bases — they would agree with themselves and delete each
#: other's corpora.
DEFAULT_PREFIX = "agentcore"
DEFAULT_ENVIRONMENT = "dev"


def _resolve(explicit: Optional[str], primary: str, fallbacks: tuple, default: str, what: str) -> str:
    if explicit:
        return explicit
    value = os.environ.get(primary)
    if value:
        return value
    for name in fallbacks:
        value = os.environ.get(name)
        if value:
            logger.info(
                f"managed KB {what} tag resolved from {name}; {primary} is not set. "
                f"This is expected for a local run and unexpected in a deployment."
            )
            return value
    logger.warning(
        f"managed KB {what} tag falling back to {default!r}: none of {primary} or "
        f"{fallbacks} is set. Two deployments that both reach this default share a "
        f"tag scope and will each treat the other's knowledge bases as their own."
    )
    return default


def tag_prefix(explicit: Optional[str] = None) -> str:
    """The project-prefix tag value."""
    return _resolve(explicit, ENV_TAG_VALUE_PREFIX, FALLBACK_PREFIX_VARS, DEFAULT_PREFIX, "prefix")


def tag_environment(explicit: Optional[str] = None) -> str:
    """The environment tag value."""
    return _resolve(
        explicit,
        ENV_TAG_VALUE_ENVIRONMENT,
        FALLBACK_ENVIRONMENT_VARS,
        DEFAULT_ENVIRONMENT,
        "environment",
    )


def build_tags(
    app_kb_id: str,
    owner_user_id: str,
    project_prefix: Optional[str] = None,
    environment: Optional[str] = None,
) -> Dict[str, str]:
    """The complete tag set written at ``CreateKnowledgeBase`` time.

    The owner tag must be opaque (Requirement 20.12). An email address here would
    put PII in a field readable by anyone holding
    ``bedrock:ListKnowledgeBases``, and unlike a database column a tag cannot be
    scrubbed retroactively from the audit trail it has already entered. An
    address-shaped value is therefore rejected rather than trimmed: silently
    dropping it would hide the caller's mistake.
    """
    if "@" in owner_user_id:
        raise ValueError(
            "ownerUserId tag must be an opaque identifier, never an email address "
            "or other personally identifying value (Requirement 20.12)"
        )
    return {
        TAG_KEY_PREFIX: tag_prefix(project_prefix),
        TAG_KEY_ENVIRONMENT: tag_environment(environment),
        TAG_KEY_APP_KB_ID: app_kb_id,
        TAG_KEY_OWNER_USER_ID: owner_user_id,
    }


def project_tag_filter(
    project_prefix: Optional[str] = None,
    environment: Optional[str] = None,
) -> Dict[str, str]:
    """The subset of tags a knowledge base must carry to be considered ours.

    Derived from the same resolution :func:`build_tags` uses, not mirrored from
    it. That is the entire point of this module: a reader that re-derives what the
    writer wrote is a reader that can be wrong on its own.
    """
    return {
        TAG_KEY_PREFIX: tag_prefix(project_prefix),
        TAG_KEY_ENVIRONMENT: tag_environment(environment),
    }


def matches_project(
    tags: Optional[Mapping[str, Any]],
    project_prefix: Optional[str] = None,
    environment: Optional[str] = None,
) -> bool:
    """Whether these tags identify a knowledge base this deployment owns.

    ``False`` for absent or unreadable tags. Unknown ownership is not ownership,
    and refusing to act on a resource we cannot attribute is the only safe
    direction for a pass that deletes things.
    """
    if not tags:
        return False
    expected = project_tag_filter(project_prefix, environment)
    return all(str(tags.get(key, "")) == value for key, value in expected.items())


__all__ = [
    "DEFAULT_ENVIRONMENT",
    "DEFAULT_PREFIX",
    "ENV_TAG_VALUE_ENVIRONMENT",
    "ENV_TAG_VALUE_PREFIX",
    "FALLBACK_ENVIRONMENT_VARS",
    "FALLBACK_PREFIX_VARS",
    "SCOPE_KEYS",
    "TAG_KEY_APP_KB_ID",
    "TAG_KEY_ENVIRONMENT",
    "TAG_KEY_OWNER_USER_ID",
    "TAG_KEY_PREFIX",
    "build_tags",
    "matches_project",
    "project_tag_filter",
    "tag_environment",
    "tag_prefix",
]
