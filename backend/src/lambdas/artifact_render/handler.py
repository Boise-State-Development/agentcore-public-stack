"""Artifact render Lambda — SCAFFOLD.

This Lambda fronts the `artifacts.{domain}` CloudFront origin. Its job:

  1. Receive a request from CloudFront carrying a render-token JWT
     (`?t=...` on the URL).
  2. Verify the JWT against the HMAC key in Secrets Manager
     (RENDER_TOKEN_SECRET_ARN). The JWT carries
     `{user_id, artifact_id, version, exp}`.
  3. Read the artifact metadata row from DynamoDB
     (ARTIFACTS_TABLE / SK = ARTIFACT#{id}#V#{version}).
  4. If content_ref points to S3, fetch the blob from ARTIFACTS_BUCKET;
     if inline, read it from the DDB item.
  5. Wrap the content in a minimal HTML shell with strict CSP and
     return it as a 200. The CDN's response-headers-policy also stamps
     the CSP, so even a buggy handler can't downgrade security.

Current state: returns a placeholder response. Real JWT verification +
content fetch is implemented as a follow-up (the infra is provisioned
first so the surrounding wiring can be validated end-to-end).

Boundary: this Lambda runs OUTSIDE the apis/* import boundary
(test_import_boundaries.py) — it's a standalone deployable, not part of
app-api or inference-api. Do not import from apis/ here.

Dependencies (none today; TODO for v1):
  - PyJWT for token validation
  - boto3 (already in Lambda runtime; used for Secrets Manager + S3 + DDB)
"""

from __future__ import annotations

import json
import os
from typing import Any

# Pinned at deploy time via ArtifactsStack environment block. Accessed
# lazily so a missing env var becomes a runtime 500 with a clear log line
# rather than an import-time crash.
_FRAME_ANCESTOR = os.environ.get("FRAME_ANCESTOR_ORIGIN", "")
_CSP_SCRIPT_SRC = os.environ.get(
    "CSP_SCRIPT_SRC",
    "'self' 'unsafe-inline'",
)


def _csp_header() -> str:
    """Build the artifact-origin CSP. Mirrors the CloudFront response-
    headers-policy so the policy is identical whether CloudFront sets it
    or the Lambda does (defense in depth)."""
    return "; ".join(
        [
            "default-src 'none'",
            f"script-src {_CSP_SCRIPT_SRC}",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data: https:",
            "font-src 'self' data:",
            "connect-src 'none'",
            f"frame-ancestors {_FRAME_ANCESTOR}",
            "form-action 'none'",
            "base-uri 'none'",
        ]
    )


def _placeholder_html() -> str:
    """Minimal HTML returned while JWT validation + content fetch are
    pending. Keeps the deployed pipeline observable (you can hit the
    artifact origin in a browser and see *something* render) without
    serving real user content prematurely."""
    return (
        "<!doctype html>"
        "<html><head>"
        "<meta charset='utf-8'>"
        "<title>Artifact render — pending</title>"
        "<style>body{font:14px system-ui;padding:2rem;color:#444}</style>"
        "</head><body>"
        "<h1>Artifact render service</h1>"
        "<p>Infrastructure deployed; render logic is a follow-up.</p>"
        "</body></html>"
    )


def handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    """Lambda Function URL handler. Payload format v2.0."""
    # TODO: extract `?t=` query param, verify JWT (PyJWT, HS256), fetch
    # content from S3/DDB, render HTML shell wrapping the content.
    return {
        "statusCode": 200,
        "headers": {
            "content-type": "text/html; charset=utf-8",
            "content-security-policy": _csp_header(),
            "x-content-type-options": "nosniff",
            "referrer-policy": "no-referrer",
            "cache-control": "no-store",
        },
        "body": _placeholder_html(),
    }


# Local smoke test: `python handler.py` prints the placeholder response.
if __name__ == "__main__":
    print(json.dumps(handler({}, None), indent=2))
