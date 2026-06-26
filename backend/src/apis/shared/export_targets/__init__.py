"""Export-target capability core, shared across consumers.

An export-target adapter writes a rendered document *out* to a connected app
(e.g. saving a conversation transcript to Google Drive) — the write-direction
mirror of `file_sources`. This package holds the provider-agnostic core: the
adapter contract + registry, the concrete adapters, the transcript renderer,
the domain models, and the connector-visibility / token-resolution helpers.

It lives in `apis.shared` because more than one consumer needs it: app-api's
`POST /sessions/{id}/export` endpoint and the agent-side `save_conversation`
tool (which cannot import from `apis.app_api`). The FastAPI/HTTP error mapping
stays in `apis.app_api.export_targets` — only boundary-free logic lives here.
"""
