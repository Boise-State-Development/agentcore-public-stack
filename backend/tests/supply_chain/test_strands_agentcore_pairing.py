"""Guard rails for the Strands / AgentCore / MCP pin set.

Two rules that used to live only as prose in the kaizen queue, now asserted:

1. **`strands-agents` >= 1.56 and `bedrock-agentcore` >= 1.23.1 move together.**
   A half-bumped pair imports fine locally and in the suite, then fails on the
   AgentCore Runtime as "initialization time exceeded (30s)", which reaches
   the user as a 502. Nothing in a unit test would otherwise notice, so the
   pairing is checked on the declared pins, on what `uv.lock` resolved, and
   on the Lambda images that install `bedrock-agentcore` from their own
   hand-maintained requirements files.

2. **`mcp` stays below 2 because we declared it, not by accident.**
   `strands-agents` itself allows `mcp<2.2`. Without a declared constraint the
   only thing holding the 1.x line is `idna==3.15` (mcp 2.x -> httpx2 -> idna
   >=3.18), so a routine security bump of `idna` would silently cross the
   major version under `integrations/mcp_apps.py`'s `ClientSession` patch.
   See the comment on `[tool.uv].constraint-dependencies` in pyproject.toml.
"""

import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.version import Version

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PYPROJECT_PATH = BACKEND_ROOT / "pyproject.toml"
UV_LOCK_PATH = BACKEND_ROOT / "uv.lock"

STRANDS_PAIRING_FLOOR = Version("1.56.0")
AGENTCORE_PAIRING_FLOOR = Version("1.23.1")

# Lambda images that pip-install from their own requirements file rather
# than from uv.lock. Each says "versions match backend/uv.lock" in its header.
LAMBDA_REQUIREMENTS_FILES = [
    BACKEND_ROOT / "src" / "lambdas" / "scheduled_runs_worker" / "requirements.txt",
    BACKEND_ROOT / "src" / "lambdas" / "scheduled_runs_dispatcher" / "requirements.txt",
    BACKEND_ROOT / "src" / "apis" / "app_api" / "kb_sync" / "requirements.txt",
    BACKEND_ROOT / "src" / "apis" / "app_api" / "kb_migration" / "requirements.txt",
]

# Packages whose Lambda-side pins must equal the lockfile. boto3/botocore are
# included because bedrock-agentcore 1.23.1 raised its boto floor to 1.43.72:
# an agentcore bump without the boto bump fails `pip install` at image build.
LOCK_MATCHED_PACKAGES = ("bedrock-agentcore", "boto3", "botocore")


def _load_toml(path: Path) -> dict:
    with open(path, "rb") as f:
        return tomllib.load(f)


def _all_declared_requirements(pyproject: dict) -> List[Requirement]:
    project = pyproject["project"]
    reqs = [Requirement(dep) for dep in project.get("dependencies", [])]
    for group, deps in project.get("optional-dependencies", {}).items():
        if group == "all":
            continue
        reqs.extend(Requirement(dep) for dep in deps)
    return reqs


def _exact_pins(pyproject: dict, name: str) -> List[Tuple[Requirement, Version]]:
    pins = []
    for req in _all_declared_requirements(pyproject):
        if req.name != name:
            continue
        specs = list(req.specifier)
        assert len(specs) == 1 and specs[0].operator == "==", f"{req} is not a single exact pin"
        pins.append((req, Version(specs[0].version)))
    return pins


def _declared_version(pyproject: dict, name: str) -> Version:
    pins = _exact_pins(pyproject, name)
    assert pins, f"{name} is not pinned in pyproject.toml"
    versions = {v for _, v in pins}
    assert len(versions) == 1, (
        f"{name} is pinned at more than one version across pyproject.toml "
        f"groups: {sorted(str(r) for r, _ in pins)}. The base package and its "
        "extras (e.g. strands-agents and strands-agents[bidi]) must move together."
    )
    return versions.pop()


def _locked_versions() -> Dict[str, Version]:
    lock = _load_toml(UV_LOCK_PATH)
    return {pkg["name"]: Version(pkg["version"]) for pkg in lock["package"] if "version" in pkg}


def _requirements_pin(path: Path, name: str) -> Optional[Version]:
    pattern = re.compile(rf"^\s*{re.escape(name)}\s*==\s*([^\s#;]+)", re.IGNORECASE)
    for line in path.read_text().splitlines():
        match = pattern.match(line)
        if match:
            return Version(match.group(1))
    return None


def _paired(strands: Version, agentcore: Version) -> bool:
    return (strands >= STRANDS_PAIRING_FLOOR) == (agentcore >= AGENTCORE_PAIRING_FLOOR)


# ---------------------------------------------------------------------------
# Guard rail 1: strands >= 1.56  <=>  bedrock-agentcore >= 1.23.1
# ---------------------------------------------------------------------------


def test_declared_strands_and_agentcore_pins_are_paired():
    pyproject = _load_toml(PYPROJECT_PATH)
    strands = _declared_version(pyproject, "strands-agents")
    agentcore = _declared_version(pyproject, "bedrock-agentcore")

    assert _paired(strands, agentcore), (
        f"strands-agents=={strands} with bedrock-agentcore=={agentcore} breaks the "
        f"pairing: strands >= {STRANDS_PAIRING_FLOOR} requires bedrock-agentcore >= "
        f"{AGENTCORE_PAIRING_FLOOR} and vice versa. A half-bumped pair presents on "
        "the AgentCore Runtime as 'initialization time exceeded (30s)' -> 502."
    )


def test_locked_strands_and_agentcore_match_declared_pins():
    pyproject = _load_toml(PYPROJECT_PATH)
    locked = _locked_versions()

    for name in ("strands-agents", "bedrock-agentcore"):
        declared = _declared_version(pyproject, name)
        assert name in locked, f"{name} missing from uv.lock"
        assert locked[name] == declared, (
            f"uv.lock resolves {name}=={locked[name]} but pyproject.toml pins "
            f"{declared}. Run `uv lock` in backend/."
        )

    assert _paired(locked["strands-agents"], locked["bedrock-agentcore"])


def test_lambda_requirements_match_lock_for_agentcore_and_boto():
    locked = _locked_versions()
    mismatches = []
    checked = 0

    for path in LAMBDA_REQUIREMENTS_FILES:
        assert path.exists(), f"Lambda requirements file not found: {path}"
        for name in LOCK_MATCHED_PACKAGES:
            pinned = _requirements_pin(path, name)
            if pinned is None:
                continue
            checked += 1
            if pinned != locked[name]:
                rel = path.relative_to(BACKEND_ROOT)
                mismatches.append(f"  {rel}: {name}=={pinned} (uv.lock has {locked[name]})")

    assert checked > 0, "No bedrock-agentcore/boto3/botocore pins found in Lambda requirements files"
    assert not mismatches, "Lambda requirements drifted from backend/uv.lock:\n" + "\n".join(mismatches)


# ---------------------------------------------------------------------------
# Guard rail 2: mcp < 2 is held by a DECLARED constraint
# ---------------------------------------------------------------------------


def _declared_mcp_constraint(pyproject: dict) -> SpecifierSet:
    constraints = pyproject.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])
    specs = [Requirement(c).specifier for c in constraints if Requirement(c).name == "mcp"]
    assert specs, (
        "pyproject.toml [tool.uv].constraint-dependencies no longer constrains `mcp`. "
        "strands-agents allows mcp<2.2, so nothing but an unrelated idna pin would "
        "keep the resolver on mcp 1.x. Restore `mcp<2` or lift it deliberately "
        "together with a test pass over integrations/mcp_apps.py."
    )
    combined = SpecifierSet()
    for spec in specs:
        combined &= spec
    return combined


def test_mcp_below_2_is_a_declared_constraint():
    combined = _declared_mcp_constraint(_load_toml(PYPROJECT_PATH))

    for probe in ("2.0.0", "2.0.0rc1", "2.1.0", "2.2.0"):
        assert not combined.contains(probe, prereleases=True), (
            f"[tool.uv] constraint on mcp ({combined}) admits {probe}; it must exclude every 2.x release."
        )
    assert combined.contains("1.30.0"), f"[tool.uv] constraint on mcp ({combined}) excludes the 1.x line entirely"


def test_uv_lock_was_resolved_under_the_mcp_constraint():
    lock = _load_toml(UV_LOCK_PATH)
    manifest_constraints = lock.get("manifest", {}).get("constraints", [])
    mcp_entries = [c for c in manifest_constraints if c.get("name") == "mcp"]

    assert mcp_entries, "uv.lock [manifest] carries no mcp constraint; it was not resolved under pyproject's [tool.uv] constraint"
    for entry in mcp_entries:
        assert not SpecifierSet(entry["specifier"]).contains("2.0.0", prereleases=True)

    locked = _locked_versions()
    assert "mcp" in locked, "mcp missing from uv.lock"
    assert locked["mcp"] < Version("2"), f"uv.lock resolved mcp=={locked['mcp']}"
