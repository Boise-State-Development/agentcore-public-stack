"""A memory space as a `.zip` (§9's "own your data" export), shared by the space and project routes.

The archive mirrors the space: ``MEMORY.md``, every entry with its frontmatter
under ``entries/<type>/``, a ``metadata.json`` for what the markdown doesn't
carry and, for an item-format space, ``provenance.json`` (Shared Projects 2.7):
who added, changed, proposed, approved, restored or moved each item, keyed by
the anchor the file carries, so an export stays attributable after it leaves
the platform.
"""

from __future__ import annotations

import json
import re
import zipfile
from datetime import datetime, timezone
from tempfile import SpooledTemporaryFile
from typing import Dict, Iterator, List

from apis.shared.memory.format import MemoryFormatError, parse_file
from apis.shared.memory.service import MemorySpaceExport

# Spill the zip to disk beyond this size so a large space never pins app-api
# memory (the entry count is bounded by the consolidation cap, so this is a
# ceiling, not the common case).
ZIP_SPOOL_MAX_BYTES = 8 * 1024 * 1024
_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

# Bumped only by a change a reader of an older export would misread.
PROVENANCE_FORMAT = "memory-provenance/1"


def safe_component(value: str, fallback: str) -> str:
    """Reduce a user string to one safe archive path segment.

    Collapses separators / ``..`` / other unsafe characters so a hostile slug
    or space name cannot escape its folder in the zip (zip-slip). Empty results
    fall back to ``fallback``.
    """
    cleaned = _UNSAFE_PATH_CHARS.sub("-", (value or "").strip()).strip("-._")
    return cleaned or fallback


def entry_path(slug: str, entry_type: str) -> str:
    """Where an entry lands inside the archive's root folder."""
    return f"entries/{safe_component(entry_type, 'fact')}/{safe_component(slug, 'entry')}.md"


def export_metadata_json(export: MemorySpaceExport) -> str:
    """Serialize the space-level state the markdown files don't carry (§9)."""
    space = export.space
    meta = {
        "spaceId": space.space_id,
        "name": space.name,
        "template": space.template,
        "createdAt": space.created_at,
        "updatedAt": space.updated_at,
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "owner": {"userId": space.owner_id, "email": space.owner_email},
        "members": [
            {
                "email": m.email,
                "permission": m.permission,
                "createdAt": m.created_at,
            }
            for m in export.members
        ],
        "entryCount": len(export.files),
    }
    if space.scope != "personal":
        meta["scope"] = space.scope
    if space.project_id:
        meta["projectId"] = space.project_id
    return json.dumps(meta, indent=2, ensure_ascii=False)


def provenance_json(export: MemorySpaceExport) -> str:
    """Each file's items, in file order, with where each came from (Shared Projects 2.7).

    People are emails, as provenance stores them. An item no save has touched
    since item history began (2.5a-2) has its anchor and nothing else, which
    reads the way the Memory page says it: "added before item history was kept".
    """
    files: List[Dict[str, object]] = []
    for ref, body in export.files:
        recorded = export.provenance.get(ref.slug, {})
        try:
            anchors = [i.anchor for i in parse_file(body.decode("utf-8")).items if i.anchor]
        except (MemoryFormatError, UnicodeDecodeError):
            anchors = sorted(recorded)
        items = []
        for anchor in anchors:
            entry: Dict[str, object] = {"anchor": anchor}
            if anchor in recorded:
                entry.update(recorded[anchor].model_dump(by_alias=True, exclude_none=True, exclude_defaults=True))
            items.append(entry)
        files.append(
            {
                "slug": ref.slug,
                "path": entry_path(ref.slug, ref.entry_type),
                "version": ref.version,
                "updatedAt": ref.updated,
                "pinned": list(ref.pinned),
                "items": items,
            }
        )
    space = export.space
    doc: Dict[str, object] = {
        "format": PROVENANCE_FORMAT,
        "spaceId": space.space_id,
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }
    if space.scope != "personal":
        doc["scope"] = space.scope
    if space.project_id:
        doc["projectId"] = space.project_id
    return json.dumps(doc, indent=2, ensure_ascii=False)


def build_export_zip(root: str, export: MemorySpaceExport) -> SpooledTemporaryFile:
    """Write the space's corpus into a spooled zip mirroring the S3 layout."""
    spool: SpooledTemporaryFile = SpooledTemporaryFile(max_size=ZIP_SPOOL_MAX_BYTES, mode="w+b")
    with zipfile.ZipFile(spool, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{root}/MEMORY.md", export.index_text)
        for ref, body in export.files:
            zf.writestr(f"{root}/{entry_path(ref.slug, ref.entry_type)}", body)
        zf.writestr(f"{root}/metadata.json", export_metadata_json(export))
        if export.space.file_format == "canonical":
            zf.writestr(f"{root}/provenance.json", provenance_json(export))
    spool.seek(0)
    return spool


def stream_and_close(spool: SpooledTemporaryFile) -> Iterator[bytes]:
    """Yield the spooled zip in chunks, closing (and unlinking) it when done."""
    try:
        while True:
            chunk = spool.read(65536)
            if not chunk:
                break
            yield chunk
    finally:
        spool.close()
