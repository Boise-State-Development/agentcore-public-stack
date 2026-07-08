"""DynamoDB-safe (de)serialization for Agent binding/model config payloads.

DynamoDB rejects Python ``float`` on write and returns ``Decimal`` on read. The Agent
record's ``modelConfig.params`` (temperature, top_p, …) and binding ``config`` blobs are
free-form, so any float nested in them must round-trip through ``Decimal``. Mirrors the
established pattern in ``apis/shared/sessions/metadata.py`` — kept here so the assistants
service persistence path (Phase 1 PR-2) has a single, tested helper.
"""

from decimal import Decimal
from typing import Any


def to_ddb_safe(obj: Any) -> Any:
    """Recursively convert floats to ``Decimal`` for a DynamoDB write."""
    if isinstance(obj, bool):
        # bool is a subclass of int — leave it alone (Decimal(str(True)) would raise).
        return obj
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: to_ddb_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [to_ddb_safe(v) for v in obj]
    return obj


def from_ddb(obj: Any) -> Any:
    """Recursively convert ``Decimal`` back to native numbers after a DynamoDB read.

    Integral decimals become ``int``; the rest become ``float`` — so ``maxTokens`` reads
    back as ``4096`` (int), not ``4096.0``.
    """
    if isinstance(obj, Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    if isinstance(obj, dict):
        return {k: from_ddb(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [from_ddb(v) for v in obj]
    return obj
