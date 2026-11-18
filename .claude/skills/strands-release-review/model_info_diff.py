"""Diff Strands' shared model info for the model ids we curate, between two wheels.

Usage (from the repo root; the wheel trees are untrusted downloads, so run with -I):

    python3 -I .claude/skills/strands-release-review/model_info_diff.py \
        <scratch>/wheels/w<old>/strands <scratch>/wheels/w<new>/strands \
        frontend/ai.client/src/app/admin/manage-models/models/curated-models.ts

Prints one line per curated id: the `model_info.json` key it resolves to in each
wheel and every capability or limit that changed. Release notes rarely mention
these flips (`forcedToolChoice`, `defaultEffort`, `temporaryMessages`,
`serverSideSummary`, `promptCaching`), and some of them change what a request
looks like on the wire.

It never imports Strands. The id lookup mirrors
`strands.models._model_info.lookup_model`, and the two suffix patterns are read
from each wheel's own `_model_info.py`, so a change to the lookup rules shows up
as a key change rather than as a silent mismatch. Note which camelCase keys the
new wheel's `_CAPABILITY_FIELDS` maps: a key the Python side does not map (for
example `defaultEffort` on 1.65.0) is data only, read by the TypeScript SDK.
"""

import json
import re
import sys
from pathlib import Path

_PATTERN_LINE = r'^{name} = re\.compile\(r"(?P<pattern>[^"]+)"\)'


def _load_lookup(strands_dir: Path) -> tuple[dict, re.Pattern, re.Pattern, set[str]]:
    source = (strands_dir / "models" / "_model_info.py").read_text()
    patterns = []
    for name in ("_VERSION_SUFFIX", "_DATE_SUFFIX"):
        match = re.search(_PATTERN_LINE.format(name=name), source, re.MULTILINE)
        if match is None:
            raise SystemExit(f"{strands_dir}: could not find {name} in _model_info.py; the lookup changed, read it")
        patterns.append(re.compile(match.group("pattern")))
    mapped = set(re.findall(r'^\s+"(\w+)": "\w+",$', source, re.MULTILINE))
    data = json.loads((strands_dir / "models" / "model_info.json").read_text())
    return data, patterns[0], patterns[1], mapped


def _lookup(models: dict, version: re.Pattern, date: re.Pattern, model_id: str) -> tuple[str | None, dict]:
    candidate = model_id
    while True:
        unversioned = version.sub("", candidate)
        for key in (candidate, unversioned, date.sub("", unversioned)):
            if key in models:
                return key, models[key]
        dot = candidate.find(".")
        if dot == -1:
            return None, {}
        candidate = candidate[dot + 1 :]


def _flatten(entry: dict) -> dict[str, object]:
    flat: dict[str, object] = {}
    for section, values in entry.items():
        if isinstance(values, dict):
            for key, value in values.items():
                flat[f"{section}.{key}"] = value
        else:
            flat[section] = values
    return flat


def _curated_ids(curated_ts: Path) -> list[str]:
    return sorted(set(re.findall(r"^\s+modelId: '([^']+)'", curated_ts.read_text(), re.MULTILINE)))


def main() -> None:
    if len(sys.argv) != 4:
        raise SystemExit(__doc__)
    old_data, old_version, old_date, _ = _load_lookup(Path(sys.argv[1]))
    new_data, new_version, new_date, new_mapped = _load_lookup(Path(sys.argv[2]))
    ids = _curated_ids(Path(sys.argv[3]))

    if old_data.get("defaults") != new_data.get("defaults"):
        print(f"defaults: {old_data.get('defaults')} -> {new_data.get('defaults')}")

    for model_id in ids:
        old_key, old_entry = _lookup(old_data["models"], old_version, old_date, model_id)
        new_key, new_entry = _lookup(new_data["models"], new_version, new_date, model_id)
        before, after = _flatten(old_entry), _flatten(new_entry)
        changes = []
        for field in sorted(set(before) | set(after)):
            if before.get(field) == after.get(field):
                continue
            unmapped = field.startswith("capabilities.") and field.split(".", 1)[1] not in new_mapped
            note = " (not read by the Python SDK)" if unmapped else ""
            changes.append(f"{field}: {before.get(field, '-')} -> {after.get(field, '-')}{note}")
        key = (new_key or "no entry") if old_key == new_key else f"{old_key} -> {new_key}"
        print(f"{model_id} [{key}]: " + ("; ".join(changes) if changes else "unchanged"))


if __name__ == "__main__":
    main()
