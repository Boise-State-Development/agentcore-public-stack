"""Build a `Content-Disposition` header that survives a non-ASCII filename.

Two separate hazards make a raw f-string interpolation unsafe here:

* **Latin-1 encoding.** Starlette encodes response headers as latin-1, so an
  emoji, a CJK character, a curly quote, or the U+202F narrow no-break space
  macOS puts in screenshot names raises `UnicodeEncodeError` *while the
  response is being written* — the client sees a 500, not a download. S3 is
  the same story on the presigned-URL side: it 400s a
  `response-content-disposition` it cannot encode as latin-1.
* **Header injection.** A filename carrying `"`, CR, or LF would otherwise
  break out of the quoted-string and forge header lines.

`build_content_disposition` answers both by emitting the RFC 6266 pair: a
sanitized ASCII `filename` that any client can read, plus an RFC 5987
`filename*` that carries the real UTF-8 name percent-encoded (so it is ASCII
on the wire). Modern browsers prefer `filename*` and save the true name;
anything older falls back to the ASCII form instead of failing.
"""

from __future__ import annotations

import re
from urllib.parse import quote

# Anything outside this set collapses to '_' in the ASCII fallback. The class
# is an allowlist on purpose: it rules out non-latin-1 bytes, the quote and
# backslash that would escape the quoted-string, and the CR/LF that would
# forge a header line, without having to enumerate them.
_SAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._ -]+")

# Keeps the header comfortably inside server header-size limits and matches
# what most filesystems accept for a single component.
_MAX_BASE_LEN = 120

_DEFAULT_BASE = "download"


def _split_extension(filename: str) -> tuple[str, str]:
    """Split a trailing `.ext` off so truncation never eats the extension.

    Only a short, alphanumeric trailing segment counts as an extension — a
    name like ``Notes. Final thoughts`` keeps its tail as part of the base.
    """
    base, dot, ext = filename.rpartition(".")
    if dot and base and ext.isalnum() and len(ext) <= 8:
        return base, f".{ext}"
    return filename, ""


def build_content_disposition(disposition_type: str, filename: str) -> str:
    """Return a `Content-Disposition` value that is always latin-1 encodable.

    Args:
        disposition_type: ``"attachment"`` or ``"inline"``.
        filename: The desired filename *including* its extension. May contain
            any Unicode; the caller does not need to pre-sanitize it.

    Returns:
        e.g. ``attachment; filename="Research-Notes.zip";
        filename*=UTF-8''Research%20%F0%9F%A7%A0%20Notes.zip``
    """
    name = (filename or "").strip() or _DEFAULT_BASE
    base, ext = _split_extension(name)

    ascii_base = _SAFE_FILENAME_CHARS.sub("_", base).strip(" ._")[:_MAX_BASE_LEN]
    ascii_name = f"{ascii_base or _DEFAULT_BASE}{ext}"

    utf8_name = quote(name, safe="")
    return (
        f"{disposition_type}; filename=\"{ascii_name}\"; "
        f"filename*=UTF-8''{utf8_name}"
    )
