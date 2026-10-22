"""Sync policy models — scheduled re-index of assistant knowledge-base sources.

A SyncPolicy is the single source of truth for "this content source resyncs".
It is inert data: nothing fires unless the dispatcher reads it from the
DueSyncIndex, so deleting the record is total revocation of the schedule.

Stored in the assistants table using the adjacency list pattern:
    PK: AST#{assistant_id}
    SK: SYNCPOL#{policy_id}

GSI4 (DueSyncIndex) keys are present ONLY while state == "active" (sparse
index) — a paused policy is physically invisible to the dispatcher, not
filtered at query time.

A ``drive_folder`` policy (docs/specs/assistant-kb-sync.md §11) covers every
file in one Drive folder: its ``source_ref`` is ``{connector_id}:{folder_id}``
(:func:`folder_source_ref`), and it also carries the folder's display path,
the files someone removed from the knowledge base, and its last run's counts.
"""

from typing import List, Literal, Optional, Tuple

from pydantic import BaseModel, ConfigDict, Field

SyncSourceType = Literal["web_crawl", "drive_file", "drive_folder"]
SyncInterval = Literal["daily", "weekly", "monthly"]
SyncPolicyState = Literal["active", "paused_error", "paused_inactive", "paused_reauth", "paused_user"]
SyncRunResult = Literal["changed", "unchanged", "failed", "skipped"]

# Sentinel partition key for the sparse due index. Single logical partition —
# fine at our scale; shard to SYNCDUE#{0..N} if writes ever demand it.
DUE_INDEX_PK = "SYNCDUE"


def folder_source_ref(connector_id: str, folder_id: str) -> str:
    """A ``drive_folder`` policy's ``source_ref``: the connector, then the folder."""
    return f"{connector_id}:{folder_id}"


def parse_folder_source_ref(source_ref: str) -> Optional[Tuple[str, str]]:
    """``(connector_id, folder_id)`` from a ``drive_folder`` ``source_ref``, or ``None``.

    Split on the LAST ``:``: Drive folder ids never contain one, whatever a
    connector id holds.
    """
    connector_id, sep, folder_id = source_ref.rpartition(":")
    if not sep or not connector_id or not folder_id:
        return None
    return connector_id, folder_id


class FolderRunSummary(BaseModel):
    """What one ``drive_folder`` run did, as counts only (never names).

    ``listed`` counts every file the walk saw; ``unchanged``/``updated``/
    ``added`` the accepted files by outcome; ``removed_from_source`` the
    documents newly marked gone from the folder; ``failed`` the files a
    download or stage failed for; ``skipped`` files over the per-file size
    cap; ``deferred`` files a per-run cap left for the next run;
    ``truncated`` that the walk stopped at its own cap.
    """

    model_config = ConfigDict(populate_by_name=True)

    listed: int = 0
    unchanged: int = 0
    updated: int = 0
    added: int = 0
    removed_from_source: int = Field(0, alias="removedFromSource")
    failed: int = 0
    skipped: int = 0
    deferred: int = 0
    truncated: bool = False


class SyncPolicy(BaseModel):
    """Complete sync policy model (internal use)"""

    model_config = ConfigDict(populate_by_name=True, extra="allow")

    policy_id: str = Field(..., alias="policyId", description="Sync policy identifier (syn-{12-hex})")
    assistant_id: str = Field(..., alias="assistantId", description="Parent assistant identifier")
    source_type: SyncSourceType = Field(..., alias="sourceType", description="Kind of content source this policy re-syncs")
    source_ref: str = Field(
        ...,
        alias="sourceRef",
        description=(
            "web_crawl: crawl_id of the CrawlJob to re-run; drive_file: document_id holding the import provenance; "
            "drive_folder: {connector_id}:{folder_id}"
        ),
    )
    source_name: Optional[str] = Field(
        None, alias="sourceName", description="drive_folder: the folder's path, for display only (never logged)"
    )
    interval: SyncInterval = Field(..., description="Re-sync cadence (bounded enum — no cron)")
    state: SyncPolicyState = Field("active", description="Lifecycle state; only 'active' policies appear in DueSyncIndex")
    state_reason: Optional[str] = Field(None, alias="stateReason", description="Human-readable reason for a paused state")
    next_sync_at: Optional[str] = Field(None, alias="nextSyncAt", description="ISO 8601 next due time; drives DueSyncIndex sort key")
    last_sync_at: Optional[str] = Field(None, alias="lastSyncAt", description="ISO 8601 timestamp of the last completed run")
    last_result: Optional[SyncRunResult] = Field(None, alias="lastResult", description="Outcome of the last completed run")
    consecutive_failures: int = Field(0, alias="consecutiveFailures", description="Transient-failure streak (circuit breaker input)")
    consecutive_not_found: int = Field(0, alias="consecutiveNotFound", description="Source-gone streak (404 fast path input)")
    sync_run_started_at: Optional[str] = Field(
        None, alias="syncRunStartedAt", description="Set while a worker run is in flight; stale stamps are treated as crashed runs"
    )
    last_manual_run_at: Optional[str] = Field(
        None, alias="lastManualRunAt", description="Last 'Sync now' trigger; enforces the manual-run cooldown"
    )
    excluded_file_ids: Optional[List[str]] = Field(
        None,
        alias="excludedFileIds",
        description="drive_folder: provider file ids someone removed from the knowledge base; never re-imported",
    )
    last_run_summary: Optional[FolderRunSummary] = Field(
        None, alias="lastRunSummary", description="drive_folder: counts from the last completed run"
    )
    created_by_user_id: str = Field(..., alias="createdByUserId", description="User whose credentials background fetches use")
    created_at: str = Field(..., alias="createdAt", description="ISO 8601 timestamp of creation")
    updated_at: str = Field(..., alias="updatedAt", description="ISO 8601 timestamp of last update")
