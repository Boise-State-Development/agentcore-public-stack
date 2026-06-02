"""
Container env var contract verification.

Cross-references every env var the CDK constructs set against every env
var the Python code actually reads. Catches the bug class that bit us
in commits 3fd8cddd and 902f5de7 — silent name typos on either side
(e.g., CDK setting `DYNAMODB_EVENTS_TABLE` while Python reads
`DYNAMODB_QUOTA_EVENTS_TABLE`) where TypeScript and CDK tests both
pass cleanly because IAM/CFN don't validate string values, and the
runtime container just falls back to defaults silently.

The test is intentionally strict: every CDK-set env var name must
either:
  1. Be read by at least one Python file (`os.environ.get`,
     `os.environ[...]`, `os.getenv`, or referenced via the
     `EnvVars` constants class in
     `backend/src/agents/main_agent/config/constants.py`), OR
  2. Be in the explicit `INTENTIONAL_NOT_READ_BY_PYTHON` allow-list
     below with a comment explaining why.

If you add a new env var to a CDK construct, either add a corresponding
`os.environ.get(NAME, ...)` in Python or add the name to the allow-list
with a one-line justification.

Run with: pytest tests/supply_chain/test_env_var_contract.py -v
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CDK_LIB = REPO_ROOT / "infrastructure" / "lib"
PY_SRC = REPO_ROOT / "backend" / "src"

# CDK construct files that set container/Lambda env vars. If a new
# construct adds an env block, list it here.
CDK_ENV_FILES = [
    CDK_LIB / "constructs" / "app-api" / "app-api-environment.ts",
    CDK_LIB / "constructs" / "app-api" / "app-api-service-construct.ts",
    CDK_LIB / "constructs" / "inference-api" / "inference-agentcore-construct.ts",
    CDK_LIB / "constructs" / "artifacts" / "artifact-render-lambda-construct.ts",
    CDK_LIB / "constructs" / "rag-ingestion" / "rag-ingestion-lambda-construct.ts",
]

# CDK-set env vars the Python code does not read.
#
# The deal: if you add a name here, document WHY. The two acceptable
# reasons are:
#   - "documents available config; intentional fallback if Python
#     starts reading it later" (e.g., COGNITO_ISSUER_URL — useful as
#     a record of the configured issuer; harmless to set unread)
#   - "set on container but only consumed inside container by other
#     CDK-injected fallback paths" (rare)
#
# Anything else means a typo or a missing Python reader. Fix the bug,
# don't add the name here.
INTENTIONAL_NOT_READ_BY_PYTHON: dict[str, str] = {
    # Could be useful for auditing or future code; currently dead but
    # harmless to set.
    "AGENTCORE_MEMORY_TYPE": "documents memory backend choice; not currently read by python",
    "COGNITO_ISSUER_URL": "documents configured issuer; not currently read by python",
    "OAUTH_CLIENT_SECRETS_ARN": "documents configured ARN; not currently read by python",
    "OAUTH_TOKEN_ENCRYPTION_KEY_ARN": "documents configured KMS key; not currently read by python",
}

# Python env vars CDK is *not* expected to set (tunable knobs with
# explicit defaults, AWS-injected vars, dev-only flags). The test
# does not enforce CDK to set every Python-read env var — most have
# sensible defaults and aren't required at runtime. We only enforce
# the reverse direction (no orphan CDK env vars).
#
# Reference list maintained for human review only; not used in any
# assertion.

ENV_DICT_KEY_RE = re.compile(
    r"""
    ^                          # start of line (no leading non-whitespace)
    \s+                        # indent
    ([A-Z][A-Z0-9_]+)          # KEY
    \s*:                       # colon
    """,
    re.VERBOSE | re.MULTILINE,
)

PY_ENV_RE = re.compile(
    r"""
    os\.(?:environ\.get|getenv|environ\[) # os.environ.get / os.getenv / os.environ[
    \s*\(?\s*                              # optional whitespace + optional (
    ['"]                                   # opening quote
    ([A-Z][A-Z0-9_]+)                      # NAME
    ['"]                                   # closing quote
    """,
    re.VERBOSE,
)

# The EnvVars class in constants.py defines indirected env var
# references like:
#   class EnvVars:
#       DYNAMODB_QUOTA_EVENTS_TABLE = "DYNAMODB_QUOTA_EVENTS_TABLE"
ENV_VARS_CLASS_RE = re.compile(
    r"""
    ^                                   # line start (in MULTILINE)
    \s+                                 # indent
    [A-Z][A-Z0-9_]*                     # field name
    \s*=\s*                             # =
    ['"]                                # opening quote
    ([A-Z][A-Z0-9_]+)                   # ENV VAR NAME
    ['"]                                # closing quote
    """,
    re.VERBOSE | re.MULTILINE,
)


def extract_python_env_vars() -> set[str]:
    """Every env var name referenced anywhere under backend/src.

    Strategy: any ALL_CAPS_WITH_UNDERSCORES identifier that appears
    inside string literals (single or double quotes) in any .py
    file is treated as a potentially-read env var name. False
    positives (things that look like env var names but aren't) are
    acceptable — the only consequence is that an actually-orphan
    CDK env var might be missed if it happens to share its name with
    some unrelated string elsewhere. The cost of false positives is
    low; the cost of false negatives (missing an orphan) is the
    bug class this whole test exists to prevent.

    This catches:
      • os.environ.get("NAME") / os.getenv("NAME") / os.environ["NAME"]
      • CONST = "NAME"  (e.g., _CALLBACK_URL_ENV = "AGENTCORE_LOCAL_OAUTH_CALLBACK_URL")
      • EnvVars.MEMORY_ID = "AGENTCORE_MEMORY_ID"
      • Names referenced inside docstrings / comments / log messages

    Identifier filter: must start with uppercase letter, contain only
    uppercase letters / digits / underscores, length >= 6 to avoid
    matching short tokens like "AWS" or "API".
    """
    QUOTED_IDENT_RE = re.compile(r"""['"]([A-Z][A-Z0-9_]{5,})['"]""")
    names: set[str] = set()
    for py_file in PY_SRC.rglob("*.py"):
        text = py_file.read_text(encoding="utf-8")
        for m in QUOTED_IDENT_RE.finditer(text):
            names.add(m.group(1))
    return names


def extract_cdk_env_vars_from_file(ts_file: Path) -> set[str]:
    """Every env var key set in a CDK env block.

    Walks the file and collects identifier-shaped object keys
    (ALL_CAPS_WITH_UNDERSCORES) that appear at the start of a line
    after object indentation. This catches both the inline-dict shape
    used by `environment: { KEY: value, ... }` and the
    `environment['KEY'] = value;` / `params.environment['KEY'] = ...`
    shape used by code that builds env vars incrementally.
    """
    text = ts_file.read_text(encoding="utf-8")
    names: set[str] = set()

    # Inline dict keys: KEY: ...
    for m in ENV_DICT_KEY_RE.finditer(text):
        names.add(m.group(1))

    # environment['KEY'] = ...
    bracket_re = re.compile(r"environment\[['\"]([A-Z][A-Z0-9_]+)['\"]\]\s*=")
    for m in bracket_re.finditer(text):
        names.add(m.group(1))

    return names


def extract_all_cdk_env_vars() -> dict[str, set[str]]:
    """Map of {ts file → set of env vars it sets}."""
    result: dict[str, set[str]] = {}
    for f in CDK_ENV_FILES:
        if f.exists():
            result[str(f.relative_to(REPO_ROOT))] = extract_cdk_env_vars_from_file(f)
    return result


# Non-env-var keys that look like ALL_CAPS but aren't env vars
# (these slip through the inline-dict regex because TypeScript object
# literals don't distinguish env vars from other typed config).
NOT_ENV_VARS = {
    # AppConfig field paths surfaced via const props
    "TaskDefinition",
    # Argument/property names that match the shape but aren't env vars
}


@pytest.fixture(scope="module")
def python_env_vars() -> set[str]:
    return extract_python_env_vars()


@pytest.fixture(scope="module")
def cdk_env_vars_by_file() -> dict[str, set[str]]:
    return extract_all_cdk_env_vars()


def test_python_env_vars_extracted_sanity(python_env_vars):
    """Sanity: we found a non-trivial number of Python env var names.
    If this drops below 50, the regex broke."""
    assert len(python_env_vars) > 50, (
        f"Expected >50 Python env vars, found {len(python_env_vars)}. "
        "Regex extractor likely broken."
    )


def test_cdk_env_vars_extracted_sanity(cdk_env_vars_by_file):
    """Sanity: we found env vars in each CDK file."""
    for f, names in cdk_env_vars_by_file.items():
        assert len(names) > 0, f"No env vars extracted from {f} — regex broken or file empty"


def test_no_orphan_cdk_env_vars(python_env_vars, cdk_env_vars_by_file):
    """Every env var the CDK sets must be either:
      1. Read by at least one Python file, OR
      2. In INTENTIONAL_NOT_READ_BY_PYTHON with a documented reason.

    This catches the bug class introduced in commits 3fd8cddd and
    902f5de7 where I (the prior agent) renamed env vars on the CDK
    side without checking what Python actually reads — for example
    setting `DYNAMODB_EVENTS_TABLE` when Python reads
    `DYNAMODB_QUOTA_EVENTS_TABLE`. TypeScript compiled clean, IAM
    accepted any string, and the broken state only surfaced when a
    quota event tried to write to the default table at runtime.
    """
    all_cdk = set()
    for names in cdk_env_vars_by_file.values():
        all_cdk |= names

    orphans = all_cdk - python_env_vars - set(INTENTIONAL_NOT_READ_BY_PYTHON.keys()) - NOT_ENV_VARS

    if orphans:
        # Build a helpful failure message: for each orphan, suggest
        # the closest match in python_env_vars (likely typo).
        from difflib import get_close_matches

        lines = [
            f"\n{len(orphans)} CDK-set env var(s) are not read by any Python file:",
        ]
        for name in sorted(orphans):
            close = get_close_matches(name, python_env_vars, n=1, cutoff=0.7)
            hint = f" (did you mean {close[0]}?)" if close else ""
            lines.append(f"  - {name}{hint}")
        lines.append(
            "\nFix by either:"
            "\n  • Adding os.environ.get('NAME', ...) in the relevant Python file"
            "\n  • Renaming the CDK env var to match what Python actually reads"
            "\n  • Adding the name to INTENTIONAL_NOT_READ_BY_PYTHON in this test"
            "\n    with a comment explaining why it's set unread."
        )
        pytest.fail("\n".join(lines))


def test_intentional_dead_list_has_no_strays(python_env_vars):
    """If a name in INTENTIONAL_NOT_READ_BY_PYTHON is now actually read
    by Python, it should be removed from the allow-list (the comment
    is misleading). This keeps the allow-list honest."""
    strays = set(INTENTIONAL_NOT_READ_BY_PYTHON.keys()) & python_env_vars
    assert strays == set(), (
        f"These env vars are in INTENTIONAL_NOT_READ_BY_PYTHON but ARE read by Python: "
        f"{sorted(strays)}. Remove them from the allow-list."
    )


def test_intentional_dead_list_has_no_unset_names(cdk_env_vars_by_file):
    """If a name in INTENTIONAL_NOT_READ_BY_PYTHON is no longer set by
    CDK, it should be removed from the allow-list (it's not an
    'intentional dead' anymore — it's just an old comment)."""
    all_cdk = set()
    for names in cdk_env_vars_by_file.values():
        all_cdk |= names
    strays = set(INTENTIONAL_NOT_READ_BY_PYTHON.keys()) - all_cdk
    assert strays == set(), (
        f"These env vars are in INTENTIONAL_NOT_READ_BY_PYTHON but are no longer "
        f"set by CDK: {sorted(strays)}. Remove them from the allow-list."
    )
