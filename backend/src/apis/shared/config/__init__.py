"""Process-level configuration helpers shared by app-api, inference-api and agents.

Keep this package import-light: ``inference_api/main.py`` imports it before
anything else, so nothing here may pull in boto3, FastAPI or any module that
reads the environment at import time.
"""

from apis.shared.config.runtime_environment import (
    ACCOUNT_ID_ENV,
    PROJECT_PREFIX_ENV,
    DerivedVariable,
    EnvironmentReport,
    audit_derived_environment,
    derived_environment_manifest,
    hydrate_derived_environment,
    log_environment_report,
    resolve_account_id,
)

__all__ = [
    "ACCOUNT_ID_ENV",
    "PROJECT_PREFIX_ENV",
    "DerivedVariable",
    "EnvironmentReport",
    "audit_derived_environment",
    "derived_environment_manifest",
    "hydrate_derived_environment",
    "log_environment_report",
    "resolve_account_id",
]
