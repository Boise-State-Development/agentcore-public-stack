"""Backfill: bring skill rows up to the agentskills.io bundle layout (Skills v2).

Skills authored before Skills v2 PR-2 predate two things:

1. **The ``SKILL.md`` write-through projection.** Their S3 prefix holds only
   resource bytes. (Even with it the prefix is storage, not a conformant
   bundle: its directory is the ``skill_id``, which can differ from the
   projected ``name``. An export is the conformant artifact.)
2. **The standard bundle layout.** v1 stored resources content-addressed
   (``skills/{id}/{sha256}``) with no ``kind``; v2 stores them at
   ``skills/{id}/{references|scripts|assets}/{filename}``.

This script fixes both, per skill: it copies each legacy content-hash object to
its standard path, rewrites the row's ``resources`` manifest to point there
(adding ``kind``), and writes the ``SKILL.md`` projection generated from the
row. The DynamoDB row stays the source of truth throughout — nothing here
invents metadata.

It does NOT touch ``instructions``, ``description`` or any other authored
field. A skill whose prose is stale is a content problem, not a layout one.

SAFETY
------
* **Dry-run by default.** Pass ``--apply`` to actually write.
* **Idempotent + re-runnable.** A skill already in the standard layout has its
  projection rewritten (cheap, byte-identical) and its objects left alone.
  Legacy objects are *copied*, never moved, and only deleted with
  ``--delete-legacy`` once the copy is verified present.
* **Throttled.** ``--sleep`` between skills (default 50ms).
* **Scoped.** ``--skill`` limits the run to one skill id.

PROJECTIONS ONLY
----------------
``--projections-only`` skips the resource layout and regenerates every skill's
``SKILL.md`` from its row, writing only the ones whose bytes differ from what
is stored (so a second run writes nothing). Use it after a change to
``generate_skill_md``. Every run, in either mode, also reports, by skill id
only and without changing anything:

* skill ids whose ``slugify_skill_name`` is shared with another skill's
  (``pdf_tools`` and ``pdf-tools`` both activate as ``pdf-tools``);
* rows whose regenerated frontmatter fails strict agentskills.io validation
  (a legacy empty description, metadata over its limits).

Run against dev first, then prod::

    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/backfill_skill_bundles.py \\
        --table dev-boisestateai-v2-app-roles \\
        --bucket dev-boisestateai-v2-skill-resources            # dry-run
    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/backfill_skill_bundles.py \\
        --table dev-boisestateai-v2-app-roles \\
        --bucket dev-boisestateai-v2-skill-resources --apply
    AWS_PROFILE=dev-ai backend/.venv/bin/python backend/scripts/backfill_skill_bundles.py \\
        --table dev-boisestateai-v2-app-roles \\
        --bucket dev-boisestateai-v2-skill-resources --projections-only   # dry-run
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

# The generator and the layout rules are shared with the live write path, so a
# backfilled bundle is byte-identical to one the app would write today.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from apis.shared.skills.bundle import (  # noqa: E402
    generate_skill_md,
    skill_md_frontmatter,
    slugify_skill_name,
)
from apis.shared.skills.skill_md import validate_frontmatter  # noqa: E402
from apis.shared.skills.resource_store import (  # noqa: E402
    resource_key,
    skill_md_key,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill_skill_bundles")


def is_legacy_key(s3_key: str, skill_id: str) -> bool:
    """True if a manifest entry still uses the v1 content-addressed key.

    v1 wrote ``skills/{id}/{sha256}`` — a flat key with no bundle subdirectory.
    v2 always writes ``skills/{id}/{references|scripts|assets}/{filename}``, so
    the presence of a subdirectory is the discriminator.
    """
    prefix = f"skills/{skill_id}/"
    if not s3_key.startswith(prefix):
        # Unrecognized shape — treat as legacy so it gets normalized.
        return True
    return "/" not in s3_key[len(prefix) :]


def scan_skills(table, skill_id: Optional[str]) -> List[Dict[str, Any]]:
    """Return the skill rows to process (all, or just the one named)."""
    if skill_id:
        response = table.get_item(Key={"PK": f"SKILL#{skill_id}", "SK": "METADATA"})
        item = response.get("Item")
        return [item] if item else []

    filter_expr = "begins_with(PK, :pk_prefix) AND SK = :sk"
    expr_values = {":pk_prefix": "SKILL#", ":sk": "METADATA"}

    response = table.scan(
        FilterExpression=filter_expr, ExpressionAttributeValues=expr_values
    )
    items = list(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(
            FilterExpression=filter_expr,
            ExpressionAttributeValues=expr_values,
            ExclusiveStartKey=response["LastEvaluatedKey"],
        )
        items.extend(response.get("Items", []))
    return items


def object_exists(s3, bucket: str, key: str) -> bool:
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return False
        raise


def migrate_resources(
    s3,
    bucket: str,
    skill_id: str,
    resources: List[Dict[str, Any]],
    *,
    apply: bool,
    delete_legacy: bool,
) -> Tuple[List[Dict[str, Any]], int]:
    """Copy legacy objects into the standard layout and rewrite the manifest.

    Returns the (possibly rewritten) manifest and the number of entries moved.
    """
    migrated: List[Dict[str, Any]] = []
    moved = 0

    for ref in resources:
        entry = dict(ref)
        filename = entry.get("filename") or ""
        old_key = entry.get("s3Key") or ""
        # v1 rows carry no `kind`; everything it could store was a reference doc.
        kind = entry.get("kind") or "reference"
        entry["kind"] = kind

        if not filename or not is_legacy_key(old_key, skill_id):
            migrated.append(entry)
            continue

        new_key = resource_key(skill_id, kind, filename)
        entry["s3Key"] = new_key
        moved += 1

        if not apply:
            logger.info("  [dry-run] copy %s -> %s", old_key, new_key)
            migrated.append(entry)
            continue

        if object_exists(s3, bucket, new_key):
            logger.info("  %s already present, skipping copy", new_key)
        elif not object_exists(s3, bucket, old_key):
            # The manifest points at bytes that are gone. Rewriting the key
            # would be a lie, so leave the entry untouched and shout.
            logger.warning(
                "  MISSING legacy object %s for %s/%s — manifest left as-is",
                old_key,
                skill_id,
                filename,
            )
            entry["s3Key"] = old_key
            moved -= 1
            migrated.append(entry)
            continue
        else:
            s3.copy_object(
                Bucket=bucket,
                CopySource={"Bucket": bucket, "Key": old_key},
                Key=new_key,
                ContentType=entry.get("contentType") or "application/octet-stream",
                MetadataDirective="REPLACE",
                ServerSideEncryption="AES256",
            )
            logger.info("  copied %s -> %s", old_key, new_key)

        if delete_legacy and object_exists(s3, bucket, new_key):
            s3.delete_object(Bucket=bucket, Key=old_key)
            logger.info("  deleted legacy %s", old_key)

        migrated.append(entry)

    return migrated, moved


def _projection(row: Dict[str, Any]) -> str:
    return generate_skill_md(
        skill_id=row["skillId"],
        description=row.get("description") or "",
        instructions=row.get("instructions") or "",
        allowed_tools=[str(t) for t in (row.get("allowedTools") or [])],
        skill_metadata=dict(row.get("skillMetadata") or {}),
    )


def read_object(s3, bucket: str, key: str) -> Optional[bytes]:
    """The object's bytes, or None when it does not exist."""
    try:
        return s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
            return None
        raise


PROJECTION_UNCHANGED = "unchanged"
PROJECTION_WRITTEN = "written"
PROJECTION_WOULD_WRITE = "would_write"


def write_projection(
    s3, bucket: str, row: Dict[str, Any], *, apply: bool
) -> str:
    """Write the row's ``SKILL.md`` projection into the bundle prefix if it changed.

    Returns ``"unchanged"`` when the stored object already has these bytes,
    ``"would_write"`` on a dry run that would change it, and ``"written"``.
    """
    skill_id = row["skillId"]
    content = _projection(row).encode("utf-8")
    key = skill_md_key(skill_id)

    if read_object(s3, bucket, key) == content:
        logger.info("  %s unchanged", key)
        return PROJECTION_UNCHANGED

    if not apply:
        logger.info("  [dry-run] write %s (%d bytes)", key, len(content))
        return PROJECTION_WOULD_WRITE

    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=content,
        ContentType="text/markdown",
        ServerSideEncryption="AES256",
    )
    logger.info("  wrote %s", key)
    return PROJECTION_WRITTEN


def shared_slugs(rows: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Each activation name two or more skill ids slugify to, with those ids (sorted)."""
    by_slug: Dict[str, List[str]] = defaultdict(list)
    for row in rows:
        skill_id = row.get("skillId")
        if skill_id:
            by_slug[slugify_skill_name(skill_id)].append(skill_id)
    return {
        slug: sorted(ids) for slug, ids in sorted(by_slug.items()) if len(ids) > 1
    }


def invalid_projections(rows: List[Dict[str, Any]]) -> List[str]:
    """Skill ids whose regenerated frontmatter fails strict validation (sorted)."""
    invalid = []
    for row in rows:
        skill_id = row.get("skillId")
        if not skill_id:
            continue
        frontmatter = skill_md_frontmatter(
            skill_id=skill_id,
            description=row.get("description") or "",
            allowed_tools=[str(t) for t in (row.get("allowedTools") or [])],
            skill_metadata=dict(row.get("skillMetadata") or {}),
        )
        if any(d.is_error for d in validate_frontmatter(frontmatter, strict=True)):
            invalid.append(skill_id)
    return sorted(invalid)


def report(rows: List[Dict[str, Any]]) -> Tuple[Dict[str, List[str]], List[str]]:
    """Log the shared-slug and invalid-projection findings; change nothing."""
    collisions = shared_slugs(rows)
    invalid = invalid_projections(rows)
    for slug, ids in collisions.items():
        logger.warning("REPORT shared activation name %s: %s", slug, ", ".join(ids))
    for skill_id in invalid:
        logger.warning("REPORT projection fails strict validation: %s", skill_id)
    return collisions, invalid


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", required=True, help="app-roles DynamoDB table")
    parser.add_argument("--bucket", required=True, help="skill-resources S3 bucket")
    parser.add_argument("--skill", help="Only process this skill id")
    parser.add_argument(
        "--apply", action="store_true", help="Actually write (default: dry-run)"
    )
    parser.add_argument(
        "--delete-legacy",
        action="store_true",
        help="Delete the old content-hash objects after a verified copy",
    )
    parser.add_argument(
        "--projections-only",
        action="store_true",
        help="Only regenerate SKILL.md projections; leave the resource layout alone",
    )
    parser.add_argument("--sleep", type=float, default=0.05)
    args = parser.parse_args()

    dynamodb = boto3.resource("dynamodb")
    table = dynamodb.Table(args.table)
    s3 = boto3.client("s3")

    rows = scan_skills(table, args.skill)
    if not rows:
        logger.warning("No skills found (table=%s skill=%s)", args.table, args.skill)
        return 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info("%s: %d skill(s) to inspect", mode, len(rows))

    projections: Dict[str, int] = defaultdict(int)
    moves = 0

    for row in rows:
        skill_id = row.get("skillId")
        if not skill_id:
            logger.warning("Row without skillId, skipping: PK=%s", row.get("PK"))
            continue

        logger.info("skill %s", skill_id)
        if args.projections_only:
            projections[write_projection(s3, args.bucket, row, apply=args.apply)] += 1
            time.sleep(args.sleep)
            continue

        resources = [dict(r) for r in (row.get("resources") or [])]

        migrated, moved = migrate_resources(
            s3,
            args.bucket,
            skill_id,
            resources,
            apply=args.apply,
            delete_legacy=args.delete_legacy,
        )
        moves += moved

        if moved and args.apply:
            table.update_item(
                Key={"PK": f"SKILL#{skill_id}", "SK": "METADATA"},
                UpdateExpression="SET #r = :r",
                ExpressionAttributeNames={"#r": "resources"},
                ExpressionAttributeValues={":r": migrated},
            )
            logger.info("  manifest rewritten (%d entr(y|ies))", moved)

        projections[write_projection(s3, args.bucket, row, apply=args.apply)] += 1

        time.sleep(args.sleep)

    collisions, invalid = report([r for r in rows if r.get("skillId")])
    summary = (
        f"{mode} complete: {sum(projections.values())} skill(s); projections "
        f"{projections[PROJECTION_WRITTEN]} written, "
        f"{projections[PROJECTION_WOULD_WRITE]} would change, "
        f"{projections[PROJECTION_UNCHANGED]} unchanged; "
        f"{moves} resource(s) relocated; "
        f"{len(collisions)} shared activation name(s); "
        f"{len(invalid)} projection(s) failing strict validation"
    )
    logger.info(summary)
    print(summary)
    if not args.apply:
        logger.info("Re-run with --apply to write.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
