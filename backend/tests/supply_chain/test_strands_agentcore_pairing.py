"""Guard rails for the Strands / AgentCore / MCP pin set.

Two rules that used to live only as prose in the kaizen queue, now asserted:

1. **`strands-agents` >= 1.56 and `bedrock-agentcore` >= 1.23.1 move together.**
   A half-bumped pair imports fine locally and in the suite, then fails on the
   AgentCore Runtime as "initialization time exceeded (30s)", which reaches
   the user as a 502. Nothing in a unit test would otherwise notice, so the
   pairing is checked on the declared pins, on what `uv.lock` resolved, and
   on the Lambda images that install `bedrock-agentcore` from their own
   hand-maintained requirements files.

2. **The `mcp` SDK line is chosen by a declared exact pin, inside Strands' range.**
   `mcp` reaches us only through `strands-agents`, which allows a range
   (`mcp>=1.23.0,<2.2` on 1.57.2). Our seams on the SDK
   (`integrations/mcp_apps.py`'s `ClientSession` patch and
   `integrations/mcp_transport.py`) fail silently on a line they were not
   written for, so the version is an exact `[tool.uv]` constraint that
   `uv.lock` agrees with, and it must stay inside the range the pinned
   Strands declares. See the comment on `[tool.uv].constraint-dependencies`
   in pyproject.toml.
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
# Guard rail 2: the mcp version is a DECLARED exact pin inside Strands' range
# ---------------------------------------------------------------------------

# The 2.x transport's own dependencies, pinned with it so the lock never
# drifts. `mcp-types` is exact-pinned by `mcp` itself.
_MCP_COMPANION_PINS = ("httpx2", "httpcore2", "truststore")


def _declared_constraints(pyproject: dict) -> Dict[str, SpecifierSet]:
    constraints = pyproject.get("tool", {}).get("uv", {}).get("constraint-dependencies", [])
    out: Dict[str, SpecifierSet] = {}
    for raw in constraints:
        req = Requirement(raw)
        out[req.name] = out.get(req.name, SpecifierSet()) & req.specifier
    return out


def _exact_pin(spec: SpecifierSet) -> Optional[Version]:
    pins = [s for s in spec if s.operator == "=="]
    return Version(pins[0].version) if len(pins) == 1 and len(spec) == 1 else None


def _declared_mcp_pin() -> Version:
    spec = _declared_constraints(_load_toml(PYPROJECT_PATH)).get("mcp")
    assert spec is not None, (
        "pyproject.toml [tool.uv].constraint-dependencies no longer constrains `mcp`. "
        "strands-agents allows a range of SDK versions, so the resolver would move "
        "the MCP SDK on its own. Pin it exactly, together with a test pass over "
        "integrations/mcp_apps.py and integrations/mcp_transport.py."
    )
    pin = _exact_pin(spec)
    assert pin is not None, f"[tool.uv] constraint on mcp ({spec}) must be one exact `==` pin"
    return pin


def test_mcp_is_a_declared_exact_pin():
    _declared_mcp_pin()


def test_mcp_pin_is_inside_the_range_strands_declares():
    from importlib.metadata import requires

    pin = _declared_mcp_pin()
    strands_mcp = [Requirement(r) for r in requires("strands-agents") or [] if Requirement(r).name == "mcp"]
    assert strands_mcp, "strands-agents no longer declares an mcp requirement"
    for req in strands_mcp:
        if req.marker is None:
            assert req.specifier.contains(pin), (
                f"mcp=={pin} is outside strands-agents' declared range ({req.specifier})"
            )


def test_uv_lock_was_resolved_under_the_mcp_pin():
    pin = _declared_mcp_pin()
    lock = _load_toml(UV_LOCK_PATH)
    manifest_constraints = lock.get("manifest", {}).get("constraints", [])
    mcp_entries = [c for c in manifest_constraints if c.get("name") == "mcp"]
    assert mcp_entries, "uv.lock [manifest] carries no mcp constraint; it was not resolved under pyproject's [tool.uv] constraint"

    locked = _locked_versions()
    assert locked.get("mcp") == pin, f"uv.lock resolved mcp=={locked.get('mcp')}, pyproject pins {pin}"
    assert locked.get("mcp-types") == pin, f"uv.lock resolved mcp-types=={locked.get('mcp-types')}, expected {pin}"


def test_the_2x_transport_dependencies_are_pinned_and_locked():
    declared = _declared_constraints(_load_toml(PYPROJECT_PATH))
    locked = _locked_versions()
    for name in _MCP_COMPANION_PINS:
        assert name in declared, f"[tool.uv] constraint-dependencies does not pin {name}"
        pin = _exact_pin(declared[name])
        assert pin is not None, f"[tool.uv] constraint on {name} ({declared[name]}) must be one exact `==` pin"
        assert locked.get(name) == pin, f"uv.lock resolved {name}=={locked.get(name)}, pyproject pins {pin}"


def test_lambda_images_that_install_strands_carry_the_same_mcp_set():
    """The scheduled-runs image installs Strands, and so `mcp`, from its own
    requirements file. A Lambda left on another SDK line is a second line in
    production that no test here exercises."""
    locked = _locked_versions()
    mismatches = []
    checked = 0
    for path in LAMBDA_REQUIREMENTS_FILES:
        if _requirements_pin(path, "strands-agents") is None and _requirements_pin(path, "mcp") is None:
            continue
        rel = path.relative_to(BACKEND_ROOT)
        for name in ("strands-agents", "mcp", "mcp-types", *_MCP_COMPANION_PINS):
            pinned = _requirements_pin(path, name)
            checked += 1
            if pinned is None:
                mismatches.append(f"  {rel}: {name} is not pinned (uv.lock has {locked.get(name)})")
            elif pinned != locked.get(name):
                mismatches.append(f"  {rel}: {name}=={pinned} (uv.lock has {locked.get(name)})")
    assert checked > 0, "No Lambda requirements file installs strands-agents; re-check this guard"
    assert not mismatches, "Lambda mcp set drifted from backend/uv.lock:\n" + "\n".join(mismatches)
