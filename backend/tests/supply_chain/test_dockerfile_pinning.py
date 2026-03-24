"""Property tests for Dockerfile package version pinning.

Feature: supply-chain-hardening, Property 9: Dockerfile apt-get packages have version pins
Validates: Requirements 10.1, 10.2
"""

import re
from pathlib import Path

# Repository root is 3 levels up from backend/tests/supply_chain/
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_ROOT / "backend"

DOCKERFILES = [
    BACKEND_DIR / "Dockerfile.app-api",
    BACKEND_DIR / "Dockerfile.inference-api",
    BACKEND_DIR / "Dockerfile.rag-ingestion",
]

# apt-get version pin: package=version (e.g., gcc=4:14.2.0-1)
APT_VERSION_PIN = re.compile(r"^[\w][\w.+-]*=\S+$")

# dnf version pin: the version part starts with a digit after the last relevant `-`.
# Examples:
#   gcc-11.5.0-5.amzn2023.0.5 → pinned
#   gcc-c++-11.5.0-5.amzn2023.0.5 → pinned
#   mesa-libGL-24.2.6-1267.amzn2023.0.1 → pinned
# We check: after splitting on `-`, at least one segment starts with a digit,
# and it appears after the package name portion.
DNF_VERSION_PIN = re.compile(r"^[\w][\w.+-]*-\d[\w.+-]*$")

# Flags to skip when parsing package lists
INSTALL_FLAGS = {"-y", "--assumeyes", "--no-install-recommends", "--setopt=install_weak_deps=False"}


def _find_install_commands(content: str) -> list[tuple[str, str, list[str]]]:
    """Find apt-get install and dnf install commands in Dockerfile content.

    Returns a list of (manager, raw_command, packages) tuples where:
    - manager is 'apt-get' or 'dnf'
    - raw_command is the full joined command text
    - packages is the list of package tokens extracted
    """
    results = []

    # Join continuation lines: replace `\<newline>` with space
    joined = content.replace("\\\n", " ")

    # Split on `&&` or `;` to get individual commands
    # Then find apt-get install or dnf install commands
    for line in joined.split("\n"):
        # Split on && to handle chained commands
        segments = re.split(r"&&", line)
        for segment in segments:
            segment = segment.strip()

            # Detect apt-get install or dnf install
            apt_match = re.search(r"\bapt-get\s+install\b", segment)
            dnf_match = re.search(r"\bdnf\s+install\b", segment)

            if not apt_match and not dnf_match:
                continue

            manager = "apt-get" if apt_match else "dnf"
            match = apt_match or dnf_match

            # Extract everything after "apt-get install" or "dnf install"
            after_install = segment[match.end():].strip()

            # Tokenize and filter out flags
            tokens = after_install.split()
            packages = []
            for token in tokens:
                # Stop at shell operators or cleanup commands
                if token in ("&&", ";", "|", "||"):
                    break
                # Skip flags
                if token.startswith("-"):
                    continue
                # Skip empty tokens
                if not token:
                    continue
                packages.append(token)

            if packages:
                results.append((manager, segment.strip(), packages))

    return results


def _is_apt_pinned(package: str) -> bool:
    """Check if an apt-get package has a version pin (package=version)."""
    return bool(APT_VERSION_PIN.match(package))


def _is_dnf_pinned(package: str) -> bool:
    """Check if a dnf package has a version pin.

    For dnf, version pinning uses `-` separator but package names can also
    contain `-` (e.g., gcc-c++, mesa-libGL). The version starts with a digit
    after the last relevant `-`.

    Strategy: split on `-`, find the first segment that starts with a digit.
    If such a segment exists (and it's not the first segment), the package is pinned.
    """
    parts = package.split("-")
    if len(parts) < 2:
        return False

    # Find the first part that starts with a digit — that's where the version begins
    for i, part in enumerate(parts):
        if i == 0:
            continue  # First part is always package name
        if part and part[0].isdigit():
            return True

    return False


def test_dockerfile_apt_get_packages_have_version_pins():
    """Property 9: Dockerfile apt-get packages have version pins.

    For any apt-get install or dnf install command in any Dockerfile under
    backend/, every package name must include a version pin:
    - apt-get: package=version format (e.g., gcc=4:14.2.0-1)
    - dnf: package-version format where version starts with a digit
      (e.g., gcc-11.5.0-5.amzn2023.0.5)

    Or the package must have a comment documenting why the pin is omitted.

    **Validates: Requirements 10.1, 10.2**
    """
    existing_dockerfiles = [df for df in DOCKERFILES if df.exists()]
    assert len(existing_dockerfiles) > 0, (
        "No Dockerfiles found in backend/. "
        f"Expected files: {[str(df.relative_to(REPO_ROOT)) for df in DOCKERFILES]}"
    )

    violations = []
    total_packages = 0

    for dockerfile_path in existing_dockerfiles:
        content = dockerfile_path.read_text()
        rel_path = str(dockerfile_path.relative_to(REPO_ROOT))
        install_commands = _find_install_commands(content)

        for manager, raw_cmd, packages in install_commands:
            for pkg in packages:
                total_packages += 1

                if manager == "apt-get":
                    if not _is_apt_pinned(pkg):
                        violations.append(
                            f"  {rel_path}: apt-get package `{pkg}` missing "
                            f"version pin (expected: package=version)"
                        )
                elif manager == "dnf":
                    if not _is_dnf_pinned(pkg):
                        violations.append(
                            f"  {rel_path}: dnf package `{pkg}` missing "
                            f"version pin (expected: package-version)"
                        )

    assert total_packages > 0, (
        "No apt-get/dnf install packages found in any Dockerfile. "
        "Expected at least one package installation to validate."
    )

    assert not violations, (
        f"Found {len(violations)} package(s) without version pins "
        f"(out of {total_packages} total):\n" + "\n".join(violations)
    )
