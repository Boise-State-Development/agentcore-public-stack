"""Score a run's audit step against the expected answers in criteria.py.

Usage: python3 -I tools/score_audit.py <run.json>
<run.json> is GET /sessions/{id}/workflow-runs/{runId}; the audit step's
steps[].checklist rows are read from it.
"""

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("criteria", HERE / "criteria.py")
criteria = importlib.util.module_from_spec(spec)
spec.loader.exec_module(criteria)

expected = {}
for set_name, title, fields in criteria.SECTIONS:
    for name, _t, _f, code in fields:
        expected[(set_name, name)] = {criteria.CODE[c] for c in code.split("/")}

run = json.load(open(sys.argv[1]))
run = run.get("run", run)
rows = []
for step in run.get("steps", []):
    rows.extend(step.get("checklist") or [])

got = {}
states = Counter()
errors = []
for row in rows:
    if row.get("status") != "done":
        errors.append(f"{row.get('fieldSet')}: {row.get('status')} {row.get('error') or ''}")
    for field in row.get("fields") or []:
        got[(row["fieldSet"], field["name"])] = field
        states[field.get("state")] += 1

right = wrong = missing = 0
lines = []
for key, allowed in expected.items():
    field = got.get(key)
    if field is None:
        missing += 1
        lines.append(f"MISSING  {key[0]}.{key[1]}")
        continue
    value = field.get("value")
    if value in allowed:
        right += 1
    else:
        wrong += 1
        lines.append(f"WRONG    {key[0]}.{key[1]}: got {value!r}, expected {'/'.join(sorted(allowed))}"
                     f" | {str(field.get('quote') or '')[:110]}")
plants = {("formatting", "project_description_page_limit"): "A", ("budget", "senior_salary_two_months"): "B",
          ("supplementary-docs", "dmp_repository"): "G"}
print(f"{right}/{len(expected)} as expected, {wrong} wrong, {missing} missing; set errors: {errors or 'none'}")
for key, plant in plants.items():
    print(f"  plant {plant} ({key[1]}): {(got.get(key) or {}).get('value')!r}")
print("  provenance states:", dict(states))
print("\n".join(lines))
