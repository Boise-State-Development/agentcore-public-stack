"""The owner-facing knowledge base upgrade surface (Requirements 21, 23).

Deliberately a **separate package from** ``apis.app_api.kb_migration``. That
package holds the four Lambda handlers, which share one size-constrained image;
this one is HTTP-only and imports ``apis.shared.assistants`` for the permission
model, which pulls the embeddings stack at module scope. Putting the two in the
same package invites a handler import that blows the image-size budget — the
failure ``tests/architecture/test_kb_backend_boundary.py`` exists to prevent.

Nothing here writes ``retrievalEngine``. Enrolment only moves a record into
``shadow``; the worker promotes, and only after verification.
"""
