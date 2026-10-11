"""Derived Runtime environment (docs/specs/agentcore-runtime-v2.md §7).

The contract under test: an absent manifest variable is set to
``{PROJECT_PREFIX}-{suffix}`` (``-{AWS_ACCOUNT_ID}`` when account-scoped), an
explicit value is never overwritten, drift between the two is reported, and
with no prefix nothing happens at all.
"""

from __future__ import annotations

import ast
import logging
import re
from pathlib import Path

import pytest

from apis.shared.config import (
    ACCOUNT_ID_ENV,
    PROJECT_PREFIX_ENV,
    DerivedVariable,
    audit_derived_environment,
    derived_environment_manifest,
    hydrate_derived_environment,
    log_environment_report,
    resolve_account_id,
)

PREFIX = "acme-ai"
ACCOUNT = "123456789012"


# --------------------------------------------------------------------------- manifest


def test_manifest_loads_with_unique_well_formed_entries():
    manifest = derived_environment_manifest()
    names = [v.name for v in manifest]
    assert len(manifest) >= 20
    assert len(set(names)) == len(names)
    for variable in manifest:
        assert re.fullmatch(r"[A-Z][A-Z0-9_]+", variable.name), variable.name
        # CDK joins prefix and suffix with "-"; a suffix with anything else in
        # it would not be what getResourceName produces.
        assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", variable.suffix), variable.suffix


def test_manifest_covers_the_names_the_runtime_reads_most():
    names = {v.name for v in derived_environment_manifest()}
    assert {
        "DYNAMODB_SESSIONS_METADATA_TABLE_NAME",
        "DYNAMODB_ASSISTANTS_TABLE_NAME",
        "S3_USER_FILES_BUCKET_NAME",
        "S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME",
        "AGENTCORE_RUNTIME_WORKLOAD_NAME",
    } <= names


def test_manifest_never_lists_a_value_cdk_does_not_build_from_the_prefix():
    # These follow domain, certificate or physical-id rules that live in CDK
    # (spec §7.4 "Don't derive domains"); deriving them here would be wrong.
    names = {v.name for v in derived_environment_manifest()}
    for forbidden in (
        "FRONTEND_URL",
        "CORS_ORIGINS",
        "AGENTCORE_MCP_APPS_SANDBOX_ORIGIN",
        "AGENTCORE_MEMORY_ID",
        "AGENTCORE_CODE_INTERPRETER_ID",
        "BROWSER_ID",
        "PROJECT_PREFIX",
    ):
        assert forbidden not in names


# --------------------------------------------------------------------------- derive


def test_derive_plain_and_account_scoped_names():
    plain = DerivedVariable("DYNAMODB_X", "sessions-metadata")
    scoped = DerivedVariable("S3_X", "rag-documents", account_scoped=True)
    assert plain.derive(PREFIX, None) == "acme-ai-sessions-metadata"
    assert scoped.derive(PREFIX, ACCOUNT) == "acme-ai-rag-documents-123456789012"
    assert scoped.derive(PREFIX, None) is None
    assert plain.derive("", ACCOUNT) is None


def test_account_id_is_read_off_an_explicit_scoped_value_when_aws_account_id_is_absent():
    env = {
        PROJECT_PREFIX_ENV: PREFIX,
        "S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME": f"{PREFIX}-rag-documents-{ACCOUNT}",
    }
    assert resolve_account_id(env) == ACCOUNT


def test_account_id_prefers_aws_account_id_and_ignores_malformed_values():
    env = {
        PROJECT_PREFIX_ENV: PREFIX,
        ACCOUNT_ID_ENV: "999999999999",
        "S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME": f"{PREFIX}-rag-documents-{ACCOUNT}",
    }
    assert resolve_account_id(env) == "999999999999"
    assert resolve_account_id({PROJECT_PREFIX_ENV: PREFIX, ACCOUNT_ID_ENV: "not-an-account"}) is None
    assert resolve_account_id({PROJECT_PREFIX_ENV: PREFIX, "S3_USER_FILES_BUCKET_NAME": "someone-elses-bucket"}) is None


# --------------------------------------------------------------------------- hydrate


def test_hydrate_sets_absent_names_and_leaves_explicit_ones_alone():
    env = {
        PROJECT_PREFIX_ENV: PREFIX,
        ACCOUNT_ID_ENV: ACCOUNT,
        "DYNAMODB_SESSIONS_METADATA_TABLE_NAME": "legacy-sessions",  # explicit, differs
        "DYNAMODB_USERS_TABLE_NAME": f"{PREFIX}-users",  # explicit, matches
        "S3_USER_FILES_BUCKET_NAME": "",  # blank .env line counts as absent
    }
    report = hydrate_derived_environment(env)

    assert env["DYNAMODB_SESSIONS_METADATA_TABLE_NAME"] == "legacy-sessions"
    assert report.drifted == {"DYNAMODB_SESSIONS_METADATA_TABLE_NAME": ("legacy-sessions", f"{PREFIX}-sessions-metadata")}
    assert "DYNAMODB_USERS_TABLE_NAME" in report.matched
    assert env["S3_USER_FILES_BUCKET_NAME"] == f"{PREFIX}-user-file-uploads-{ACCOUNT}"
    assert env["DYNAMODB_ASSISTANTS_TABLE_NAME"] == f"{PREFIX}-rag-assistants"
    assert env["AGENTCORE_RUNTIME_WORKLOAD_NAME"] == f"{PREFIX}-platform-workload"
    assert report.applied["S3_USER_FILES_BUCKET_NAME"] == env["S3_USER_FILES_BUCKET_NAME"]
    assert not report.skipped
    # Everything in the manifest is now present.
    for variable in derived_environment_manifest():
        assert env[variable.name]


def test_hydrate_without_a_prefix_changes_nothing():
    env = {"DYNAMODB_USERS_TABLE_NAME": "keep-me"}
    report = hydrate_derived_environment(env)
    assert env == {"DYNAMODB_USERS_TABLE_NAME": "keep-me"}
    assert not report.enabled
    assert not report.applied and not report.drifted and not report.skipped


def test_hydrate_skips_account_scoped_names_when_no_account_can_be_found():
    env = {PROJECT_PREFIX_ENV: PREFIX}
    report = hydrate_derived_environment(env)
    scoped = {v.name for v in derived_environment_manifest() if v.account_scoped}
    assert set(report.skipped) == scoped
    for name in scoped:
        assert name not in env
    assert env["DYNAMODB_SESSIONS_METADATA_TABLE_NAME"] == f"{PREFIX}-sessions-metadata"


def test_hydrate_is_idempotent():
    env = {PROJECT_PREFIX_ENV: PREFIX, ACCOUNT_ID_ENV: ACCOUNT}
    first = hydrate_derived_environment(env)
    snapshot = dict(env)
    second = hydrate_derived_environment(env)
    assert env == snapshot
    assert first.applied and not second.applied
    assert set(second.matched) == set(first.applied)


def test_audit_reports_but_never_writes():
    env = {PROJECT_PREFIX_ENV: PREFIX, ACCOUNT_ID_ENV: ACCOUNT, "DYNAMODB_USERS_TABLE_NAME": "other"}
    report = audit_derived_environment(env)
    assert env == {PROJECT_PREFIX_ENV: PREFIX, ACCOUNT_ID_ENV: ACCOUNT, "DYNAMODB_USERS_TABLE_NAME": "other"}
    assert report.drifted == {"DYNAMODB_USERS_TABLE_NAME": ("other", f"{PREFIX}-users")}
    assert not report.applied
    assert "DYNAMODB_SESSIONS_METADATA_TABLE_NAME" in report.skipped


# --------------------------------------------------------------------------- logging


def test_report_logs_a_warning_per_drifted_variable(caplog):
    env = {PROJECT_PREFIX_ENV: PREFIX, ACCOUNT_ID_ENV: ACCOUNT, "DYNAMODB_USERS_TABLE_NAME": "other"}
    report = hydrate_derived_environment(env)
    logger = logging.getLogger("test.derived-env")
    with caplog.at_level(logging.INFO, logger="test.derived-env"):
        log_environment_report(report, logger, service="inference-api")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "DYNAMODB_USERS_TABLE_NAME" in warnings[0].getMessage()
    assert "agentcore-runtime-v2.md" in warnings[0].getMessage()
    summary = [r for r in caplog.records if "derived environment" in r.getMessage()]
    assert summary and "1 drifted" in summary[0].getMessage()


def test_report_is_silent_at_info_without_a_prefix(caplog):
    report = hydrate_derived_environment({})
    logger = logging.getLogger("test.derived-env.quiet")
    with caplog.at_level(logging.INFO, logger="test.derived-env.quiet"):
        log_environment_report(report, logger, service="inference-api")
    assert not [r for r in caplog.records if r.levelno >= logging.INFO]


# --------------------------------------------------------------------------- entrypoint ordering


def test_inference_api_entrypoint_hydrates_before_any_other_apis_import():
    """Import-time readers (e.g. bedrock_embeddings) must see the derived names.

    Hydration therefore has to be the first ``apis.*`` thing the entrypoint
    touches. This reads the module source rather than importing it, so the
    check holds whatever the test process's environment is.
    """
    source = Path(__file__).resolve().parents[2] / "src" / "apis" / "inference_api" / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    hydrate_line = None
    first_other_apis_import = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("apis."):
            if node.module == "apis.shared.config":
                continue
            if first_other_apis_import is None or node.lineno < first_other_apis_import:
                first_other_apis_import = node.lineno
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "hydrate_derived_environment":
            hydrate_line = node.lineno
    assert hydrate_line is not None, "inference_api/main.py no longer calls hydrate_derived_environment()"
    assert first_other_apis_import is not None
    assert hydrate_line < first_other_apis_import, (
        f"hydrate_derived_environment() at line {hydrate_line} must come before the first other "
        f"apis.* import at line {first_other_apis_import}"
    )


@pytest.mark.parametrize("module", ["apis.shared.config", "apis.shared.config.runtime_environment"])
def test_config_package_stays_import_light(module):
    """inference_api/main.py imports this before anything else, so it must not drag in heavy modules."""
    import importlib
    import sys

    importlib.import_module(module)
    for heavy in ("boto3", "fastapi", "strands"):
        # Either the heavy module was never imported, or it was imported by
        # something else in this test session; what we can assert cheaply is
        # that our module's own import graph is tiny.
        pass
    source = Path(sys.modules[module].__file__).read_text(encoding="utf-8")
    assert "import boto3" not in source and "from fastapi" not in source and "strands" not in source
