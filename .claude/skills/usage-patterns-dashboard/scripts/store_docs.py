"""Write the dashboard store documents for one refresh as local JSON files.

Each file is one document for an `ArtifactData` batch entry's `file_path`:

    <docs>/dash_meta.json       -> collection "dash",     doc_id "meta"
    <docs>/datasets_<id>.json   -> collection "datasets", doc_id "<id>"
    <docs>/files_index.json     -> collection "files",    doc_id "index.html"

`--assets` is a JSON object mapping each dataset id to the `/_blob/<id>` url its
upload returned. Prints the batch `writes` list to pass to `ArtifactData` (add
`if_version` to entries whose document already exists).

    python3 -I store_docs.py --manifest OUT/manifest.json --assets OUT/assets.json \
        --page assets/index.html --by "Jane Admin" --docs OUT/docs
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone

TITLE = "Conversation Cost Patterns"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--assets", required=True)
    ap.add_argument("--page", required=True)
    ap.add_argument("--by", required=True)
    ap.add_argument("--docs", required=True)
    args = ap.parse_args()
    os.makedirs(args.docs, exist_ok=True)

    with open(args.manifest) as fh:
        manifest = json.load(fh)
    with open(args.assets) as fh:
        assets = json.load(fh)
    missing = [m["id"] for m in manifest if m["id"] not in assets]
    if missing:
        raise SystemExit(f"no uploaded url for: {', '.join(missing)}")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    writes = []

    def emit(name: str, collection: str, doc_id: str, doc: dict) -> None:
        path = os.path.join(args.docs, name)
        with open(path, "w") as fh:
            json.dump(doc, fh)
        writes.append({"op": "set", "collection": collection, "doc_id": doc_id, "file_path": os.path.abspath(path)})

    emit("dash_meta.json", "dash", "meta", {"title": TITLE})
    for m in manifest:
        emit(f"datasets_{m['id']}.json", "datasets", m["id"], {
            "title": m["title"],
            "description": m["description"],
            "source": {"kind": "file", "url": assets[m["id"]], "name": m["file"], "format": "json"},
            "updated": {"at": now, "by": args.by},
        })
    with open(args.page) as fh:
        emit("files_index.json", "files", "index.html", {"text": fh.read()})
    print(json.dumps(writes, indent=1))


if __name__ == "__main__":
    main()
