"""Unit tests for the web-source deletion cascade.

The cascade's whole job is deciding *which* documents belong to a crawl and
tearing them down in an order that can't strand state, so that's what these
assert: prefix scoping, page-by-page soft-delete, sync-policy removal, and the
crawl row going last.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from apis.app_api.documents.models import Document
from apis.app_api.web_sources.deletion_service import (
    WebSourceDeletionError,
    delete_web_source,
)

ASSISTANT_ID = "ast-1"
OWNER_ID = "user-1"
CRAWL_ID = "CRAWL-1"
ROOT_URL = "https://example.com/docs/"

MODULE = "apis.app_api.web_sources.deletion_service"


def _doc(
    document_id: str,
    *,
    source_file_id: str | None = None,
    connector: str | None = "web",
    status: str = "complete",
) -> Document:
    return Document.model_validate(
        {
            "documentId": document_id,
            "assistantId": ASSISTANT_ID,
            "filename": f"{document_id}.html",
            "contentType": "text/html",
            "sizeBytes": 10,
            "s3Key": f"assistants/{ASSISTANT_ID}/documents/{document_id}/page.html",
            "status": status,
            "chunkCount": 2,
            "sourceConnectorId": connector,
            "sourceFileId": source_file_id,
            "createdAt": "2026-07-14T00:00:00Z",
            "updatedAt": "2026-07-14T00:00:00Z",
        }
    )


@pytest.fixture
def patched():
    """Patch every collaborator the cascade reaches out to."""
    with patch(
        f"{MODULE}.list_assistant_documents", new_callable=AsyncMock
    ) as list_docs, patch(
        f"{MODULE}.soft_delete_document", new_callable=AsyncMock
    ) as soft_delete, patch(
        f"{MODULE}.hard_delete_crawl_job", new_callable=AsyncMock, return_value=True
    ) as hard_delete, patch(
        f"{MODULE}.delete_sync_policies_for_source",
        new_callable=AsyncMock,
        return_value=0,
    ) as delete_policies, patch(
        f"{MODULE}.cleanup_assistant_documents", new_callable=AsyncMock
    ) as cleanup:
        soft_delete.side_effect = lambda assistant_id, document_id, owner_id: _doc(
            document_id, source_file_id=f"{ROOT_URL}{document_id}"
        )
        yield {
            "list_docs": list_docs,
            "soft_delete": soft_delete,
            "hard_delete": hard_delete,
            "delete_policies": delete_policies,
            "cleanup": cleanup,
        }


async def _run(patched) -> int:
    removed = await delete_web_source(
        assistant_id=ASSISTANT_ID,
        crawl_id=CRAWL_ID,
        root_url=ROOT_URL,
        owner_id=OWNER_ID,
    )
    # The cleanup fan-out is a background task; let it start so the assertion
    # on `cleanup_assistant_documents` isn't racing the event loop.
    await asyncio.sleep(0)
    return removed


@pytest.mark.asyncio
async def test_deletes_only_pages_under_the_crawl_root(patched):
    patched["list_docs"].return_value = (
        [
            _doc("DOC-1", source_file_id=f"{ROOT_URL}intro"),
            _doc("DOC-2", source_file_id=f"{ROOT_URL}guide/setup"),
            # Same site, different crawl root — must survive.
            _doc("DOC-3", source_file_id="https://example.com/blog/post"),
            # A device upload — no provenance at all.
            _doc("DOC-4", connector=None, source_file_id=None),
            # A Drive import that happens to sit under a similar path.
            _doc("DOC-5", connector="google_drive", source_file_id=f"{ROOT_URL}sheet"),
        ],
        None,
    )

    removed = await _run(patched)

    assert removed == 2
    deleted_ids = {c.args[1] for c in patched["soft_delete"].await_args_list}
    assert deleted_ids == {"DOC-1", "DOC-2"}


@pytest.mark.asyncio
async def test_skips_pages_already_being_deleted(patched):
    patched["list_docs"].return_value = (
        [
            _doc("DOC-1", source_file_id=f"{ROOT_URL}intro"),
            _doc("DOC-2", source_file_id=f"{ROOT_URL}gone", status="deleting"),
        ],
        None,
    )

    removed = await _run(patched)

    assert removed == 1
    patched["soft_delete"].assert_awaited_once()


@pytest.mark.asyncio
async def test_walks_every_page_of_results(patched):
    patched["list_docs"].side_effect = [
        ([_doc("DOC-1", source_file_id=f"{ROOT_URL}a")], "token-2"),
        ([_doc("DOC-2", source_file_id=f"{ROOT_URL}b")], None),
    ]

    removed = await _run(patched)

    assert removed == 2
    assert patched["list_docs"].await_count == 2
    assert patched["list_docs"].await_args_list[1].kwargs["next_token"] == "token-2"


@pytest.mark.asyncio
async def test_removes_the_sync_policy_before_the_pages(patched):
    """A dispatcher sweep landing mid-delete would re-crawl the source and
    resurrect the very pages we're removing."""
    order: list[str] = []
    patched["list_docs"].return_value = (
        [_doc("DOC-1", source_file_id=f"{ROOT_URL}a")],
        None,
    )
    patched["delete_policies"].side_effect = lambda *a: order.append("policy") or 1
    patched["soft_delete"].side_effect = lambda assistant_id, document_id, owner_id: (
        order.append("page") or _doc(document_id, source_file_id=f"{ROOT_URL}a")
    )
    patched["hard_delete"].side_effect = lambda *a: order.append("crawl") or True

    await _run(patched)

    assert order == ["policy", "page", "crawl"]
    patched["delete_policies"].assert_awaited_once_with(ASSISTANT_ID, CRAWL_ID)


@pytest.mark.asyncio
async def test_hands_the_pages_to_background_cleanup(patched):
    patched["list_docs"].return_value = (
        [_doc("DOC-1", source_file_id=f"{ROOT_URL}a")],
        None,
    )

    await _run(patched)

    patched["cleanup"].assert_awaited_once()
    assistant_id, documents = patched["cleanup"].await_args.args
    assert assistant_id == ASSISTANT_ID
    assert [d.document_id for d in documents] == ["DOC-1"]


@pytest.mark.asyncio
async def test_removes_a_crawl_that_produced_no_pages(patched):
    """A crawl that failed on its root page has nothing to sweep, but the row
    itself still has to go — otherwise it's undeletable from the UI."""
    patched["list_docs"].return_value = ([], None)

    removed = await _run(patched)

    assert removed == 0
    patched["hard_delete"].assert_awaited_once_with(ASSISTANT_ID, CRAWL_ID)
    patched["cleanup"].assert_not_awaited()


@pytest.mark.asyncio
async def test_raises_when_the_crawl_row_survives(patched):
    patched["list_docs"].return_value = ([], None)
    patched["hard_delete"].return_value = False

    with pytest.raises(WebSourceDeletionError):
        await delete_web_source(
            assistant_id=ASSISTANT_ID,
            crawl_id=CRAWL_ID,
            root_url=ROOT_URL,
            owner_id=OWNER_ID,
        )
