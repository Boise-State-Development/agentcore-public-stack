"""Property tests for shell script hardening.

Feature: supply-chain-hardening, Property 4: Global npm installs specify exact versions
Feature: supply-chain-hardening, Property 5: CI install paths use npm ci with lockfile check
Validates: Requirements 4.1, 4.3, 6.1, 6.2
"""

import glob
import re
from pathlib import Path

# Repository root is 3 levels up from backend/tests/supply_chain/
REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS_DIR = REPO_ROOT / "scripts"

# Pattern to match `npm install -g <package>` commands.
# Captures the package argument(s) after `-g`.
# Handles both `npm install -g pkg` and `npm install --global pkg`.
NPM_GLOBAL_INSTALL_PATTERN = re.compile(
    r"npm\s+install\s+(?:.*\s)?(?:-g|--global)\s+(.+?)(?:\s*[;&|#\n]|$)"
)

# Pattern to verify a package has an @version suffix (e.g., aws-cdk@2.1113.0)
VERSIONED_PACKAGE_PATTERN = re.compile(r"^[\w@/.-]+@[\d][\w.*-]*$")

# Pattern to detect `npm install` for project dependencies (not global).
# Matches `npm install` that is NOT followed by `-g` or `--global`.
# This should be `npm ci` instead.
NPM_PROJECT_INSTALL_PATTERN = re.compile(
    r"^\s*npm\s+install\b(?!\s+.*(?:-g|--global))"
)

# Pattern to detect `npm ci` usage
NPM_CI_PATTERN = re.compile(r"npm\s+ci\b")

# Pattern to detect lockfile existence check
LOCKFILE_CHECK_PATTERN = re.compile(r"package-lock\.json")


def _collect_shell_scripts() -> list[Path]:
    """Collect all .sh files recursively under scripts/."""
    return sorted(
        Path(f) for f in glob.glob(str(SCRIPTS_DIR / "**" / "*.sh"), recursive=True)
    )


def _collect_install_scripts() -> list[Path]:
    """Collect install.sh scripts from stack directories under scripts/."""
    return sorted(
        Path(f)
        for f in glob.glob(str(SCRIPTS_DIR / "*" / "install.sh"))
    )


def test_global_npm_installs_specify_exact_versions():
    """Property 4: Global npm installs specify exact versions.

    For any `npm install -g` command in any shell script under scripts/,
    the package name must include an @version suffix (e.g., aws-cdk@2.1113.0).

    **Validates: Requirements 4.1, 4.3**
    """
    all_scripts = _collect_shell_scripts()
    assert len(all_scripts) > 0, "No shell scripts found under scripts/"

    violations = []
    found_global_installs = 0

    for script_path in all_scripts:
        content = script_path.read_text()
        rel_path = script_path.relative_to(REPO_ROOT)

        for line_no, line in enumerate(content.splitlines(), start=1):
            # Skip comments
            stripped = line.strip()
            if stripped.startswith("#"):
                continue

            match = NPM_GLOBAL_INSTALL_PATTERN.search(line)
            if match:
                packages_str = match.group(1).strip()
                # Split on whitespace to handle multiple packages
                packages = packages_str.split()
                for pkg in packages:
                    # Skip flags (e.g., --save, --silent)
                    if pkg.startswith("-"):
                        continue
                    found_global_installs += 1
                    if not VERSIONED_PACKAGE_PATTERN.match(pkg):
                        violations.append(
                            f"  {rel_path}:{line_no}: `{pkg}` missing @version suffix"
                        )

    assert found_global_installs > 0, (
        "No `npm install -g` commands found in any shell script under scripts/. "
        "Expected at least one global npm install to validate."
    )

    assert not violations, (
        f"Found {len(violations)} global npm install(s) without exact version pins "
        f"(expected: package@version):\n" + "\n".join(violations)
    )


def test_install_scripts_use_npm_ci_with_lockfile_check():
    """Property 5: CI install paths use npm ci with lockfile check.

    For any install script under scripts/*/install.sh that installs npm
    project dependencies, the script must use `npm ci` (not `npm install`)
    and must include a lockfile existence check for package-lock.json.

    Note: `npm install -g` (global installs) are exempt — only project
    dependency installs should use `npm ci`.

    **Validates: Requirements 6.1, 6.2**
    """
    install_scripts = _collect_install_scripts()
    assert len(install_scripts) > 0, "No install.sh scripts found under scripts/*/"

    violations = []
    scripts_with_npm_deps = 0

    for script_path in install_scripts:
        content = script_path.read_text()
        rel_path = script_path.relative_to(REPO_ROOT)

        # Check if this script installs npm project dependencies
        # (has `npm ci` or `npm install` without -g)
        has_npm_ci = bool(NPM_CI_PATTERN.search(content))
        has_npm_project_install = False

        for line_no, line in enumerate(content.splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if NPM_PROJECT_INSTALL_PATTERN.search(line):
                has_npm_project_install = True

        if not has_npm_ci and not has_npm_project_install:
            # This script doesn't install npm project deps — skip
            continue

        scripts_with_npm_deps += 1

        # Violation: uses `npm install` (not global) instead of `npm ci`
        if has_npm_project_install:
            # Find the offending lines for the error message
            for line_no, line in enumerate(content.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if NPM_PROJECT_INSTALL_PATTERN.search(line):
                    violations.append(
                        f"  {rel_path}:{line_no}: uses `npm install` instead of `npm ci`: "
                        f"{stripped}"
                    )

        # Violation: uses npm ci but no lockfile check
        if has_npm_ci and not LOCKFILE_CHECK_PATTERN.search(content):
            violations.append(
                f"  {rel_path}: uses `npm ci` but has no lockfile existence check "
                f"(should check for package-lock.json)"
            )

    assert scripts_with_npm_deps > 0, (
        "No install scripts found that install npm project dependencies. "
        "Expected at least one script with `npm ci` or `npm install`."
    )

    assert not violations, (
        f"Found {len(violations)} npm install enforcement violation(s):\n"
        + "\n".join(violations)
    )
