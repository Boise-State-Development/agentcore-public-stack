"""The per-file knowledge-base upload limit (``KB_UPLOAD_MAX_FILE_BYTES``).

One number per engine, read by every path that adds a file: 50 MiB by default for
a managed knowledge base (Bedrock's own non-adjustable per-file limit, so never
more), 20 MiB for the classic Docling pipeline (its processing limit). The SPA
reads it off the documents list as ``kbUsage.maxFileBytes``.

The presign endpoint refuses an over-limit declared size with a 413 BEFORE it
reserves anything; the managed ingestion consumer holds the stored object to the
same limit (tests/lambdas/test_kb_ingestion_consumer.py), because the presigned PUT
does not sign Content-Length.
"""

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from apis.app_api.documents.services import import_service
from apis.app_api.file_sources.models import FileSourceTooLargeError
from apis.shared.kb_backend import upload_limit
from tests.routes.test_document_upload_byte_cap import (  # noqa: F401 (fixtures)
    ASSISTANT_ID,
    ROUTES_MODULE,
    _kb,
    _owner,
    _post,
    _seed_managed_kb,
    app,
    table,
)

MIB = 1024 * 1024
REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _default_limit(monkeypatch):
    monkeypatch.delenv("KB_UPLOAD_MAX_FILE_BYTES", raising=False)
    monkeypatch.delenv("MANAGED_KB_NEW_DEFAULT", raising=False)


def _patched_upload(app, size_bytes):
    with patch(
        f"{ROUTES_MODULE}.resolve_assistant_permission", new_callable=AsyncMock, return_value=_owner()
    ), patch(f"{ROUTES_MODULE}.create_document", new_callable=AsyncMock) as create, patch(
        f"{ROUTES_MODULE}.generate_upload_url",
        new_callable=AsyncMock,
        return_value=("https://signed.example/put", None),
    ) as gen:
        resp = _post(app, size_bytes)
    return resp, create, gen


class TestTheLimit:
    def test_managed_defaults_to_50_mib(self):
        assert upload_limit.max_file_bytes(managed=True) == 50 * MIB

    def test_classic_keeps_its_20_mib_processing_limit(self):
        assert upload_limit.max_file_bytes(managed=False) == 20 * MIB

    def test_a_value_above_bedrocks_limit_is_clamped(self, monkeypatch):
        monkeypatch.setenv("KB_UPLOAD_MAX_FILE_BYTES", str(200 * MIB))
        assert upload_limit.max_file_bytes(managed=True) == 50 * MIB
        assert upload_limit.max_file_bytes(managed=False) == 20 * MIB

    def test_the_old_10_mb_behaviour_is_one_setting(self, monkeypatch):
        monkeypatch.setenv("KB_UPLOAD_MAX_FILE_BYTES", str(10 * MIB))
        assert upload_limit.max_file_bytes(managed=True) == 10 * MIB
        assert upload_limit.max_file_bytes(managed=False) == 10 * MIB

    @pytest.mark.parametrize("raw", ["", "  ", "lots", "0", "-5"])
    def test_an_unusable_value_falls_back_to_the_default(self, monkeypatch, raw):
        # An unset GitHub variable arrives as "".
        monkeypatch.setenv("KB_UPLOAD_MAX_FILE_BYTES", raw)
        assert upload_limit.max_file_bytes(managed=True) == 50 * MIB

    def test_the_message_matches_the_spa(self):
        assert upload_limit.too_large_message(50 * MIB) == "Files can be up to 50 MB."
        assert (
            upload_limit.too_large_message(20 * MIB, 25 * MIB)
            == "Files can be up to 20 MB. This one is 25 MB."
        )

    def test_the_classic_limit_matches_the_ingestion_handler(self):
        """The rag-ingestion image enforces its own copy; the two must agree."""
        handler = (
            REPO_ROOT / "backend/src/apis/app_api/documents/ingestion/handler.py"
        ).read_text()
        match = re.search(r"MAX_FILE_SIZE_MB = (\d+)", handler)
        assert match, "handler.py no longer declares MAX_FILE_SIZE_MB"
        assert int(match.group(1)) * MIB == upload_limit.CLASSIC_MAX_FILE_BYTES


class TestPresignOnAManagedKnowledgeBase:
    def test_over_the_limit_is_413_and_reserves_nothing(self, table, app):
        _seed_managed_kb(table)
        resp, create, gen = _patched_upload(app, 50 * MIB + 1)

        assert resp.status_code == 413
        assert resp.json()["detail"].startswith("Files can be up to 50 MB.")
        create.assert_not_awaited()
        gen.assert_not_awaited()
        assert int(_kb(table)["reservedBytes"]) == 0

    def test_at_the_limit_is_accepted_and_reserved(self, table, app):
        _seed_managed_kb(table)
        resp, create, gen = _patched_upload(app, 50 * MIB)

        assert resp.status_code == 200
        create.assert_awaited_once()
        assert int(_kb(table)["reservedBytes"]) == 50 * MIB

    def test_a_lowered_limit_is_enforced(self, table, app, monkeypatch):
        monkeypatch.setenv("KB_UPLOAD_MAX_FILE_BYTES", str(10 * MIB))
        _seed_managed_kb(table)
        resp, _, _ = _patched_upload(app, 11 * MIB)

        assert resp.status_code == 413
        assert resp.json()["detail"].startswith("Files can be up to 10 MB.")
        assert int(_kb(table)["reservedBytes"]) == 0

    def test_a_file_over_every_limit_never_starts_provisioning(self, table, app):
        """Over the managed ceiling, nothing could accept it, so no Bedrock
        knowledge base is spent on a first upload that is refused."""
        with patch(f"{ROUTES_MODULE}.begin_born_managed", new_callable=AsyncMock) as born:
            resp, _, _ = _patched_upload(app, 60 * MIB)

        assert resp.status_code == 413
        born.assert_not_awaited()


def test_a_negative_declared_size_is_a_422_not_a_500(table, app):
    resp, create, _ = _patched_upload(app, -1)
    assert resp.status_code == 422
    create.assert_not_awaited()


class TestPresignOnAClassicKnowledgeBase:
    def test_over_20_mib_is_413_with_the_classic_limit(self, table, app):
        resp, create, _ = _patched_upload(app, 25 * MIB)

        assert resp.status_code == 413
        assert resp.json()["detail"] == "Files can be up to 20 MB. This one is 25 MB."
        create.assert_not_awaited()
        assert _kb(table) is None

    def test_under_20_mib_is_accepted(self, table, app):
        resp, create, _ = _patched_upload(app, 15 * MIB)
        assert resp.status_code == 200
        create.assert_awaited_once()


class TestTheListReportsTheLimit:
    def _list(self, app, documents=()):
        with patch(
            f"{ROUTES_MODULE}.resolve_assistant_permission", new_callable=AsyncMock, return_value=_owner()
        ), patch(
            f"{ROUTES_MODULE}.list_assistant_documents",
            new_callable=AsyncMock,
            return_value=(list(documents), None),
        ):
            return TestClient(app).get(f"/assistants/{ASSISTANT_ID}/documents").json()

    def test_a_managed_knowledge_base_reports_50_mib(self, table, app):
        _seed_managed_kb(table)
        assert self._list(app)["kbUsage"]["maxFileBytes"] == 50 * MIB

    def test_a_classic_knowledge_base_reports_20_mib(self, table, app):
        doc = MagicMock()
        doc.model_dump.return_value = {
            "documentId": "d1", "assistantId": ASSISTANT_ID, "filename": "a.pdf",
            "contentType": "application/pdf", "sizeBytes": 1, "status": "complete",
            "createdAt": "2026-11-07T00:00:00Z", "updatedAt": "2026-11-07T00:00:00Z",
        }
        usage = self._list(app, [doc])["kbUsage"]
        assert usage["engine"] == "s3vectors"
        assert usage["maxFileBytes"] == 20 * MIB

    def test_a_new_agent_that_will_be_born_managed_reports_50_mib(self, table, app, monkeypatch):
        """No record and no documents: the first upload makes it managed, so the
        dropzone must not promise the classic limit."""
        monkeypatch.setenv("MANAGED_KB_NEW_DEFAULT", "true")
        assert self._list(app)["kbUsage"]["maxFileBytes"] == 50 * MIB

    def test_a_new_agent_without_born_managed_reports_20_mib(self, table, app):
        assert self._list(app)["kbUsage"]["maxFileBytes"] == 20 * MIB


class TestDriveImportUsesTheSameLimit:
    def _adapter(self, **download):
        adapter = MagicMock()
        adapter.download = AsyncMock(**download)
        return adapter

    def test_the_adapter_is_told_the_limit(self):
        adapter = self._adapter(side_effect=FileSourceTooLargeError("too big"))
        with patch.object(import_service, "_mark_failed", new_callable=AsyncMock) as failed:
            import_service.asyncio.run(
                import_service.run_import(
                    assistant_id=ASSISTANT_ID, adapter=adapter, access_token="t",
                    items=[("d1", "f1")], max_file_bytes=20 * MIB,
                )
            )

        assert adapter.download.await_args.kwargs["max_bytes"] == 20 * MIB
        failed.assert_awaited_once()
        assert failed.await_args.args[2] == "Files can be up to 20 MB."

    def test_an_adapter_that_ignores_the_limit_still_never_stages(self):
        content = b"x" * (2 * MIB)
        adapter = self._adapter(
            return_value=SimpleNamespace(content=content, filename="a.pdf", content_type="application/pdf")
        )
        with patch.object(import_service, "_mark_failed", new_callable=AsyncMock) as failed, patch.object(
            import_service, "_put_object", new_callable=AsyncMock
        ) as put, patch.object(import_service, "update_document_import_metadata", new_callable=AsyncMock):
            import_service.asyncio.run(
                import_service.run_import(
                    assistant_id=ASSISTANT_ID, adapter=adapter, access_token="t",
                    items=[("d1", "f1")], max_file_bytes=1 * MIB,
                )
            )

        put.assert_not_awaited()
        assert failed.await_args.args[2] == "Files can be up to 1 MB."

    def test_without_a_limit_it_defaults_to_the_managed_one(self):
        adapter = self._adapter(side_effect=FileSourceTooLargeError("too big"))
        with patch.object(import_service, "_mark_failed", new_callable=AsyncMock):
            import_service.asyncio.run(
                import_service.run_import(
                    assistant_id=ASSISTANT_ID, adapter=adapter, access_token="t", items=[("d1", "f1")],
                )
            )
        assert adapter.download.await_args.kwargs["max_bytes"] == 50 * MIB
