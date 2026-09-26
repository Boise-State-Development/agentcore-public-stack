"""Shared Projects user surface (app-api) — ``/projects`` CRUD, members, transfer (PR-1.2).

The HTTP layer over ``apis.shared.projects``. It also injects the harness
gateway whose delete runs the document and sync-policy cleanup that
``apis.shared`` cannot import.
"""
