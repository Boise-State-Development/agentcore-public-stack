"""Unit tests for documentation files and mypy version consistency.

Validates: Requirements 11.1–11.5, 12.1, 14.1, 14.3
"""

from pathlib import Path

import tomllib

# Repository root is 3 levels up from backend/tests/supply_chain/
REPO_ROOT = Path(__file__).resolve().parents[3]


class TestContributingMd:
    """Tests for CONTRIBUTING.md existence and required sections."""

    CONTRIBUTING_PATH = REPO_ROOT / "CONTRIBUTING.md"

    def test_contributing_md_exists(self):
        """CONTRIBUTING.md must exist at the repository root.

        **Validates: Requirement 11.1**
        """
        assert self.CONTRIBUTING_PATH.exists(), (
            "CONTRIBUTING.md not found at repository root"
        )

    def test_contributing_has_prerequisites(self):
        """CONTRIBUTING.md must document prerequisites.

        **Validates: Requirement 11.1**
        """
        content = self.CONTRIBUTING_PATH.read_text()
        assert "prerequisite" in content.lower(), (
            "CONTRIBUTING.md missing prerequisites section"
        )
        # Check for key tools
        for tool in ["Node.js", "Python", "Docker", "AWS CLI", "uv"]:
            assert tool.lower() in content.lower(), (
                f"CONTRIBUTING.md missing prerequisite: {tool}"
            )

    def test_contributing_has_install_steps(self):
        """CONTRIBUTING.md must document install steps for all components.

        **Validates: Requirement 11.2**
        """
        content = self.CONTRIBUTING_PATH.read_text().lower()
        for component in ["backend", "frontend", "infrastructure"]:
            assert component in content, (
                f"CONTRIBUTING.md missing install steps for {component}"
            )

    def test_contributing_has_environment_config(self):
        """CONTRIBUTING.md must document environment variable configuration.

        **Validates: Requirement 11.3**
        """
        content = self.CONTRIBUTING_PATH.read_text()
        assert "environment" in content.lower(), (
            "CONTRIBUTING.md missing environment configuration section"
        )
        # Should reference the actual config file locations
        assert ".env" in content or "environment.ts" in content, (
            "CONTRIBUTING.md should reference backend .env or frontend environment.ts"
        )

    def test_contributing_has_test_instructions(self):
        """CONTRIBUTING.md must document how to run test suites.

        **Validates: Requirement 11.4**
        """
        content = self.CONTRIBUTING_PATH.read_text().lower()
        assert "test" in content, (
            "CONTRIBUTING.md missing test instructions"
        )
        assert "pytest" in content, (
            "CONTRIBUTING.md missing backend test command (pytest)"
        )
        assert "npm test" in content, (
            "CONTRIBUTING.md missing frontend test command (npm test)"
        )

    def test_contributing_has_aws_credentials(self):
        """CONTRIBUTING.md must document AWS credential setup.

        **Validates: Requirement 11.5**
        """
        content = self.CONTRIBUTING_PATH.read_text().lower()
        assert "aws" in content and "credential" in content, (
            "CONTRIBUTING.md missing AWS credential setup section"
        )


class TestArtifactRetentionMd:
    """Tests for .github/ARTIFACT_RETENTION.md."""

    RETENTION_PATH = REPO_ROOT / ".github" / "ARTIFACT_RETENTION.md"

    REQUIRED_ARTIFACT_TYPES = [
        "docker image",
        "cdk",
        "test",
        "deployment",
        "scan",
    ]

    def test_artifact_retention_md_exists(self):
        """ARTIFACT_RETENTION.md must exist in .github/.

        **Validates: Requirement 14.1**
        """
        assert self.RETENTION_PATH.exists(), (
            ".github/ARTIFACT_RETENTION.md not found"
        )

    def test_artifact_retention_documents_all_types(self):
        """ARTIFACT_RETENTION.md must document all artifact types.

        **Validates: Requirement 14.3**
        """
        content = self.RETENTION_PATH.read_text().lower()
        missing = []
        for artifact_type in self.REQUIRED_ARTIFACT_TYPES:
            if artifact_type not in content:
                missing.append(artifact_type)

        assert not missing, (
            f"ARTIFACT_RETENTION.md missing documentation for: {', '.join(missing)}"
        )


class TestMypyVersion:
    """Tests for mypy python_version consistency."""

    PYPROJECT_PATH = REPO_ROOT / "backend" / "pyproject.toml"

    def test_mypy_version_matches_requires_python(self):
        """mypy python_version must match the minimum requires-python version.

        **Validates: Requirement 12.1**
        """
        with open(self.PYPROJECT_PATH, "rb") as f:
            pyproject = tomllib.load(f)

        # Extract requires-python minimum version
        requires_python = pyproject.get("project", {}).get("requires-python", "")
        # Parse minimum version from ">=3.10" or ">=3.13"
        min_version = requires_python.replace(">=", "").strip()
        assert min_version, f"Could not parse requires-python: {requires_python}"

        # Extract mypy python_version
        mypy_version = (
            pyproject.get("tool", {}).get("mypy", {}).get("python_version", "")
        )
        assert mypy_version, "mypy python_version not found in pyproject.toml"

        assert mypy_version == min_version, (
            f"mypy python_version ({mypy_version}) does not match "
            f"requires-python minimum ({min_version}). "
            f"Update [tool.mypy] python_version to '{min_version}'"
        )
