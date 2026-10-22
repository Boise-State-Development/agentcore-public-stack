"""MIME policy for skill bundle files — the one place that decides what a
skill resource may be and how it is allowed to leave the API.

Why this module exists
----------------------
A skill resource is **content one user uploads and another user's browser
downloads**, and app-api is served from the *same origin* as the Angular SPA
(the CloudFront ``/api/*`` behavior). That makes an uploaded file a potential
top-level document on the SPA's own origin: if the response says
``Content-Type: text/html`` and ``Content-Disposition: inline``, the browser
parses it as a document and any ``<script>`` in it runs with the *viewer's*
session — the viewer's cookies, the viewer's CSRF token, the viewer's
privileges. An unprivileged author uploading ``payload.html`` and getting an
admin to open it is full privilege escalation, not a display bug.

Two controls, applied at both ends, so neither one is load-bearing alone:

**Write side** — :func:`resolve_upload_content_type` derives the stored media
type from the *filename extension against an allowlist* and **ignores the
client-supplied multipart ``Content-Type`` entirely**. A caller cannot label
their bytes, and cannot upload an extension that is not on the list. This is
the same posture the agent-icon route already takes (sniff the bytes, ignore
the header) — see ``apis.shared.assistants.icons``.

**Read side** — :func:`safe_download_content_type` and
:func:`resource_download_headers` re-derive the served type from the filename
at *serve* time and force ``attachment`` + ``nosniff`` + a no-op CSP. This is
what protects rows written before the allowlist existed: a stored
``content_type`` of ``text/html`` is never reflected back to a browser, so
already-uploaded payloads are neutralized without a data migration.

No entry in the allowlist is a media type a browser will execute as script in
the page's origin. Notably absent:

* ``.html`` / ``.htm`` / ``.xhtml`` / ``.xml`` / ``.svg`` — parsed as documents
  and can carry inline script. SVG is on this list for the same reason as HTML:
  ``image/svg+xml`` navigated to top-level is a scriptable document.
* ``.js`` / ``.mjs`` / ``.css`` as *active* types — the extensions are allowed
  (a skill may legitimately ship example code) but map to ``text/plain``, so
  the bytes are readable and inert. This matches spec D5: ``script`` resources
  are stored, listed, and never executed.

Both tiers (admin catalog and user-authored "My Skills") and the runtime's
authoring tools funnel their writes through ``SkillBundleFiles.add_resource``
(``apis.shared.skills.bundle_files``), so applying the policy there covers all
of them. Enforced by tests/apis/app_api/skills/test_skill_resource_mime.py.
"""

from __future__ import annotations

import os
from typing import Dict, Final

from apis.shared.files.content_disposition import build_content_disposition

# Extension → the media type we store and serve. Every value is inert: no
# entry is parsed as a scriptable document by any browser. Source-code
# extensions deliberately collapse to ``text/plain`` (readable, never
# executable) rather than their "correct" active type.
SAFE_EXTENSION_CONTENT_TYPES: Final[Dict[str, str]] = {
    # Prose / structured text — the bread and butter of a skill bundle.
    "md": "text/markdown",
    "markdown": "text/markdown",
    "txt": "text/plain",
    "text": "text/plain",
    "csv": "text/csv",
    "tsv": "text/tab-separated-values",
    "json": "application/json",
    "jsonl": "application/json",
    "yaml": "application/yaml",
    "yml": "application/yaml",
    "toml": "text/plain",
    "ini": "text/plain",
    "cfg": "text/plain",
    "env": "text/plain",
    "log": "text/plain",
    "rst": "text/plain",
    "tex": "text/plain",
    # Example / helper code. Stored inert (D5): text/plain, not an active type.
    "py": "text/plain",
    "sh": "text/plain",
    "bash": "text/plain",
    "zsh": "text/plain",
    "ps1": "text/plain",
    "rb": "text/plain",
    "pl": "text/plain",
    "r": "text/plain",
    "js": "text/plain",
    "mjs": "text/plain",
    "cjs": "text/plain",
    "ts": "text/plain",
    "jsx": "text/plain",
    "tsx": "text/plain",
    "css": "text/plain",
    "sql": "text/plain",
    "graphql": "text/plain",
    "java": "text/plain",
    "kt": "text/plain",
    "go": "text/plain",
    "rs": "text/plain",
    "c": "text/plain",
    "h": "text/plain",
    "cpp": "text/plain",
    "hpp": "text/plain",
    "cs": "text/plain",
    "swift": "text/plain",
    "php": "text/plain",
    "lua": "text/plain",
    "dockerfile": "text/plain",
    "diff": "text/plain",
    "patch": "text/plain",
    # Documents.
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    # Raster images only. SVG is excluded on purpose — see the module docstring.
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
    "bmp": "image/bmp",
    "ico": "image/vnd.microsoft.icon",
    # Archives (a bundle may ship sample data).
    "zip": "application/zip",
    "gz": "application/gzip",
    "tar": "application/x-tar",
}

# What a resource is served as when its extension is not on the allowlist.
# Only reachable for rows written before the allowlist existed, and only on the
# read path, which pairs it with ``attachment`` + ``nosniff``.
FALLBACK_CONTENT_TYPE: Final[str] = "application/octet-stream"

# Response headers that make a skill-resource body undownloadable *as a
# document*, whatever its bytes or its stored type claim to be:
#
#   nosniff  — the browser must honor our Content-Type and must not re-sniff
#              ``<html>`` bytes into ``text/html``.
#   CSP      — ``default-src 'none'`` + ``sandbox`` means that even if a
#              browser did treat the body as a document, it has no script,
#              no fetch, and an opaque origin, so it cannot touch the SPA's
#              session. ``frame-ancestors 'none'`` keeps it out of frames.
#   no-store — these are per-user private files behind a session cookie;
#              nothing on the path (CloudFront included) should retain them.
#
# Which of these the browser actually receives depends on how the response is
# reached, and BOTH paths are real:
#
#   Direct ALB / local dev — the ALB is `internetFacing: true`, and a
#     localhost:4200 SPA talks to the dev backend directly. There is no edge
#     policy on this path, so this dict IS the browser's only CSP and
#     ``sandbox`` is the live control it claims to be.
#
#   Behind CloudFront — the `/api/*` behavior's `ApiResponseHeadersPolicy`
#     (infrastructure/lib/constructs/spa/spa-distribution-construct.ts) sets
#     its CSP with `override: true`, so it REPLACES this header and the
#     browser sees `default-src 'none'; frame-ancestors 'none'` without
#     ``; sandbox``. That is deliberate, not a regression: the edge policy is
#     an origin-wide backstop covering every `/api/*` response — attachment
#     bodies and the top-level OAuth login navigation included — where a bare
#     `sandbox` would force an opaque origin for no real gain, because
#     `default-src 'none'` already blocks every script that an opaque origin
#     would be protecting the session from. `infrastructure/test/
#     api-security-headers.test.ts` pins that string and asserts the omission
#     is intentional, so the two layers cannot drift silently.
#
# Do not "fix" the difference by dropping `override: true`: that would turn
# the edge backstop into a mere default, letting any future route, middleware
# or error handler define the CSP for the whole origin.
RESOURCE_SECURITY_HEADERS: Final[Dict[str, str]] = {
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; frame-ancestors 'none'; sandbox"
    ),
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}

# The upload-side filename guard in ``SkillBundleFiles`` already forbids path
# separators, so a header value built from a validated filename cannot break
# out. Belt-and-braces for the read path, which also runs against legacy rows:
# ``build_content_disposition`` collapses anything outside its safe set before
# it reaches a header. See :func:`resource_download_headers`.


class SkillResourceTypeError(ValueError):
    """An upload whose filename is not an allowed skill-resource type.

    A ``ValueError`` subclass so the existing route mappers
    (``_skill_value_error`` / ``_resource_value_error``) turn it into a 400
    with this message, which names the extension the author actually sent.
    """


def resource_extension(filename: str) -> str:
    """Return a filename's lowercase extension without the dot ('' if none).

    ``Dockerfile`` (and any other extension-less name whose stem is itself a
    known type key) resolves through its stem, so a bundle can ship one.
    """
    name = os.path.basename(filename or "").strip()
    stem, _, ext = name.rpartition(".")
    if not stem:  # no dot at all — try the whole name as a type key
        return name.lower() if name.lower() in SAFE_EXTENSION_CONTENT_TYPES else ""
    return ext.lower()


def is_allowed_resource_filename(filename: str) -> bool:
    """True when ``filename``'s extension is on the allowlist."""
    return resource_extension(filename) in SAFE_EXTENSION_CONTENT_TYPES


def resolve_upload_content_type(filename: str) -> str:
    """The media type to store for an upload, derived from its filename.

    The client-supplied multipart ``Content-Type`` is deliberately **not** a
    parameter: it is attacker-controlled and was the write half of the stored-XSS
    chain this module exists to close.

    Raises:
        SkillResourceTypeError: The extension is missing or not allowed. The
            message names what was sent so an author can rename or convert.
    """
    ext = resource_extension(filename)
    content_type = SAFE_EXTENSION_CONTENT_TYPES.get(ext)
    if content_type is None:
        sent = f"'.{ext}'" if ext else "no recognized extension"
        raise SkillResourceTypeError(
            f"File type not allowed for skill resources ({sent}). "
            "Skill files must be documents, data, images, or plain-text code — "
            "web documents such as .html, .htm, .xhtml, .xml and .svg are not "
            "accepted because they can execute script in a reader's browser. "
            f"Allowed extensions: {', '.join(sorted(SAFE_EXTENSION_CONTENT_TYPES))}."
        )
    return content_type


# Media types whose bytes are text an agent can read or write inline. Shared by
# the runtime's ``read_skill_file`` (what it returns as text rather than
# describing) and ``UserSkillService.write_text_file`` (what it will accept as
# text), so the two cannot disagree about which files are text.
_TEXT_CONTENT_TYPES: Final[frozenset] = frozenset(
    {
        "application/json",
        "application/xml",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
        "application/javascript",
    }
)


def is_text_content_type(content_type: str) -> bool:
    """True if ``content_type`` is one an agent handles inline as text."""
    ct = (content_type or "").split(";", 1)[0].strip().lower()
    return ct.startswith("text/") or ct in _TEXT_CONTENT_TYPES


def safe_download_content_type(filename: str) -> str:
    """The media type to *serve*, re-derived from the filename at read time.

    Never returns the stored ``content_type``. Rows written before the upload
    allowlist existed can carry ``text/html``; reflecting that is exactly the
    bug. An unrecognized extension serves as ``application/octet-stream``.
    """
    return SAFE_EXTENSION_CONTENT_TYPES.get(
        resource_extension(filename), FALLBACK_CONTENT_TYPE
    )


def resource_download_headers(filename: str) -> Dict[str, str]:
    """Full response headers for serving one skill resource's bytes.

    ``attachment``, not ``inline``: both callers of the read routes are the SPA
    fetching the body over XHR (``responseType: 'text'``) to render it in an
    in-app viewer, so nothing legitimate depends on the browser rendering this
    URL as a document — and ``attachment`` is what stops a hand-shared link
    from becoming a top-level document on the SPA's origin.

    The name is reduced to its basename *first* — the read path also runs
    against legacy rows written before the upload-side separator guard, so a
    stored ``../../etc/passwd`` must not suggest itself as a save path. What
    is left goes through the shared builder, which emits the sanitized ASCII
    ``filename`` plus an RFC 5987 ``filename*``. The ``filename*`` is
    percent-encoded ASCII, so it cannot break out of the header either, and
    it changes only the name the browser *saves under* — ``attachment``,
    ``nosniff`` and the inert CSP above are what keep the bytes from being
    treated as a document, and none of them depend on the filename.
    """
    return {
        "Content-Disposition": build_content_disposition(
            "attachment", os.path.basename(filename or "") or "resource"
        ),
        **RESOURCE_SECURITY_HEADERS,
    }
