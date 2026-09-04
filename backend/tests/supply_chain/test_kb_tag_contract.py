"""
The managed knowledge base tag contract, asserted across three languages.

Requirements 20.8, 20.11, 20.12, 14.1.

Four components have to agree on the tags that identify this platform's knowledge
bases: the Python that writes them, the Python that filters on them, the CDK that
supplies their values, and the shell script that tears them down. Nothing in any
type system spans that set, and when nothing asserted it they drifted **three
ways** — different key names, different value sources, and a construct exporting
environment variables that no code read.

The failure was silent in the worst way. The writer and the reconciler agreed with
each other, because both fell back to the same hardcoded defaults; so knowledge
bases were created, found, and reconciled normally. Only teardown disagreed, and
its symptom was matching nothing and reporting success — a leak of resources
billing at $5.00/GB-month, with no CloudFormation console to notice them in.

So these tests read the TypeScript and the shell script as text and compare them
against the Python constants. Static, cross-language, and ugly — and the only
shape of test that can catch this class of defect.

Feature: managed-kb-migration
Requirements: 20.8, 20.11, 20.12, 14.1
"""

import re
from pathlib import Path

import pytest

from apis.shared.kb_backend import tags as canonical

REPO_ROOT = Path(__file__).resolve().parents[3]
CONSTRUCT = (
    REPO_ROOT / "infrastructure" / "lib" / "constructs" / "managed-kb" / "kb-migration-construct.ts"
)
TEARDOWN = REPO_ROOT / "scripts" / "teardown" / "managed-kb.sh"


def _ts_tag_keys() -> dict:
    """Extract ``MANAGED_KB_TAG_KEYS`` from the construct.

    Parsed rather than duplicated, so this test cannot itself drift from the file
    it is checking.
    """
    body = CONSTRUCT.read_text(encoding="utf-8")
    match = re.search(
        r"export const MANAGED_KB_TAG_KEYS\s*=\s*\{(.*?)\}\s*as const", body, re.DOTALL
    )
    if not match:
        match = re.search(r"export const MANAGED_KB_TAG_KEYS\s*=\s*\{(.*?)^\};", body, re.DOTALL | re.MULTILINE)
    assert match, "could not find MANAGED_KB_TAG_KEYS in the construct"
    return dict(re.findall(r"(\w+):\s*'([^']+)'", match.group(1)))


class TestPythonIsInternallyConsistent:
    """The writer and the filter must derive from one implementation, not mirror
    each other. ``tombstones.project_tag_filter`` was documented as a *mirror* of
    ``provisioning.build_tags`` and had drifted from it."""

    def test_the_filter_is_a_subset_of_what_is_written(self, monkeypatch):
        monkeypatch.setenv(canonical.ENV_TAG_VALUE_PREFIX, "bsu-agentcore")
        monkeypatch.setenv(canonical.ENV_TAG_VALUE_ENVIRONMENT, "prod")

        written = canonical.build_tags("ast-1", "user-1")
        expected = canonical.project_tag_filter()

        for key, value in expected.items():
            assert written[key] == value, f"filter expects {key}={value!r}, writer wrote {written.get(key)!r}"

    def test_provisioning_and_tombstones_resolve_identically(self, monkeypatch):
        """Both delegate now; this asserts the delegation rather than the values."""
        from apis.shared.kb_backend import provisioning, tombstones

        monkeypatch.setenv(canonical.ENV_TAG_VALUE_PREFIX, "some-prefix")
        monkeypatch.setenv(canonical.ENV_TAG_VALUE_ENVIRONMENT, "some-env")

        written = provisioning.build_tags("ast-1", "user-1")
        expected = tombstones.project_tag_filter()

        assert tombstones.matches_project_tags(written, expected) is True

    def test_a_knowledge_base_from_another_environment_does_not_match(self, monkeypatch):
        """Both scope keys are required. A knowledge base carrying our project
        prefix but another environment's tag belongs to that environment."""
        from apis.shared.kb_backend import tombstones

        monkeypatch.setenv(canonical.ENV_TAG_VALUE_PREFIX, "shared-prefix")
        monkeypatch.setenv(canonical.ENV_TAG_VALUE_ENVIRONMENT, "prod")
        theirs = canonical.build_tags("ast-1", "user-1")

        monkeypatch.setenv(canonical.ENV_TAG_VALUE_ENVIRONMENT, "dev")
        ours = tombstones.project_tag_filter()

        assert tombstones.matches_project_tags(theirs, ours) is False

    def test_untagged_never_matches(self):
        from apis.shared.kb_backend import tombstones

        assert tombstones.matches_project_tags({}, {"ManagedKbPrefix": "p"}) is False
        assert tombstones.matches_project_tags(None, {"ManagedKbPrefix": "p"}) is False
        assert canonical.matches_project(None) is False


class TestValueResolution:
    def test_the_construct_supplied_variable_wins(self, monkeypatch):
        monkeypatch.setenv(canonical.ENV_TAG_VALUE_PREFIX, "from-construct")
        monkeypatch.setenv("PROJECT_PREFIX", "from-fallback")
        assert canonical.tag_prefix() == "from-construct"

    def test_the_fallback_is_used_when_the_primary_is_absent(self, monkeypatch):
        """So a local run, and the App API which receives ``PROJECT_PREFIX`` but not
        the tag variables, still resolve to the right scope."""
        monkeypatch.delenv(canonical.ENV_TAG_VALUE_PREFIX, raising=False)
        monkeypatch.setenv("PROJECT_PREFIX", "from-fallback")
        assert canonical.tag_prefix() == "from-fallback"

    def test_an_explicit_argument_beats_the_environment(self, monkeypatch):
        monkeypatch.setenv(canonical.ENV_TAG_VALUE_PREFIX, "from-env")
        assert canonical.tag_prefix("explicit") == "explicit"

    def test_the_writer_and_the_filter_share_one_fallback_chain(self, monkeypatch):
        """An asymmetric fallback is how a writer and a reader disagree while both
        look correct — which is exactly what happened."""
        monkeypatch.delenv(canonical.ENV_TAG_VALUE_PREFIX, raising=False)
        monkeypatch.delenv(canonical.ENV_TAG_VALUE_ENVIRONMENT, raising=False)
        monkeypatch.setenv("PROJECT_PREFIX", "p")
        monkeypatch.setenv("ENVIRONMENT", "e")

        written = canonical.build_tags("ast-1", "user-1")
        assert canonical.matches_project(written) is True

    def test_the_last_resort_default_is_warned_about(self, monkeypatch, caplog):
        """Two deployments that both reach the default share a tag scope and will
        each treat the other's knowledge bases as their own."""
        for name in (
            canonical.ENV_TAG_VALUE_PREFIX,
            *canonical.FALLBACK_PREFIX_VARS,
        ):
            monkeypatch.delenv(name, raising=False)

        import logging

        with caplog.at_level(logging.WARNING):
            assert canonical.tag_prefix() == canonical.DEFAULT_PREFIX

        assert any("falling back" in record.message for record in caplog.records)


class TestTheOwnerTagCarriesNoPii:
    """Requirement 20.12. Unlike a database column, a tag cannot be scrubbed
    retroactively from the audit trail it has already entered."""

    @pytest.mark.parametrize("owner", ["a@b.test", "First.Last@example.edu"])
    def test_an_email_is_refused_rather_than_trimmed(self, owner):
        with pytest.raises(ValueError, match="opaque"):
            canonical.build_tags("ast-1", owner)

    def test_an_opaque_id_is_accepted(self):
        written = canonical.build_tags("ast-1", "u-8f3c1e")
        assert written[canonical.TAG_KEY_OWNER_USER_ID] == "u-8f3c1e"


class TestTheCdkConstructAgrees:
    def test_the_construct_declares_the_same_key_names(self):
        """The mismatch that started this. The construct declared
        ``ManagedKbPrefix`` while the Python wrote ``prefix``, and nothing read the
        construct's declaration, so neither side was wrong on its own."""
        ts_keys = _ts_tag_keys()

        assert ts_keys.get("PREFIX") == canonical.TAG_KEY_PREFIX
        assert ts_keys.get("ENVIRONMENT") == canonical.TAG_KEY_ENVIRONMENT
        assert ts_keys.get("APP_KB_ID") == canonical.TAG_KEY_APP_KB_ID
        assert ts_keys.get("OWNER_USER_ID") == canonical.TAG_KEY_OWNER_USER_ID

    def test_the_construct_exports_the_variables_the_python_reads(self):
        """Exporting a variable nothing reads is how the correct values sat unused
        while the defaults were written into AWS."""
        body = CONSTRUCT.read_text(encoding="utf-8")

        assert f"{canonical.ENV_TAG_VALUE_PREFIX}:" in body, (
            f"the construct does not set {canonical.ENV_TAG_VALUE_PREFIX}, so the "
            f"provisioning Lambda would fall back to a default"
        )
        assert f"{canonical.ENV_TAG_VALUE_ENVIRONMENT}:" in body

    def test_the_construct_supplies_real_values_not_literals(self):
        """`config.projectPrefix`, not a hardcoded string — the whole point is that
        two deployments differ."""
        body = CONSTRUCT.read_text(encoding="utf-8")
        line = next(
            line for line in body.splitlines() if f"{canonical.ENV_TAG_VALUE_PREFIX}:" in line
        )
        assert "config." in line, f"prefix tag value is not derived from config: {line.strip()}"


class TestTheEnvironmentTagValueIsPlumbedNotGuessed:
    """The gap the key-name tests above could not see.

    Every existing test here checks that the three languages agree on the tag
    *keys*, and that the Python writer and Python filter share one fallback
    chain. Nothing checked that the value the **CDK computes** and the value the
    **teardown script defaults to** agree — and they did not.

    `managedKbEnvironmentTagValue` falls back to `production ? 'prod' : 'nonprod'`;
    `config.production` defaults to `true` because `platform.yml` never passed a
    production flag and `cdk.context.json` says `true`. So a *development* deploy
    tagged its knowledge bases `ManagedKbEnvironment=prod`, while
    `scripts/teardown/managed-kb.sh` falls back to `dev`. Teardown would have
    matched nothing and reported a clean run, leaving billed Bedrock knowledge
    bases behind — the exact symptom of the original tag-contract drift, one
    layer up and invisible to the tests written for it.

    The fix is to stop relying on either fallback: the value is passed explicitly
    per environment. These tests guard that plumbing end to end, because an
    omission anywhere in the chain silently reinstates the guess.
    """

    WORKFLOW = REPO_ROOT / ".github/workflows/platform.yml"
    LOAD_ENV = REPO_ROOT / "scripts/common/load-env.sh"
    CONFIG_TS = REPO_ROOT / "infrastructure/lib/config.ts"

    def test_the_workflow_passes_an_environment_tag_value(self):
        body = self.WORKFLOW.read_text(encoding="utf-8")
        assert "CDK_TAG_ENVIRONMENT:" in body, (
            "platform.yml does not pass CDK_TAG_ENVIRONMENT, so config.tags.Environment "
            "is unset and the construct falls back to the production guess"
        )
        assert "vars.CDK_TAG_ENVIRONMENT" in body, (
            "CDK_TAG_ENVIRONMENT is declared but not sourced from an environment "
            "variable, so it cannot differ between development and production"
        )

    def test_load_env_forwards_it_as_the_flat_dotted_context_key(self):
        """`--context a.b=c` sets `context['a.b']`, never nested `a.b`."""
        body = self.LOAD_ENV.read_text(encoding="utf-8")
        assert "CDK_TAG_ENVIRONMENT" in body, "load-env.sh does not export the variable"
        assert '--context tags.Environment=' in body, (
            "load-env.sh does not forward the value to CDK, so the workflow variable "
            "is exported and then dropped"
        )

    def test_config_reads_the_flat_key_and_not_only_the_nested_object(self):
        """A nested-only read is how the two earlier flat-key defects worked."""
        body = self.CONFIG_TS.read_text(encoding="utf-8")
        assert "tryGetContext('tags.Environment')" in body, (
            "config.ts reads only the nested `tags` object, so --context "
            "tags.Environment=... is silently ignored"
        )

    def test_the_shell_fallback_is_not_the_constructs_fallback(self):
        """Documents the divergence rather than pretending it is gone.

        Both fallbacks still exist and still disagree; they are simply no longer
        reached in a deployed environment. Asserting the disagreement keeps the
        reason the plumbing is mandatory visible — if someone later deletes the
        plumbing, the tests above fail and this one explains why it matters.
        """
        construct = CONSTRUCT.read_text(encoding="utf-8")
        script = (REPO_ROOT / "scripts/teardown/managed-kb.sh").read_text(encoding="utf-8")

        assert "'prod' : 'nonprod'" in construct, (
            "the construct's documented fallback changed; re-check that it now "
            "agrees with the teardown script's, or that neither is reachable"
        )
        assert "CDK_ENVIRONMENT:-dev" in script, (
            "the teardown script's documented fallback changed; re-check the pair"
        )


class TestTheTeardownScriptAgrees:
    def test_the_script_matches_the_canonical_key_names(self):
        """It previously matched ``prefix``/``env`` — keys nothing ever wrote."""
        body = TEARDOWN.read_text(encoding="utf-8")

        assert f'TAG_KEY_PREFIX="{canonical.TAG_KEY_PREFIX}"' in body
        assert f'TAG_KEY_ENVIRONMENT="{canonical.TAG_KEY_ENVIRONMENT}"' in body

    def test_the_script_reads_the_same_variables_in_the_same_order(self):
        """The variable *and* the order: a script that consulted the fallback first
        would disagree with the Python on any host where both are set."""
        body = TEARDOWN.read_text(encoding="utf-8")

        prefix_line = next(line for line in body.splitlines() if line.startswith("PREFIX="))
        env_line = next(line for line in body.splitlines() if line.startswith("ENVIRONMENT="))

        assert canonical.ENV_TAG_VALUE_PREFIX in prefix_line
        assert canonical.ENV_TAG_VALUE_ENVIRONMENT in env_line

        # Primary before fallback.
        assert prefix_line.index(canonical.ENV_TAG_VALUE_PREFIX) < prefix_line.index(
            canonical.FALLBACK_PREFIX_VARS[0]
        )

    def test_the_script_no_longer_reads_the_cdk_only_variables_first(self):
        """`CDK_PROJECT_PREFIX` is set at deploy time by `load-env.sh` and is *not*
        set in a Lambda, so reading it first is how the teardown and the writer
        ended up with different scopes."""
        body = TEARDOWN.read_text(encoding="utf-8")
        prefix_line = next(line for line in body.splitlines() if line.startswith("PREFIX="))

        assert not prefix_line.startswith('PREFIX="${CDK_PROJECT_PREFIX'), (
            "the teardown script reads CDK_PROJECT_PREFIX before the variable the "
            "provisioning code writes from"
        )
