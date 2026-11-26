"""Which arm of the prewarm-prepare experiment a conversation is in.

Pure and stdlib only, so app-api, the Runtime and the readout can all name the
same arm without a lookup.

The arm is keyed on the conversation, never the send, so every send in a
conversation is in one arm and a turn's timing is never compared across arms.
The salt is versioned (``prepare-holdout-v1``) so a later experiment can
re-randomize by changing it.
"""

from __future__ import annotations

import hashlib

ARM_TREATED = "treated"
ARM_HOLDOUT = "holdout"

_SALT = "prepare-holdout-v1:"


def prepare_arm(session_id: str, holdout_percent: int) -> str:
    """``ARM_HOLDOUT`` for ``holdout_percent`` of conversations, else ``ARM_TREATED``."""
    bucket = int(hashlib.sha256((_SALT + session_id).encode()).hexdigest()[:8], 16) % 100
    return ARM_HOLDOUT if bucket < holdout_percent else ARM_TREATED
