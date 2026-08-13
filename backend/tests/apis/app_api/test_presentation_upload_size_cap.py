"""Presentations upload under a larger cap than everything else.

The general 4MB limit is sized for Bedrock's *inline* document budget — it is
the point past which a document block starts risking a ValidationException
mid-stream. A .pptx never enters that path (Bedrock's document-format enum has
no `pptx`; it routes to the PowerPoint tools instead), so the ceiling that
justifies 4MB simply does not apply to it. Corporate templates with imagery
clear 4MB routinely, which made `create_powerpoint_presentation`'s
``template_name`` argument unusable for exactly the files it exists to accept.

The cap that *does* bind a deck is the Code Interpreter hop:
``_ci_write_bytes`` base64-encodes the whole file into a single ``writeFiles``
``text`` field (~4/3 inflation). That field is a MaxLenString (100MB), so 25MB
of deck → ~33MB of base64 sits well inside it.

Both size gates must agree on which cap applies. Historically they were one
constant read from two places; now that the cap depends on the file, a gate
that reads ``max_file_size`` directly rejects a deck the other gate allowed —
so ``max_size_for`` is the single decision point and this pins it.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from apis.app_api.files.service import FileTooLargeError, FileUploadService
from apis.shared.files.models import PresignRequest

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
GENERAL_CAP = 4 * 1024 * 1024
PRESENTATION_CAP = 25 * 1024 * 1024


@pytest.fixture
def service():
    repository = MagicMock()
    repository.get_user_quota = AsyncMock(
        return_value=SimpleNamespace(total_bytes=0)
    )
    repository.create_file = AsyncMock()

    s3_client = MagicMock()
    s3_client.generate_presigned_url.return_value = "https://example.invalid/put"

    return FileUploadService(
        repository=repository,
        s3_client=s3_client,
        bucket_name="test-bucket",
        max_file_size=GENERAL_CAP,
        presentation_max_file_size=PRESENTATION_CAP,
    )


class TestMaxSizeFor:
    def test_ordinary_documents_get_the_general_cap(self, service):
        assert service.max_size_for("report.pdf", "application/pdf") == GENERAL_CAP

    def test_presentations_get_the_presentation_cap(self, service):
        assert service.max_size_for("deck.pptx", PPTX_MIME) == PRESENTATION_CAP

    def test_extension_alone_is_enough(self, service):
        # Some clients send octet-stream for pptx; the cap must not depend on
        # the browser getting the MIME right.
        assert (
            service.max_size_for("deck.pptx", "application/octet-stream")
            == PRESENTATION_CAP
        )

    def test_defaults_match_the_documented_values(self):
        # Constructed with no overrides — these are the values the frontend
        # constants mirror, and the frontend must never be the larger side.
        default = FileUploadService(
            repository=MagicMock(), s3_client=MagicMock(), bucket_name="b"
        )
        assert default.max_file_size == GENERAL_CAP
        assert default.presentation_max_file_size == PRESENTATION_CAP


class TestPresignSizeEnforcement:
    @pytest.mark.asyncio
    async def test_rejects_ordinary_document_above_general_cap(self, service):
        request = PresignRequest(
            sessionId="s1",
            filename="big.pdf",
            mimeType="application/pdf",
            sizeBytes=GENERAL_CAP + 1,
        )
        with pytest.raises(FileTooLargeError) as exc:
            await service.request_presigned_url("u1", request)
        assert exc.value.max_size == GENERAL_CAP

    @pytest.mark.asyncio
    async def test_accepts_deck_above_general_cap(self, service):
        # The regression this whole change exists to prevent.
        request = PresignRequest(
            sessionId="s1",
            filename="template.pptx",
            mimeType=PPTX_MIME,
            sizeBytes=GENERAL_CAP + 1,
        )
        response = await service.request_presigned_url("u1", request)
        assert response.upload_id

    @pytest.mark.asyncio
    async def test_rejects_deck_above_presentation_cap(self, service):
        request = PresignRequest(
            sessionId="s1",
            filename="huge.pptx",
            mimeType=PPTX_MIME,
            sizeBytes=PRESENTATION_CAP + 1,
        )
        with pytest.raises(FileTooLargeError) as exc:
            await service.request_presigned_url("u1", request)
        # The 400 detail is built from max_size, so the user is told 25MB —
        # not the 4MB that does not apply to their file.
        assert exc.value.max_size == PRESENTATION_CAP
