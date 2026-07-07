"""KB sync Lambdas — scheduled re-index of assistant knowledge-base sources.

Two Lambda entry points packaged in the kb-sync container image
(backend/Dockerfile.kb-sync), sharing one image with different CMD
overrides:

- ``dispatcher`` — fired by the EventBridge rate rule; sweeps the sparse
  DueSyncIndex, applies the runaway guards, and async-invokes the worker.
- ``worker`` — executes a single policy's sync run (PR-2 ships a stub;
  the Drive-file path lands in PR-3, web re-crawl in PR-4).

Design: docs/specs/assistant-kb-sync.md. Keep this package importable
without FastAPI — it deploys outside the app-api container.
"""
