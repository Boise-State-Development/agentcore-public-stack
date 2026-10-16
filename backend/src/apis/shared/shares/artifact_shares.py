"""Artifact share rows and a project's Outputs pointer, written from either side.

An artifact share is two rows in the artifacts table with the same attributes:
the owner's (``USER#{owner}`` / ``SHARE#{artifact}#V#{version}#{share}``), which
lists it, and the lookup (``SHARE#{share}`` / ``META``), which a viewer resolves
by id. A share with "Project members" (Shared Projects 3.3) is also listed by
the project's ``OUTPUT#{artifactId}`` pointer, rebuilt from the artifact's
remaining project shares whenever one changes.

app-api's ``ArtifactShareService`` writes them for a person, and the
scheduled-runs worker for a schedule that publishes its runs' artifacts (3.2d),
which cannot import app-api. One copy of the keys and writes keeps the two from
drifting, as ``writer.py`` does for conversation shares.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from boto3.dynamodb.conditions import Key

from apis.shared.projects.models import ProjectOutput
from apis.shared.projects.repository import ProjectRepository

logger = logging.getLogger(__name__)

SHARE_LOOKUP_SK = "META"


def owner_share_sk(artifact_id: str, version: int, share_id: str) -> str:
    """Owner-row sort key. ``V#{version:05d}`` matches the artifact version rows."""
    return f"SHARE#{artifact_id}#V#{version:05d}#{share_id}"


def owner_share_prefix(artifact_id: str) -> str:
    return f"SHARE#{artifact_id}#V#"


def share_lookup_key(share_id: str) -> Dict[str, str]:
    return {"PK": f"SHARE#{share_id}", "SK": SHARE_LOOKUP_SK}


def write_share_rows(table: Any, attrs: Dict[str, Any]) -> None:
    """Put the owner row and the lookup row in one transaction. Raises the client's error.

    Atomic, because half a share is either unreachable or unlistable. Plain Puts
    only: a ConditionCheck would need ``dynamodb:ConditionCheckItem`` on every
    writer's role.
    """
    owner_row = {
        **attrs,
        "PK": f"USER#{attrs['owner_id']}",
        "SK": owner_share_sk(str(attrs["artifact_id"]), int(attrs["version"]), str(attrs["share_id"])),
    }
    lookup_row = {**attrs, **share_lookup_key(str(attrs["share_id"]))}
    table.meta.client.transact_write_items(
        TransactItems=[
            {"Put": {"TableName": table.name, "Item": owner_row}},
            {"Put": {"TableName": table.name, "Item": lookup_row}},
        ]
    )


def delete_share_rows(table: Any, share: Dict[str, Any]) -> None:
    """Delete one share's owner and lookup rows together. Raises the client's error.

    Fan-out rows for a ``specific`` share are the caller's to drop first; a
    project share has none.
    """
    share_id = str(share.get("share_id", ""))
    owner_sk = owner_share_sk(str(share.get("artifact_id", "")), int(share.get("version", 0)), share_id)
    table.meta.client.transact_write_items(
        TransactItems=[
            {"Delete": {"TableName": table.name, "Key": {"PK": f"USER#{share.get('owner_id', '')}", "SK": owner_sk}}},
            {"Delete": {"TableName": table.name, "Key": share_lookup_key(share_id)}},
        ]
    )


def shares_for_artifact(table: Any, owner_id: str, artifact_id: str) -> List[Dict[str, Any]]:
    """Every share ``owner_id`` holds for one artifact, keys stripped. Raises the client's error."""
    items: List[Dict[str, Any]] = []
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"USER#{owner_id}") & Key("SK").begins_with(owner_share_prefix(artifact_id)),
    }
    while True:
        resp = table.query(**kwargs)
        items.extend(resp.get("Items", []))
        last = resp.get("LastEvaluatedKey")
        if not last:
            break
        kwargs["ExclusiveStartKey"] = last
    return [_strip(item) for item in items]


def _strip(item: Dict[str, Any]) -> Dict[str, Any]:
    stripped = {k: v for k, v in item.items() if k not in ("PK", "SK")}
    if "version" in stripped:
        stripped["version"] = int(stripped["version"])
    return stripped


def output_pointer(share: Dict[str, Any]) -> ProjectOutput:
    """The ``OUTPUT#`` pointer that lists one project share."""
    return ProjectOutput(
        project_id=str(share["project_id"]),
        artifact_id=str(share["artifact_id"]),
        share_id=str(share["share_id"]),
        version=int(share.get("version", 0)),
        title=str(share.get("title", "")),
        content_type=str(share.get("content_type", "")),
        owner_id=str(share.get("owner_id", "")),
        owner_email=str(share.get("owner_email", "")),
        shared_at=str(share.get("created_at", "")),
        schedule_id=share.get("schedule_id") or None,
    )


def sync_project_output(
    table: Any,
    owner_id: str,
    artifact_id: str,
    project_id: str,
    repository: Optional[ProjectRepository] = None,
) -> None:
    """Point ``OUTPUT#{artifact_id}`` at the artifact's newest share with the project, or remove it.

    Raises, so each caller decides whether a failed rebuild matters to it.
    """
    repo = repository or ProjectRepository()
    remaining = [
        s for s in shares_for_artifact(table, owner_id, artifact_id)
        if s.get("access_level") == "project" and s.get("project_id") == project_id
    ]
    if remaining:
        repo.put_output(output_pointer(max(remaining, key=lambda s: str(s.get("created_at", "")))))
    else:
        repo.delete_output(project_id, artifact_id)
