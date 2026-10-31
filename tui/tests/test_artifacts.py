"""`/artifacts`: saving an artifact's source to disk."""

from __future__ import annotations

from pathlib import Path

import httpx

from aps_tui.client.catalog import ArtifactVersion
from aps_tui.credentials import CredentialSource
from aps_tui.screens.artifact_picker import ArtifactPicker
from aps_tui.screens.chat import _free_path
from aps_tui.widgets.messages import Notice

from .conftest import make_config, rendered_text, send, sse_response
from .test_chat_screen import agent_turn, build_agent_app
from .test_pickers import catalog_for


class TestNaming:
    def test_file_name_is_a_slug_with_version_and_extension(self) -> None:
        art = ArtifactVersion("a1", 2, "Budget: Q3 / 2026!", "text/html; charset=utf-8")
        assert art.file_name() == "budget-q3-2026-v2.html"

    def test_unknown_types_fall_back_to_txt_and_empty_titles_to_the_id(self) -> None:
        assert ArtifactVersion("abcdef123456789", 1, "", "application/x-thing").file_name() == "abcdef123456-v1.txt"
        assert ArtifactVersion("a", 1, "Notes", "text/markdown").file_name() == "notes-v1.md"

    def test_free_path_never_overwrites(self, tmp_path: Path) -> None:
        taken = tmp_path / "notes-v1.md"
        taken.write_text("x")
        (tmp_path / "notes-v1-2.md").write_text("y")
        assert _free_path(taken) == tmp_path / "notes-v1-3.md"
        assert _free_path(tmp_path / "fresh.md") == tmp_path / "fresh.md"


class Backend:
    def __init__(self, *, content_status: int = 200) -> None:
        self.artifacts = [
            {"artifact_id": "a1", "version": 1, "title": "Budget", "content_type": "text/html; charset=utf-8", "updated_at": "2026-10-30T10:00:00Z"},
            {"artifact_id": "a1", "version": 2, "title": "Budget", "content_type": "text/html; charset=utf-8", "updated_at": "2026-10-30T11:00:00Z"},
            {"artifact_id": "a2", "version": 1, "title": "Notes", "content_type": "text/markdown", "updated_at": "2026-10-30T09:00:00Z"},
        ]
        self.content_status = content_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/artifacts"):
            return httpx.Response(200, json={"artifacts": self.artifacts})
        if "/artifacts/" in path and path.endswith("/content"):
            if self.content_status != 200:
                return httpx.Response(self.content_status, json={"detail": "Artifact is too large to preview — download it instead"})
            version = request.url.params.get("version")
            return httpx.Response(200, json={"content": f"<h1>v{version}</h1>", "content_type": "text/html", "version": int(version or 1)})
        if path.endswith("/chat/stream"):
            return sse_response(agent_turn("ok"))
        if path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [], "nextToken": None, "pendingInterrupts": []})
        return httpx.Response(404, json={"detail": f"no route {path}"})


def app_for(backend: Backend):  # type: ignore[no-untyped-def]
    app = build_agent_app(backend, config=make_config(api_key=None, credential_source=CredentialSource.BFF_SESSION))
    app.chat._catalog = catalog_for(backend)
    return app


async def open_picker(pilot, app, directory: str) -> ArtifactPicker | None:  # type: ignore[no-untyped-def]
    await send(pilot, app, f"/artifacts {directory}")
    for _ in range(10):
        await pilot.pause()
        if isinstance(app.screen, ArtifactPicker):
            return app.screen
    return None


class TestSaving:
    async def test_lists_newest_first_and_saves_the_chosen_version(self, tmp_path: Path) -> None:
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "hello")
            picker = await open_picker(pilot, app, str(tmp_path))
            assert picker is not None
            assert [f"{a.artifact_id}@{a.version}" for a in picker._artifacts] == ["a1@2", "a1@1", "a2@1"]
            assert str(tmp_path) in rendered_text(app)
            await pilot.press("enter")
            for _ in range(10):
                await pilot.pause()
                if (tmp_path / "budget-v2.html").exists():
                    break
            await pilot.pause()
            assert "Saved “Budget” (v2)" in rendered_text(app)
        saved = tmp_path / "budget-v2.html"
        assert saved.read_text() == "<h1>v2</h1>"
        listing = [r for r in backend.requests if r.url.path.endswith("/artifacts")]
        assert listing and listing[0].url.params["session_id"] == app.store.session_id
        content = [r for r in backend.requests if r.url.path.endswith("/content")]
        assert content[0].url.params["version"] == "2"

    async def test_an_existing_file_is_not_overwritten(self, tmp_path: Path) -> None:
        (tmp_path / "budget-v2.html").write_text("keep me")
        backend = Backend()
        app = app_for(backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "hello")
            assert await open_picker(pilot, app, str(tmp_path)) is not None
            await pilot.press("enter")
            for _ in range(10):
                await pilot.pause()
                if (tmp_path / "budget-v2-2.html").exists():
                    break
        assert (tmp_path / "budget-v2.html").read_text() == "keep me"
        assert (tmp_path / "budget-v2-2.html").read_text() == "<h1>v2</h1>"

    async def test_too_large_points_at_the_web_app(self, tmp_path: Path) -> None:
        backend = Backend(content_status=413)
        app = app_for(backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "hello")
            assert await open_picker(pilot, app, str(tmp_path)) is not None
            await pilot.press("enter")
            notices: list[Notice] = []
            for _ in range(10):
                await pilot.pause()
                notices = [n for n in app.chat.transcript.query(Notice) if "Could not fetch" in n._message]
                if notices:
                    break
            assert notices and notices[0]._hint == "Download it from the web app instead."
        assert not list(tmp_path.iterdir())

    async def test_no_artifacts_and_bad_directory_are_reported_without_a_picker(self, tmp_path: Path) -> None:
        backend = Backend()
        backend.artifacts = []
        app = app_for(backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await send(pilot, app, "hello")
            assert await open_picker(pilot, app, str(tmp_path)) is None
            assert await open_picker(pilot, app, str(tmp_path / "missing")) is None
            await send(pilot, app, "/new")
            assert await open_picker(pilot, app, str(tmp_path)) is None  # empty conversation
        assert not [r for r in backend.requests if "missing" in str(r.url)]
