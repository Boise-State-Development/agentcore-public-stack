"""`GET /files/limits` and the admission rules it describes (unified-attachment-delivery §4.4).

The endpoint and `FileUploadService.max_size_for` both read the attachment
planner (`client_limits`, `upload_size_limit`), so what the SPA and the TUI
refuse is what presign refuses. Today's limits are pinned here so a phase-1
change is visibly "no change": 5 files, 4 MB documents and images, 25 MB
spreadsheets and decks, 50 MB large documents, a 7.0 MB inline budget.
"""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apis.app_api.files import routes
from apis.app_api.files.service import FileUploadService, get_file_upload_service
from apis.shared.auth.dependencies import get_current_user_from_session
from apis.shared.auth.models import User
from apis.shared.files import attachment_plan

MB = 1024 * 1024

PDF = "application/pdf"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


@pytest.fixture(autouse=True)
def _default_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "FILE_UPLOAD_MAX_SIZE_BYTES",
        "FILE_UPLOAD_MAX_SIZE_BYTES_PRESENTATION",
        "FILE_UPLOAD_MAX_SIZE_BYTES_SPREADSHEET",
        "LARGE_DOCUMENT_ATTACHMENTS_ENABLED",
        "FOLDER_ATTACHMENTS_ENABLED",
        "ATTACHMENT_TURN_GUARD_ENABLED",
        "SKIP_AUTH",
    ):
        monkeypatch.delenv(name, raising=False)


def _service() -> FileUploadService:
    return FileUploadService(repository=MagicMock(), s3_client=MagicMock(), bucket_name="b")


def _client(*, authenticated: bool = True) -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    service = _service()
    app.dependency_overrides[get_file_upload_service] = lambda: service
    if authenticated:
        app.dependency_overrides[get_current_user_from_session] = lambda: User(
            user_id="alice", email="alice@example.com", name="Alice", roles=[], raw_token="t"
        )
    return TestClient(app)


class TestEndpoint:
    def test_requires_a_session(self) -> None:
        response = _client(authenticated=False).get("/files/limits")
        assert response.status_code == 401

    def test_serves_todays_limits(self) -> None:
        response = _client().get("/files/limits")
        assert response.status_code == 200
        assert response.json() == {
            "profile": "legacy",
            "filesPerMessage": 5,
            "imagesPerMessage": None,
            "pdfMaxPages": None,
            "inlineDocumentMaxBytes": 4 * MB,
            "inlineBudgetBytes": 7_000_000,
            "maxBytes": {
                "document": 4 * MB,
                "image": 4 * MB,
                "spreadsheet": 25 * MB,
                "presentation": 25 * MB,
                "largeDocument": 50 * MB,
            },
            "largeDocuments": True,
            "folders": {"enabled": True, "perMessage": 2},
        }

    def test_the_flags_that_widen_admission_are_reflected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LARGE_DOCUMENT_ATTACHMENTS_ENABLED", "false")
        monkeypatch.setenv("FOLDER_ATTACHMENTS_ENABLED", "false")
        body = _client().get("/files/limits").json()
        assert body["largeDocuments"] is False
        assert body["maxBytes"]["largeDocument"] is None
        assert body["folders"] == {"enabled": False, "perMessage": None}

    def test_an_environment_override_reaches_the_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FILE_UPLOAD_MAX_SIZE_BYTES_SPREADSHEET", str(10 * MB))
        assert _client().get("/files/limits").json()["maxBytes"]["spreadsheet"] == 10 * MB


class TestClientLimits:
    def test_no_turn_guard_means_no_budget_and_the_resolver_backstop(self) -> None:
        profile = replace(attachment_plan.legacy_profile(), turn_guard=False)
        out = attachment_plan.client_limits(profile, attachment_plan.upload_limits())
        assert out["inlineBudgetBytes"] is None
        assert out["filesPerMessage"] == profile.resolver_backstop_without_guard

    def test_a_non_positive_cap_is_no_limit(self) -> None:
        profile = replace(attachment_plan.legacy_profile(), max_files_per_message=0, inline_total_max_bytes=0)
        out = attachment_plan.client_limits(profile, attachment_plan.upload_limits())
        assert out["filesPerMessage"] is None
        assert out["inlineBudgetBytes"] is None

    def test_serves_the_active_profile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        custom = replace(attachment_plan.legacy_profile(), name="token", max_files_per_message=20)
        monkeypatch.setattr(attachment_plan, "current_limits_profile", lambda: custom)
        out = attachment_plan.client_limits()
        assert (out["profile"], out["filesPerMessage"]) == ("token", 20)


class TestAdmissionReadsThePlanner:
    @pytest.mark.parametrize(
        ("filename", "mime", "expected"),
        [
            ("photo.png", "image/png", 4 * MB),
            ("notes.txt", "text/plain", 50 * MB),
            ("report.pdf", PDF, 50 * MB),
            ("data.xlsx", XLSX, 25 * MB),
            ("data.csv", "text/csv", 25 * MB),
            ("deck.pptx", PPTX, 25 * MB),
        ],
    )
    def test_max_size_for_matches_the_endpoint(self, filename: str, mime: str, expected: int) -> None:
        assert _service().max_size_for(filename, mime) == expected

    def test_large_documents_off_falls_back_to_the_general_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LARGE_DOCUMENT_ATTACHMENTS_ENABLED", "false")
        service = _service()
        assert service.max_size_for("report.pdf", PDF) == 4 * MB
        assert service.max_download_bytes() == 25 * MB

    def test_max_size_for_delegates_to_upload_size_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apis.app_api.files import service as service_module

        seen = []

        def fake(filename, mime, limits):  # type: ignore[no-untyped-def]
            seen.append(limits)
            return 123

        monkeypatch.setattr(service_module, "upload_size_limit", fake)
        assert _service().max_size_for("a.pdf", PDF) == 123
        assert isinstance(seen[0], attachment_plan.UploadLimits)

    def test_count_limit_is_the_profiles(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from apis.app_api.files import service as service_module

        monkeypatch.setattr(
            service_module,
            "current_limits_profile",
            lambda: replace(attachment_plan.legacy_profile(), max_files_per_message=9),
        )
        assert _service().max_files_per_message == 9


class TestSpaFallbackMatchesTheEndpoint:
    """The SPA's one built-in copy (``FALLBACK_ATTACHMENT_LIMITS``) is what it
    judges a drop by before ``GET /files/limits`` answers, so it must equal what
    the endpoint serves with default settings."""

    _FALLBACK = (
        Path(__file__).resolve().parents[4]
        / "frontend/ai.client/src/app/services/file-upload/attachment-limits.ts"
    )

    def _fallback_numbers(self) -> dict:
        source = self._FALLBACK.read_text(encoding="utf-8")
        start = source.index("export const FALLBACK_ATTACHMENT_LIMITS")
        block = source[start : source.index("});", start)]
        numbers = {}
        for name, expr in re.findall(r"(\w+): ([0-9_ *MB]+?)\s*[,\n}]", block):
            value = 1
            for factor in expr.split("*"):
                factor = factor.strip()
                value *= MB if factor == "MB" else int(factor.replace("_", ""))
            numbers[name] = value
        return numbers

    def test_every_number_matches(self) -> None:
        if not self._FALLBACK.exists():
            pytest.skip("SPA not checked out alongside the backend")
        served = _client().get("/files/limits").json()
        expected = {
            "filesPerMessage": served["filesPerMessage"],
            "inlineDocumentMaxBytes": served["inlineDocumentMaxBytes"],
            "inlineBudgetBytes": served["inlineBudgetBytes"],
            **served["maxBytes"],
            "perMessage": served["folders"]["perMessage"],
        }
        assert self._fallback_numbers() == expected
