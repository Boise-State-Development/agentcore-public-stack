"""The largest single file a knowledge base accepts, by engine.

One number per engine, read by every path that adds a file to a knowledge base:
the presigned upload (``POST /assistants/{id}/documents/upload-url``, and the
project files route that delegates to it), a file-source import, and the managed
ingestion consumer, which holds the stored object to it before submitting it to
Bedrock. The SPA reads the same number off the documents list (``kbUsage``), so
the dropzone, the error and the server agree.

* **Managed** (a Bedrock knowledge base): ``KB_UPLOAD_MAX_FILE_BYTES``, default
  50 MiB. Bedrock's own per-document limit for a text or document file is 50 MB
  and is not adjustable, so a larger value is clamped to it rather than letting
  Bedrock refuse the file after its bytes are stored and reserved.
* **Classic** (Docling + Titan into S3 Vectors): 20 MiB, the processing limit the
  rag-ingestion handler enforces (``MAX_FILE_SIZE_MB`` in
  ``apis/app_api/documents/ingestion/handler.py``). That Lambda already runs at its
  900 s ceiling, and a 20 MB file is documented as taking 5+ minutes, so this is
  not raised. ``KB_UPLOAD_MAX_FILE_BYTES`` can lower it, never raise it.

Stdlib only: this module ships in the kb-migration Lambda image, which carries
``kb_backend`` and ``kb_migration`` and nothing else.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

ENV_MAX_FILE_BYTES = "KB_UPLOAD_MAX_FILE_BYTES"

#: Default per-file limit for a managed knowledge base.
DEFAULT_MANAGED_MAX_FILE_BYTES = 50 * 1024 * 1024

#: Bedrock Knowledge Bases' per-document ceiling for text and document files
#: (docs: "Each file size doesn't exceed the quota of 50 MB"; Service Quotas lists
#: it as not adjustable). A configured value above it is clamped here.
BEDROCK_MAX_FILE_BYTES = 50 * 1024 * 1024

#: The classic pipeline's processing limit. Must match ``MAX_FILE_SIZE_MB`` in the
#: rag-ingestion handler, which enforces it on its own (a test pins the two).
CLASSIC_MAX_FILE_BYTES = 20 * 1024 * 1024


def configured_max_file_bytes() -> int:
    """``KB_UPLOAD_MAX_FILE_BYTES`` as configured, before any engine's ceiling.

    Unset, empty (an unset GitHub variable arrives as ``""``), non-numeric or
    non-positive values fall back to the default.
    """
    raw = (os.environ.get("KB_UPLOAD_MAX_FILE_BYTES") or "").strip()
    if not raw:
        return DEFAULT_MANAGED_MAX_FILE_BYTES
    try:
        value = int(raw)
    except ValueError:
        logger.warning(
            f"{ENV_MAX_FILE_BYTES}={raw!r} is not an integer; using "
            f"{DEFAULT_MANAGED_MAX_FILE_BYTES}"
        )
        return DEFAULT_MANAGED_MAX_FILE_BYTES
    if value <= 0:
        logger.warning(
            f"{ENV_MAX_FILE_BYTES}={value} is not positive; using "
            f"{DEFAULT_MANAGED_MAX_FILE_BYTES}"
        )
        return DEFAULT_MANAGED_MAX_FILE_BYTES
    return value


def max_file_bytes(managed: bool) -> int:
    """The per-file limit for a knowledge base on this engine."""
    configured = configured_max_file_bytes()
    ceiling = BEDROCK_MAX_FILE_BYTES if managed else CLASSIC_MAX_FILE_BYTES
    return min(configured, ceiling)


def format_limit(limit_bytes: int) -> str:
    """``52428800`` → ``"50 MB"``: the number the SPA shows, in the same units."""
    mb = limit_bytes / (1024 * 1024)
    return f"{mb:.0f} MB" if mb == int(mb) else f"{mb:.1f} MB"


def too_large_message(limit_bytes: int, size_bytes: Optional[int] = None) -> str:
    """The one sentence every refusal uses, so the SPA and the row agree."""
    base = f"Files can be up to {format_limit(limit_bytes)}."
    if size_bytes is None:
        return base
    return f"{base} This one is {format_limit(size_bytes)}."


class FileTooLarge(Exception):
    """A file is over its knowledge base's per-file limit."""

    def __init__(self, size_bytes: int, limit_bytes: int) -> None:
        self.size_bytes = size_bytes
        self.limit_bytes = limit_bytes
        super().__init__(too_large_message(limit_bytes, size_bytes))
