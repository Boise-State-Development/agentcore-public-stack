"""Property tests for dependency version pinning.

Feature: supply-chain-hardening, Property 2: All Python dependencies use exact version pins
Feature: supply-chain-hardening, Property 3: All npm dependencies use exact version pins
Validates: Requirements 2.1, 2.2, 2.4, 3.1, 3.2, 5.1, 5.2
"""

import json
import re
import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

# Repository root is 3 levels up from backend/tests/supply_chain/
REPO_ROOT = Path(__file__).resolve().parents[3]
PYPROJECT_PATH = REPO_ROOT / "backend" / "pyproject.toml"

# Regex to detect the == operator in a dependency string.
# Dependency strings look like: "package==1.2.3" or "package[extra]==1.2.3"
EXACT_PIN_PATTERN = re.compile(r"==")

# Operators that indicate non-exact pins
FORBIDDEN_OPERATORS = re.compile(r"(>=|~=|<=|!=|>(?!=)|<(?!=))")

# Pattern to extract package name (with optional extras) from a dependency string
PACKAGE_NAME_PATTERN = re.compile(r"^([a-zA-Z0-9_.-]+(?:\[[a-zA-Z0-9_,.-]+\])?)")


def _parse_pyproject() -> dict:
    """Parse pyproject.toml and return the data dict."""
    with open(PYPROJECT_PATH, "rb") as f:
        return tomllib.load(f)


def _collect_dependency_strings(data: dict) -> list[tuple[str, str]]:
    """Collect all dependency strings from relevant sections.

    Returns a list of (section_name, dependency_string) tuples.
    Skips the 'all' optional dependency group (it just references other groups).
    Skips build-system.requires and requires-python.
    """
    results = []

    # [project].dependencies
    for dep in data.get("project", {}).get("dependencies", []):
        results.append(("dependencies", dep))

    # [project.optional-dependencies] — skip 'all'
    optional_deps = data.get("project", {}).get("optional-dependencies", {})
    for group_name, deps in optional_deps.items():
        if group_name == "all":
            continue
        for dep in deps:
            results.append((f"optional-dependencies.{group_name}", dep))

    return results


def test_all_python_dependencies_use_exact_pins():
    """Property 2: All Python dependencies use exact version pins.

    For any dependency string in any section of pyproject.toml
    (dependencies, [project.optional-dependencies].agentcore,
    [project.optional-dependencies].dev), the version specifier must
    use the == operator. Strings containing >=, ~=, >, <, or no
    version constraint must be rejected.

    **Validates: Requirements 2.1, 2.2, 2.4**
    """
    data = _parse_pyproject()
    all_deps = _collect_dependency_strings(data)

    assert len(all_deps) > 0, "No dependencies found in pyproject.toml"

    violations = []
    for section, dep_string in all_deps:
        # Check for forbidden operators (>=, ~=, >, <, etc.)
        if FORBIDDEN_OPERATORS.search(dep_string):
            violations.append(
                f"  [{section}] {dep_string} — uses a non-exact operator"
            )
        # Check that == is present (catches deps with no version constraint)
        elif not EXACT_PIN_PATTERN.search(dep_string):
            violations.append(
                f"  [{section}] {dep_string} — missing exact version pin (==)"
            )

    assert not violations, (
        f"Found {len(violations)} Python dependency(ies) without exact "
        f"version pins (== required):\n" + "\n".join(violations)
    )


# npm package.json files to check for exact version pins
NPM_PACKAGE_FILES = [
    REPO_ROOT / "frontend" / "ai.client" / "package.json",
    REPO_ROOT / "infrastructure" / "package.json",
]

# Prefixes that indicate non-exact npm version pins
NPM_FORBIDDEN_PREFIXES = ("^", "~", ">", "<", "*")


def test_all_npm_dependencies_use_exact_pins():
    """Property 3: All npm dependencies use exact version pins.

    For any dependency entry in the dependencies or devDependencies
    sections of frontend/ai.client/package.json and
    infrastructure/package.json, the version string must not begin
    with ^, ~, >, <, or *.

    **Validates: Requirements 3.1, 3.2, 5.1, 5.2**
    """
    violations = []
    total_deps = 0

    for pkg_path in NPM_PACKAGE_FILES:
        assert pkg_path.exists(), f"package.json not found: {pkg_path}"

        with open(pkg_path) as f:
            data = json.load(f)

        rel_path = pkg_path.relative_to(REPO_ROOT)

        for section in ("dependencies", "devDependencies"):
            deps = data.get(section, {})
            for name, version in deps.items():
                total_deps += 1
                if version.startswith(NPM_FORBIDDEN_PREFIXES):
                    violations.append(
                        f"  [{rel_path} → {section}] {name}: {version}"
                    )

    assert total_deps > 0, "No npm dependencies found in any package.json"

    assert not violations, (
        f"Found {len(violations)} npm dependency(ies) without exact "
        f"version pins (no ^, ~, >, <, or * allowed):\n"
        + "\n".join(violations)
    )
