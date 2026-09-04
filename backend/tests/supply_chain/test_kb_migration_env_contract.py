"""The migration Lambdas' environment contract, across Python and TypeScript.

Every environment variable the four handlers read must actually be set by
`kb-migration-construct.ts`, and the construct must not publish
kb-migration-specific variables nothing reads.

WHY THIS FILE EXISTS

The feature shipped four wiring mismatches of the same shape — code that reviews
cleanly, deploys cleanly, and does nothing:

1. `register_backend` was defined and called by nothing, so a promoted record
   raised `BackendUnavailable`.
2. Nothing wrote a record into `shadow`, so the worker and dispatcher were both
   correct and inert.
3. `MANAGED_KB_MIGRATION_ENABLED` was set on the Lambdas but not on the App API
   task that gates the upgrade offer, so the card rendered nothing anywhere.
4. The construct set `MANAGED_KB_WORKER_FUNCTION_NAME` while the dispatcher read
   `KB_MIGRATION_WORKER_FUNCTION_NAME`. Every tick raised
   `RuntimeError: KB_MIGRATION_WORKER_FUNCTION_NAME is not set` and no knowledge
   base could ever migrate. Its sibling `KB_SYNC_WORKER_FUNCTION_NAME` works
   precisely because `kb-sync.test.ts` asserts it.

The tag-contract tests already do this for tag *keys*. This does it for the
environment, which is the other half of the same seam: two files in two
languages that must agree on a string, with nothing but a schedule to tell you
they don't.

Feature: managed-kb-migration
Requirements: 15.14, 15.11, 24.x
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
HANDLER_DIR = REPO_ROOT / "backend/src/apis/app_api/kb_migration"
KB_BACKEND_DIR = REPO_ROOT / "backend/src/apis/shared/kb_backend"
CONSTRUCT = (
    REPO_ROOT / "infrastructure/lib/constructs/managed-kb/kb-migration-construct.ts"
)

#: Variables the handlers read that the construct is NOT required to set. Every
#: entry is a deliberate exemption with a reason, not a backlog — adding one
#: should require explaining why absence is correct.
OPTIONAL_OVERRIDES = {
    # ── Provided by the Lambda runtime itself ──
    "AWS_REGION",
    "AWS_DEFAULT_REGION",
    # ── Optional tuning overrides with an in-code default ──
    # Bounded sweep size, defaults to 20 (Requirement 15.14); a wider sweep
    # should require repeated observed ticks, not a variable edit.
    "KB_MIGRATION_DISPATCH_LIMIT",
    # Reconciler safety valves, all defaulted in reconciler.py.
    "MANAGED_KB_ORPHAN_MIN_AGE_HOURS",
    "MANAGED_KB_RECONCILER_MAX_DELETIONS",
    "MANAGED_KB_RECONCILER_MAX_SCANNED",
    # Throttle for the last-retrieved write, defaulted in idleness.py.
    "KB_LAST_RETRIEVED_THROTTLE_HOURS",
    # Namespace override; metrics.py derives one from the project prefix.
    "MANAGED_KB_METRIC_NAMESPACE",
    # ── Documented fallback links, never the primary ──
    # tags.py's FALLBACK_PREFIX_VARS. The construct sets the primary,
    # MANAGED_KB_TAG_VALUE_PREFIX, which is asserted by the tag-contract tests.
    "PROJECT_PREFIX",
    "CDK_PROJECT_PREFIX",
    "ENVIRONMENT",
    "CDK_ENVIRONMENT",
    # ── Read by a module outside these Lambdas' import closure ──
    # resource_policy.py serves the app-side sharing path and takes account_id
    # as a parameter, with this as a fallback. It is not reachable from any of
    # the four handlers (see test_lambda_image_imports.py's closure walk). If a
    # handler ever does reach it, this exemption must go and the construct must
    # set the variable — the module raises rather than guessing.
    "AWS_ACCOUNT_ID",
    "MANAGED_KB_RETRIEVAL_PRINCIPAL_ARNS",
}

#: Variables read by code *outside* this construct's Lambdas. The App API task
#: sets its own copies (see app-api-environment.ts).
READ_ELSEWHERE = {
    "MANAGED_KB_PER_OWNER_DEFAULT_BYTES",
    "MANAGED_KB_PER_OWNER_ELEVATED_BYTES",
    "MANAGED_KB_PER_KB_CEILING_BYTES",
}

#: Published by the construct and deliberately not read at runtime. Pinned so
#: the "unread" sweep still fails for anything NEW, which is how the
#: worker-function-name mismatch would have been caught.
KNOWN_UNREAD = {
    # The tag KEY names. `tags.py` owns them as Python constants; the construct
    # exports them so `test_kb_tag_contract.py` can compare the two languages
    # without either side reading the other's copy at runtime. Removing them
    # would blind that contract test.
    "MANAGED_KB_TAG_KEY_APP_KB_ID",
    "MANAGED_KB_TAG_KEY_ENVIRONMENT",
    "MANAGED_KB_TAG_KEY_OWNER_USER_ID",
    "MANAGED_KB_TAG_KEY_PREFIX",
    # "New knowledge bases are created managed" is design §14.7 steps 5-8, a
    # follow-up spec. Nothing in backend/src reads this, so setting it changes
    # nothing — which is worth knowing before someone flips it expecting an
    # effect.
    "MANAGED_KB_NEW_DEFAULT",
}

_ENV_READ = re.compile(r'os\.environ(?:\.get)?\(?\s*["\']([A-Z][A-Z0-9_]+)["\']')
_ENV_CONST = re.compile(r'=\s*["\']((?:KB_MIGRATION|MANAGED_KB)_[A-Z0-9_]+)["\']')
_TS_KEY = re.compile(r"^\s*([A-Z][A-Z0-9_]+):", re.MULTILINE)


def _python_sources():
    """The handlers plus the kb_backend modules they reach.

    kb_backend is included because the Lambdas run its code — a variable read in
    `tags.py` is just as much part of this contract as one read in `worker.py`.
    """
    return sorted(HANDLER_DIR.glob("*.py")) + sorted(KB_BACKEND_DIR.glob("*.py"))


def _names_read() -> set:
    names = set()
    for path in _python_sources():
        body = path.read_text(encoding="utf-8")
        names |= set(_ENV_READ.findall(body))
        # Constants like FLAG_MIGRATION_ENABLED = "MANAGED_KB_MIGRATION_ENABLED"
        names |= set(_ENV_CONST.findall(body))
    return names


def _names_set_by_construct() -> set:
    return set(_TS_KEY.findall(CONSTRUCT.read_text(encoding="utf-8")))


class TestTheConstructSetsWhatTheHandlersRead:
    def test_the_sources_are_where_we_think(self):
        """Guards the test itself: a moved file would make it vacuously pass."""
        assert CONSTRUCT.exists(), CONSTRUCT
        assert len(list(HANDLER_DIR.glob("*.py"))) >= 4, "expected four handlers"
        assert _names_read(), "parsed no environment reads at all"

    def test_every_required_variable_is_set(self):
        missing = sorted(_names_read() - _names_set_by_construct() - OPTIONAL_OVERRIDES)
        assert not missing, (
            "the migration Lambdas read these variables but the construct never "
            f"sets them, so each falls back or raises at runtime: {missing}\n"
            "Either set them in kb-migration-construct.ts or, if absence is "
            "genuinely correct, add them to OPTIONAL_OVERRIDES with a reason."
        )

    def test_the_worker_function_name_is_wired(self):
        """The specific failure that stopped every migration.

        Pinned by name rather than left to the sweep above, because this one is
        not a degraded default — the dispatcher raises, so nothing migrates at
        all, on a fifteen-minute schedule, in silence unless someone reads the
        logs.
        """
        assert "KB_MIGRATION_WORKER_FUNCTION_NAME" in _names_set_by_construct(), (
            "the dispatcher cannot find the worker; every tick will raise "
            "RuntimeError and no knowledge base will ever migrate"
        )

    def test_the_retention_window_reaches_the_worker(self):
        """Requirement 15.11's window must be the operator's, not the code's floor."""
        assert "KB_MIGRATION_RETAIN_DAYS" in _names_set_by_construct(), (
            "worker._retain_days() reads KB_MIGRATION_RETAIN_DAYS; without it the "
            "configured retention window is silently replaced by the 30-day floor"
        )

    def test_no_kb_migration_variable_is_published_unread(self):
        """A variable set and read by nothing is how a mismatch hides.

        The `MANAGED_KB_WORKER_FUNCTION_NAME` that broke this feature looked
        correct in the construct and in review; it was wrong only in that nothing
        consumed it. An unread variable is therefore treated as a defect, not as
        harmless.
        """
        published = {
            name
            for name in _names_set_by_construct()
            if name.startswith(("KB_MIGRATION_", "MANAGED_KB_"))
        }
        unread = sorted(published - _names_read() - READ_ELSEWHERE - KNOWN_UNREAD)
        assert not unread, (
            f"the construct publishes these but nothing reads them: {unread}\n"
            "Most likely a name mismatch with the Python, which is exactly how "
            "the dispatcher shipped unable to find its worker."
        )

    @pytest.mark.parametrize(
        "name",
        [
            "MANAGED_KB_MIGRATION_ENABLED",
            "MANAGED_KB_RECONCILER_ARMED",
            "MANAGED_KB_SERVICE_ROLE_ARN",
            "S3_ASSISTANTS_DOCUMENTS_BUCKET_NAME",
        ],
    )
    def test_known_load_bearing_variables_stay_wired(self, name):
        """Spot-pins for variables whose absence is silent rather than loud."""
        assert name in _names_set_by_construct()


# ---------------------------------------------------------------------------
# The poll budget must fit inside the Lambda that runs it
# ---------------------------------------------------------------------------
class TestTheIngestionPollBudgetFitsTheLambdaTimeout:
    """A wait longer than the timeout is a killed invocation, not a wait.

    The consumer waits for Bedrock to finish indexing inside a single invocation,
    because Lambda's asynchronous retry is capped at 2 attempts and cannot be
    extended — so redelivery spans only minutes and a slow document would
    dead-letter. That makes the in-invocation budget load-bearing, and it now lives
    in two files that have no compiler between them: the timeout in the CDK
    construct and the poll constants in Python.

    Raise either past the other and a slow-but-succeeding document is killed
    mid-wait and dead-lettered — which is exactly the failure this budget was
    introduced to remove. Hence a test rather than a comment.
    """

    def _lambda_timeout_minutes(self) -> int:
        import re

        text = CONSTRUCT.read_text(encoding="utf-8")
        # The consumer's own timeout, not another function's: anchor on its
        # construct id and read the first timeout that follows.
        start = text.index("KbIngestionConsumerLambda'")
        match = re.search(r"timeout:\s*cdk\.Duration\.minutes\((\d+)\)", text[start:])
        assert match, "could not find the ingestion consumer's timeout in the construct"
        return int(match.group(1))

    def test_the_poll_budget_leaves_headroom_under_the_lambda_timeout(self):
        from apis.app_api.kb_migration import ingestion_consumer as ic

        budget = ic.INDEXED_POLL_TIMEOUT_SECONDS + ic.RETRIEVABLE_POLL_TIMEOUT_SECONDS
        timeout = self._lambda_timeout_minutes() * 60

        assert budget < timeout, (
            f"the consumer can wait {budget:.0f}s but its Lambda times out at "
            f"{timeout}s — a slow document would be killed mid-wait and "
            f"dead-lettered, which is the bug this budget exists to prevent"
        )
        # Headroom for the ingest call, the S3 read and cold start.
        assert timeout - budget >= 120, (
            f"only {timeout - budget:.0f}s of headroom between the poll budget and "
            f"the Lambda timeout; leave at least 120s for the ingest call itself"
        )

    def test_the_budget_covers_the_measured_indexing_tail(self):
        """264 s was the slowest PDF in the §5.1 benchmark; dev saw 5 m 30 s.

        Pinned as a literal rather than compared to a constant, because asserting a
        constant against itself proves nothing. This number is a property of
        Bedrock's indexing behaviour, not a knob.
        """
        from apis.app_api.kb_migration import ingestion_consumer as ic

        assert ic.INDEXED_POLL_TIMEOUT_SECONDS >= 330, (
            "the budget no longer covers the 5 m 30 s indexing time observed in dev "
            "for a 1.5 MB PDF"
        )
