"""The conversation archive: one S3 object per turn, the search index's source.

See ``docs/specs/conversation-search.md`` §3–§4. ``documents`` cuts turns out of
a conversation; ``store`` writes and deletes them. Writes are gated by
``apis.shared.feature_flags.conversation_index_enabled``; deletes are not.
"""

from apis.shared.conversation_archive.documents import (
    ARCHIVE_PREFIX,
    MAX_TURN_TEXT_BYTES,
    MAX_USER_TEXT_BYTES,
    ArchivedTurn,
    archive_key,
    build_turn,
    clean_user_text,
    is_turn_start,
    last_turn_start,
    message_text,
    session_prefix,
    split_turns,
    with_user_text,
)
from apis.shared.conversation_archive.store import (
    archive_bucket_name,
    delete_session_archive,
    drain_pending,
    put_turns,
    reset_bucket_cache,
    schedule,
    write_turns,
)

__all__ = [
    "ARCHIVE_PREFIX",
    "MAX_TURN_TEXT_BYTES",
    "MAX_USER_TEXT_BYTES",
    "ArchivedTurn",
    "archive_bucket_name",
    "archive_key",
    "build_turn",
    "clean_user_text",
    "delete_session_archive",
    "drain_pending",
    "is_turn_start",
    "last_turn_start",
    "message_text",
    "put_turns",
    "reset_bucket_cache",
    "schedule",
    "session_prefix",
    "split_turns",
    "with_user_text",
    "write_turns",
]
