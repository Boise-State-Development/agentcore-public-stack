"""The code defaults for task models: what each task runs while no admin has assigned one.

docs/specs/admin-model-assignments.md §3. These are the platform's last resort, used when no
assignment exists, when the stored one no longer passes its checks (row deleted, disabled,
retired with no successor), or when the settings item cannot be read. A task never fails
because its setting is missing.

They are kept here, and nowhere else, so that the ids to watch for an AWS retirement notice
are in one file. ``tests/shared/test_task_models.py`` pins each one, and the admin Task models
tab shows whether each has a catalog row. Without a row, the catalog's own retirement
redirect cannot reach it.

No imports on purpose: ``agents/main_agent/config/constants.py`` reads one of these, so this
module must stay a leaf that nothing can make circular.
"""

#: Conversation titles. Fast and cheap: one call per new conversation, concurrent with the stream.
SESSION_TITLE_DEFAULT_MODEL_ID = "us.amazon.nova-micro-v1:0"

#: One-line summaries of finished tool batches, concurrent with the stream.
TOOL_SUMMARY_DEFAULT_MODEL_ID = "us.amazon.nova-micro-v1:0"

#: Compaction's rolling summary. Measured: Nova Micro kept ~77% of planted facts, Nova 2 Lite ~99%.
COMPACTION_SUMMARY_DEFAULT_MODEL_ID = "us.amazon.nova-2-lite-v1:0"
