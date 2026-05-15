"""Artifact authoring tools.

Lets the agent persist standalone HTML documents as versioned artifacts.
The writer here owns S3 upload + the DynamoDB version/HEAD rows; the
render Lambda (`backend/src/lambdas/artifact_render/handler.py`) and the
app-api render-token minter read those rows back. The DDB key layout and
the `storage`/`content_key`/`content_type` attributes are a frozen
cross-PR contract with both readers.
"""

from .tools import make_create_artifact_tool, make_update_artifact_tool

__all__ = ["make_create_artifact_tool", "make_update_artifact_tool"]
