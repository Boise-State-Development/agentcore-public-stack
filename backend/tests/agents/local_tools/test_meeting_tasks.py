"""Meeting transcripts → GitHub issues tool families.

Covers the invariants the module promises — identity by closure, never raise,
bounded payloads, the 401 shape the consent hook retries on, and the two-step
GitHub write — with every vendor call mocked at the ``httpx`` boundary. The
pure helpers (Zoom id extraction, WebVTT parsing) are tested directly.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Dict, List

import httpx
import pytest

from agents.local_tools import meeting_tasks as mt
from agents.local_tools.oauth_binding import bind_oauth_provider, oauth_provider_for
from agents.main_agent.integrations import oauth_token_cache
from apis.shared.oauth.auth_failure import looks_like_auth_failure
from apis.shared.tools.injected import MEETING_TASKS_TOOL_IDS

USER = "user-1"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _call(tool, **kwargs):
    fn = getattr(tool, "__wrapped__", None) or tool
    return await fn(**kwargs)


def _payload(result: Dict[str, Any]) -> Dict[str, Any]:
    assert result["status"] == "success", result
    return result["content"][0]["json"]


def _text(result: Dict[str, Any]) -> str:
    assert result["status"] == "error", result
    return result["content"][0]["text"]


class _FakeTransport(httpx.AsyncBaseTransport):
    """Answers requests from a list of (predicate, response) rules and records them."""

    def __init__(self, rules):
        self.rules = rules
        self.requests: List[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for predicate, response in self.rules:
            if predicate(request):
                return response() if callable(response) else response
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")


def _json(status: int, body: Any) -> httpx.Response:
    return httpx.Response(status, json=body)


@pytest.fixture
def transport(monkeypatch):
    """Route every ``httpx.AsyncClient`` the tools build through a fake transport."""
    holder: Dict[str, _FakeTransport] = {}
    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = holder["t"]
        kwargs.pop("follow_redirects", None)
        return real_client(*args, **kwargs)

    def install(rules):
        holder["t"] = _FakeTransport(rules)
        monkeypatch.setattr(mt.httpx, "AsyncClient", _client)
        return holder["t"]

    return install


@pytest.fixture(autouse=True)
def _token():
    oauth_token_cache.set(USER, "zoom-conn", "zoom-token")
    oauth_token_cache.set(USER, "google-conn", "google-token")
    oauth_token_cache.set(USER, "github-conn", "github-token")
    yield
    oauth_token_cache.clear_user(USER)


def _by_name(tools, name):
    return next(t for t in tools if t.tool_name == name)


# ---------------------------------------------------------------------------
# Contract: gate ids, binding, identity by closure
# ---------------------------------------------------------------------------


class TestContract:
    def test_gate_ids_match_the_injected_family(self):
        assert MEETING_TASKS_TOOL_IDS == frozenset(
            {
                mt.GOOGLE_CALENDAR_MEETINGS_TOOL_ID,
                mt.ZOOM_MEETING_TRANSCRIPTS_TOOL_ID,
                mt.GITHUB_ISSUES_TOOL_ID,
            }
        )
        assert mt.MEETING_TASKS_TOOL_IDS is MEETING_TASKS_TOOL_IDS
        assert [tid for tid, _ in mt.MEETING_TASKS_FACTORIES] == sorted(
            MEETING_TASKS_TOOL_IDS, key=["google_calendar_meetings", "zoom_meeting_transcripts", "github_issues_local"].index
        )

    def test_no_tool_takes_a_user_or_token_argument(self):
        tools = mt.build_meeting_tasks_tools(MEETING_TASKS_TOOL_IDS, USER, {tid: "c" for tid in MEETING_TASKS_TOOL_IDS})
        assert len(tools) == 6
        for tool in tools:
            fn = getattr(tool, "__wrapped__", None) or tool
            params = set(inspect.signature(fn).parameters)
            assert not params & {"user", "user_id", "token", "access_token", "provider_id"}, tool.tool_name

    def test_every_tool_is_bound_to_its_row_connector(self):
        tools = mt.build_meeting_tasks_tools(
            MEETING_TASKS_TOOL_IDS,
            USER,
            {"google_calendar_meetings": "g", "zoom_meeting_transcripts": "z", "github_issues_local": None},
        )
        bound = {t.tool_name: oauth_provider_for(t) for t in tools}
        assert bound == {
            "list_calendar_meetings": "g",
            "list_zoom_recordings": "z",
            "get_zoom_transcript": "z",
            "list_github_projects": None,
            "list_github_repositories": None,
            "create_project_tasks": None,
        }

    def test_builds_only_enabled_families_in_catalog_order(self):
        tools = mt.build_meeting_tasks_tools(["github_issues_local", "google_calendar_meetings"], USER, {})
        assert [t.tool_name for t in tools] == [
            "list_calendar_meetings",
            "list_github_projects",
            "list_github_repositories",
            "create_project_tasks",
        ]
        assert mt.build_meeting_tasks_tools([], USER, {}) == []
        assert mt.build_meeting_tasks_tools(None, USER, {}) == []

    def test_binding_helper_round_trips_and_defaults_to_none(self):
        class Obj:
            pass

        assert oauth_provider_for(Obj()) is None
        assert oauth_provider_for(bind_oauth_provider(Obj(), "x")) == "x"
        assert oauth_provider_for(bind_oauth_provider(Obj(), "")) is None

    @pytest.mark.asyncio
    async def test_unbound_family_explains_the_missing_connector(self):
        [tool] = mt.make_google_calendar_tools(USER, None)
        text = _text(await _call(tool))
        assert "Required OAuth provider" in text and "google_calendar_meetings" in text
        assert not looks_like_auth_failure(await _call(tool))

    @pytest.mark.asyncio
    async def test_missing_token_is_a_plain_error_not_a_401(self):
        [tool] = mt.make_google_calendar_tools("someone-else", "google-conn")
        result = await _call(tool)
        assert "connect Google Calendar" in _text(result)
        assert not looks_like_auth_failure(result)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("https://boisestate.zoom.us/j/98765432109?pwd=abc", "98765432109"),
            ("Join: https://zoom.us/w/123456789", "123456789"),
            ("https://us02web.zoom.us/s/8123456789", "8123456789"),
            ("https://meet.google.com/abc-defg-hij", None),
            ("https://zoom.us/my/vanity.room", None),
            ("", None),
            (None, None),
        ],
    )
    def test_extract_zoom_meeting_id(self, text, expected):
        assert mt.extract_zoom_meeting_id(text) == expected

    def test_extract_takes_the_first_hit_across_fields(self):
        assert mt.extract_zoom_meeting_id(None, "no link", "zoom.us/j/111222333", "zoom.us/j/999888777") == "111222333"

    def test_parse_vtt_merges_consecutive_speakers(self):
        vtt = (
            "WEBVTT\n\n"
            "1\n00:00:01.000 --> 00:00:03.000\nAlice Smith: Let's start.\n\n"
            "2\n00:00:03.000 --> 00:00:05.500\nAlice Smith: First item is the API.\n\n"
            "NOTE a comment\n\n"
            "3\n00:00:05.500 --> 00:00:07.000\nBob: I'll take it.\n\n"
            "4\n00:01:07.000 --> 00:01:09.000\nno speaker here\n"
        )
        cues = mt.parse_vtt(vtt)
        assert cues == [
            {"start_seconds": 1, "speaker": "Alice Smith", "text": "Let's start. First item is the API."},
            {"start_seconds": 5, "speaker": "Bob", "text": "I'll take it."},
            {"start_seconds": 67, "speaker": None, "text": "no speaker here"},
        ]

    def test_parse_vtt_handles_hours_and_crlf(self):
        cues = mt.parse_vtt("WEBVTT\r\n\r\n01:02:03.000 --> 01:02:04.000\r\nX: hi\r\n")
        assert cues == [{"start_seconds": 3723, "speaker": "X", "text": "hi"}]
        assert mt.parse_vtt("") == [] and mt.parse_vtt("WEBVTT\n") == []

    def test_render_transcript_lines_and_truncation(self):
        cues = [
            {"start_seconds": 1, "speaker": "Alice", "text": "one"},
            {"start_seconds": 3723, "speaker": None, "text": "two"},
            {"start_seconds": 4000, "speaker": "Bob", "text": "three"},
        ]
        text, truncated = mt.render_transcript(cues)
        assert text == "[00:00:01] Alice: one\n[01:02:03] two\n[01:06:40] Bob: three"
        assert truncated is False
        text, truncated = mt.render_transcript(cues, max_chars=40)
        assert text == "[00:00:01] Alice: one\n[01:02:03] two"
        assert truncated is True

    def test_zoom_download_host_allowlist(self):
        assert mt._is_zoom_download_host("https://us02web.zoom.us/rec/download/abc")
        assert mt._is_zoom_download_host("https://zoom.us/rec/download/abc")
        assert mt._is_zoom_download_host("https://x.zoomgov.com/rec/download/abc")
        assert not mt._is_zoom_download_host("http://us02web.zoom.us/rec/download/abc")
        assert not mt._is_zoom_download_host("https://evil.example.com/zoom.us/")
        assert not mt._is_zoom_download_host("https://notzoom.us/rec")
        assert not mt._is_zoom_download_host("")

    def test_zoom_path_segment_double_encodes_uuids(self):
        assert mt._zoom_meeting_path_segment(" 123456789 ") == "123456789"
        assert mt._zoom_meeting_path_segment("/ab//cd+e=") == "%252Fab%252F%252Fcd%252Be%253D"

    def test_parse_window_dates_span_whole_days(self):
        start, end = mt._parse_window("2026-03-01", "2026-03-02", default_back_days=7, default_forward_days=7)
        assert mt._rfc3339(start) == "2026-03-01T00:00:00Z"
        assert mt._rfc3339(end) == "2026-03-02T23:59:59Z"
        start, end = mt._parse_window("2026-03-01T10:00:00-07:00", "2026-03-01T18:00:00Z", default_back_days=7, default_forward_days=7)
        assert mt._rfc3339(start) == "2026-03-01T17:00:00Z"
        assert mt._rfc3339(end) == "2026-03-01T18:00:00Z"
        with pytest.raises(ValueError):
            mt._parse_window("2026-03-02", "2026-03-01", default_back_days=7, default_forward_days=7)


# ---------------------------------------------------------------------------
# Google Calendar
# ---------------------------------------------------------------------------


def _event(**overrides) -> Dict[str, Any]:
    base = {
        "id": "evt1",
        "status": "confirmed",
        "summary": "Sprint planning",
        "start": {"dateTime": "2026-03-02T16:00:00Z"},
        "end": {"dateTime": "2026-03-02T17:00:00Z"},
        "organizer": {"email": "pm@example.edu"},
        "attendees": [{"email": f"p{i}@example.edu"} for i in range(30)],
        "conferenceData": {"entryPoints": [{"entryPointType": "video", "uri": "https://x.zoom.us/j/98765432109?pwd=q"}]},
        "description": "x" * 1000,
        "htmlLink": "https://calendar.google.com/e/1",
    }
    base.update(overrides)
    return base


class TestGoogleCalendar:
    @pytest.mark.asyncio
    async def test_lists_events_with_zoom_ids_and_bounds(self, transport):
        t = transport(
            [
                (
                    lambda r: "calendar/v3/calendars/primary/events" in str(r.url),
                    _json(
                        200,
                        {
                            "items": [
                                _event(),
                                _event(id="evt2", status="cancelled"),
                                _event(id="evt3", conferenceData=None, location="Zoom: https://zoom.us/j/111222333"),
                            ],
                        },
                    ),
                )
            ]
        )
        [tool] = mt.make_google_calendar_tools(USER, "google-conn")
        out = _payload(await _call(tool, start="2026-03-01", end="2026-03-07", query="sprint"))

        req = t.requests[0]
        assert req.headers["Authorization"] == "Bearer google-token"
        assert req.url.params["timeMin"] == "2026-03-01T00:00:00Z"
        assert req.url.params["timeMax"] == "2026-03-07T23:59:59Z"
        assert req.url.params["singleEvents"] == "true" and req.url.params["q"] == "sprint"

        assert out["count"] == 2  # the cancelled one is dropped
        first, third = out["events"]
        assert first["zoom_meeting_id"] == "98765432109"
        assert first["zoom_join_url"].startswith("https://x.zoom.us/j/")
        assert first["attendee_count"] == 30 and len(first["attendees"]) == mt.MAX_ATTENDEES_LISTED
        assert len(first["description"]) <= mt.MAX_DESCRIPTION_CHARS
        assert third["zoom_meeting_id"] == "111222333" and third["zoom_join_url"] is None

    @pytest.mark.asyncio
    async def test_401_is_reported_in_the_retryable_shape(self, transport):
        transport([(lambda r: True, _json(401, {"error": {"message": "Invalid Credentials"}}))])
        [tool] = mt.make_google_calendar_tools(USER, "google-conn")
        result = await _call(tool)
        assert looks_like_auth_failure(result)
        assert "Google Calendar" in _text(result)

    @pytest.mark.asyncio
    async def test_other_http_errors_and_transport_failures_never_raise(self, transport):
        transport([(lambda r: True, _json(403, {"error": "forbidden " + "y" * 1000}))])
        [tool] = mt.make_google_calendar_tools(USER, "google-conn")
        text = _text(await _call(tool))
        assert "HTTP 403" in text and len(text) < 500

        def boom():
            raise httpx.ConnectError("dns")

        transport([(lambda r: True, boom)])
        text = _text(await _call(tool))
        assert "Could not reach Google Calendar" in text

    @pytest.mark.asyncio
    async def test_bad_window_is_an_error_without_a_request(self, transport):
        t = transport([])
        [tool] = mt.make_google_calendar_tools(USER, "google-conn")
        assert "Invalid date window" in _text(await _call(tool, start="2026-03-09", end="2026-03-01"))
        assert t.requests == []


# ---------------------------------------------------------------------------
# Zoom
# ---------------------------------------------------------------------------

VTT = "WEBVTT\n\n1\n00:00:01.000 --> 00:00:03.000\nAlice: Ship the API by Friday.\n\n2\n00:00:03.000 --> 00:00:05.000\nBob: I'll open the issue.\n"


def _recording(meeting_id=98765432109, with_transcript=True, download_url="https://us02web.zoom.us/rec/download/tr1"):
    files = [{"id": "f1", "file_type": "MP4", "status": "completed", "download_url": "https://us02web.zoom.us/rec/download/mp4"}]
    if with_transcript:
        files.append({"id": "f2", "file_type": "TRANSCRIPT", "status": "completed", "download_url": download_url})
    return {
        "id": meeting_id,
        "uuid": "abc==",
        "topic": "Sprint planning",
        "start_time": "2026-03-02T16:00:00Z",
        "duration": 60,
        "recording_files": files,
    }


class TestZoom:
    @pytest.mark.asyncio
    async def test_list_recordings_follows_pages_and_summarizes(self, transport):
        pages = iter(
            [
                _json(200, {"meetings": [_recording()], "next_page_token": "p2"}),
                _json(200, {"meetings": [_recording(meeting_id=5, with_transcript=False)], "next_page_token": ""}),
            ]
        )
        t = transport([(lambda r: r.url.path.endswith("/users/me/recordings"), lambda: next(pages))])
        tool = _by_name(mt.make_zoom_tools(USER, "zoom-conn"), "list_zoom_recordings")
        out = _payload(await _call(tool, from_date="2026-03-01", to_date="2026-03-15"))

        assert len(t.requests) == 2
        assert t.requests[0].url.params["from"] == "2026-03-01" and t.requests[0].url.params["to"] == "2026-03-15"
        assert t.requests[1].url.params["next_page_token"] == "p2"
        assert out["count"] == 2
        assert out["recordings"][0] == {
            "meeting_id": "98765432109",
            "uuid": "abc==",
            "topic": "Sprint planning",
            "start_time": "2026-03-02T16:00:00Z",
            "duration_minutes": 60,
            "has_transcript": True,
            "recording_file_count": 2,
        }
        assert out["recordings"][1]["has_transcript"] is False

    @pytest.mark.asyncio
    async def test_list_recordings_refuses_more_than_a_month(self, transport):
        t = transport([])
        tool = _by_name(mt.make_zoom_tools(USER, "zoom-conn"), "list_zoom_recordings")
        assert "one month" in _text(await _call(tool, from_date="2026-01-01", to_date="2026-03-01"))
        assert t.requests == []

    @pytest.mark.asyncio
    async def test_get_transcript_downloads_parses_and_attributes(self, transport):
        t = transport(
            [
                (lambda r: r.url.path == "/v2/meetings/98765432109/recordings", _json(200, _recording())),
                (lambda r: r.url.path == "/rec/download/tr1", httpx.Response(200, text=VTT)),
            ]
        )
        tool = _by_name(mt.make_zoom_tools(USER, "zoom-conn"), "get_zoom_transcript")
        out = _payload(await _call(tool, meeting_id="98765432109"))

        assert [r.headers.get("Authorization") for r in t.requests] == ["Bearer zoom-token", "Bearer zoom-token"]
        assert out["meeting_id"] == "98765432109" and out["topic"] == "Sprint planning"
        assert out["speakers"] == ["Alice", "Bob"] and out["segment_count"] == 2
        assert out["truncated"] is False and out["note"] is None
        assert out["transcript"] == "[00:00:01] Alice: Ship the API by Friday.\n[00:00:03] Bob: I'll open the issue."

    @pytest.mark.asyncio
    async def test_get_transcript_truncates_long_meetings_with_a_note(self, transport, monkeypatch):
        monkeypatch.setattr(mt, "MAX_TRANSCRIPT_CHARS", 45)
        transport(
            [
                (lambda r: "/meetings/" in r.url.path, _json(200, _recording())),
                (lambda r: "/rec/download/" in r.url.path, httpx.Response(200, text=VTT)),
            ]
        )
        tool = _by_name(mt.make_zoom_tools(USER, "zoom-conn"), "get_zoom_transcript")
        out = _payload(await _call(tool, meeting_id="98765432109"))
        assert out["truncated"] is True and "cut" in out["note"]
        assert out["transcript"] == "[00:00:01] Alice: Ship the API by Friday."

    @pytest.mark.asyncio
    async def test_get_transcript_uuid_is_double_encoded(self, transport):
        t = transport([(lambda r: "/meetings/" in r.url.path, _json(404, {"code": 3301, "message": "no recording"}))])
        tool = _by_name(mt.make_zoom_tools(USER, "zoom-conn"), "get_zoom_transcript")
        text = _text(await _call(tool, meeting_id="/ab//cd=="))
        assert "no cloud recording" in text
        assert t.requests[0].url.raw_path.decode().startswith("/v2/meetings/%252Fab%252F%252Fcd%253D%253D/recordings")

    @pytest.mark.asyncio
    async def test_get_transcript_without_a_transcript_file(self, transport):
        transport([(lambda r: "/meetings/" in r.url.path, _json(200, _recording(with_transcript=False)))])
        tool = _by_name(mt.make_zoom_tools(USER, "zoom-conn"), "get_zoom_transcript")
        assert "no transcript yet" in _text(await _call(tool, meeting_id="98765432109"))

    @pytest.mark.asyncio
    async def test_get_transcript_refuses_a_download_url_off_zoom(self, transport):
        t = transport([(lambda r: "/meetings/" in r.url.path, _json(200, _recording(download_url="https://evil.example.com/x")))])
        tool = _by_name(mt.make_zoom_tools(USER, "zoom-conn"), "get_zoom_transcript")
        assert "does not point at Zoom" in _text(await _call(tool, meeting_id="98765432109"))
        assert len(t.requests) == 1  # never fetched

    @pytest.mark.asyncio
    async def test_get_transcript_401_on_download_is_retryable(self, transport):
        transport(
            [
                (lambda r: "/meetings/" in r.url.path, _json(200, _recording())),
                (lambda r: "/rec/download/" in r.url.path, httpx.Response(401, text="unauthorized")),
            ]
        )
        tool = _by_name(mt.make_zoom_tools(USER, "zoom-conn"), "get_zoom_transcript")
        assert looks_like_auth_failure(await _call(tool, meeting_id="98765432109"))

    @pytest.mark.asyncio
    async def test_get_transcript_requires_an_id(self, transport):
        t = transport([])
        tool = _by_name(mt.make_zoom_tools(USER, "zoom-conn"), "get_zoom_transcript")
        assert "required" in _text(await _call(tool, meeting_id="  "))
        assert t.requests == []


# ---------------------------------------------------------------------------
# GitHub
# ---------------------------------------------------------------------------


class TestGitHub:
    @pytest.mark.asyncio
    async def test_list_repositories_filters_and_summarizes(self, transport):
        t = transport(
            [
                (
                    lambda r: r.url.path == "/user/repos",
                    _json(
                        200,
                        [
                            {
                                "full_name": "org/platform",
                                "private": True,
                                "has_issues": True,
                                "open_issues_count": 4,
                                "description": "d" * 500,
                                "html_url": "https://github.com/org/platform",
                            },
                            {
                                "full_name": "me/dotfiles",
                                "private": False,
                                "open_issues_count": 0,
                                "description": None,
                                "html_url": "https://github.com/me/dotfiles",
                            },
                        ],
                    ),
                )
            ]
        )
        tool = _by_name(mt.make_github_tools(USER, "github-conn"), "list_github_repositories")
        out = _payload(await _call(tool, query="PLATFORM"))
        req = t.requests[0]
        assert req.headers["Authorization"] == "Bearer github-token"
        assert req.headers["Accept"] == "application/vnd.github+json"
        assert req.url.params["affiliation"] == "owner,collaborator,organization_member"
        assert out["count"] == 1
        assert out["repositories"][0]["full_name"] == "org/platform"
        assert len(out["repositories"][0]["description"]) <= 120

        out = _payload(await _call(tool))
        assert [r["full_name"] for r in out["repositories"]] == ["org/platform", "me/dotfiles"]


# ---------------------------------------------------------------------------
# GitHub Projects (v2): board lookup, preview, and the issue → board write
# ---------------------------------------------------------------------------

PROJECT_ID = "PVT_board"
STATUS_FIELD = {
    "id": "F_status",
    "name": "Status",
    "dataType": "SINGLE_SELECT",
    "options": [{"id": "O_triage", "name": "Triage"}, {"id": "O_done", "name": "Done"}],
}
SOURCE_FIELD = {
    "id": "F_source",
    "name": "Source",
    "dataType": "SINGLE_SELECT",
    "options": [{"id": "O_snow", "name": "ServiceNow"}, {"id": "O_meeting", "name": "Meeting"}],
}
APP_FIELD = {
    "id": "F_app",
    "name": "App",
    "dataType": "SINGLE_SELECT",
    "options": [{"id": "O_ai", "name": "boisestate.ai"}, {"id": "O_majors", "name": "Majors"}],
}
TITLE_FIELD = {"id": "F_title", "name": "Title", "dataType": "TITLE"}


def _board(closed=False, fields=(STATUS_FIELD, SOURCE_FIELD, APP_FIELD, TITLE_FIELD)):
    """GraphQL answer for an org board; the ``user`` lookup of an org login is NOT_FOUND."""
    board = {
        "id": PROJECT_ID,
        "title": "Cloud Architecture & Development",
        "url": "https://github.com/orgs/org/projects/7",
        "closed": closed,
        "fields": {"nodes": list(fields)},
    }
    return _json(
        200,
        {"data": {"organization": {"projectV2": board}, "user": None}, "errors": [{"type": "NOT_FOUND", "message": "Could not resolve to a User"}]},
    )


def _gql(keyword: str):
    """Predicate: a GraphQL POST whose query mentions ``keyword``."""
    return lambda r: r.method == "POST" and r.url.path == "/graphql" and keyword in r.content.decode()


def _gql_body(request: httpx.Request) -> Dict[str, Any]:
    return json.loads(request.content)


def _is_issue_post(r: httpx.Request) -> bool:
    return r.method == "POST" and r.url.path == "/repos/org/platform/issues"


def _tasks(n=2) -> List[Dict[str, Any]]:
    return [{"title": f"Action item {i}", "body": f"From the 2026-03-02 sprint planning.\n> item {i}", "labels": ["meeting"]} for i in range(n)]


class TestProjectHelpers:
    @pytest.mark.parametrize(
        "ref, expected",
        [
            ("org/7", ("org", 7)),
            ("  My-Org/12  ", ("My-Org", 12)),
            ("https://github.com/orgs/org/projects/7", ("org", 7)),
            ("https://github.com/users/someone/projects/3/views/1", ("someone", 3)),
            ("7", None),
            ("org/platform", None),
            ("org/7/extra", None),
            ("", None),
            (None, None),
        ],
    )
    def test_parse_project_ref(self, ref, expected):
        assert mt.parse_project_ref(ref) == expected

    def test_resolve_field_values_applies_defaults_and_is_case_insensitive(self):
        selects = mt._single_select_fields({"fields": {"nodes": [STATUS_FIELD, SOURCE_FIELD, APP_FIELD, TITLE_FIELD]}})
        assert set(selects) == {"Status", "Source", "App"}  # the TITLE field is not a single-select

        values, problem = mt.resolve_field_values(selects, {})
        assert problem is None and values == {"Status": "Triage", "Source": "Meeting"}

        values, problem = mt.resolve_field_values(selects, {"app": "BOISESTATE.AI", "status": "done"})
        assert problem is None and values == {"App": "boisestate.ai", "Status": "Done", "Source": "Meeting"}

        values, problem = mt.resolve_field_values({"App": selects["App"]}, {})
        assert problem is None and values == {}  # defaults only when the board has the field

    def test_resolve_field_values_refuses_unknown_names(self):
        selects = mt._single_select_fields({"fields": {"nodes": [STATUS_FIELD, APP_FIELD]}})
        _, problem = mt.resolve_field_values(selects, {"Apps": "Majors"})
        assert "no single-select field named 'Apps'" in problem and "App, Status" in problem
        _, problem = mt.resolve_field_values(selects, {"App": "Major"})
        assert "'Major' is not an option" in problem and "boisestate.ai, Majors" in problem

    def test_build_field_update_mutation_aliases_each_field(self):
        selects = mt._single_select_fields({"fields": {"nodes": [STATUS_FIELD, SOURCE_FIELD]}})
        mutation, variables = mt.build_field_update_mutation(selects, {"Status": "Triage", "Source": "Meeting"})
        assert mutation.count("updateProjectV2ItemFieldValue") == 2
        assert "$project: ID!" in mutation and "$item: ID!" in mutation
        assert variables == {"f0": "F_status", "o0": "O_triage", "f1": "F_source", "o1": "O_meeting"}


class TestGitHubProjects:
    @pytest.mark.asyncio
    async def test_list_projects_for_an_org(self, transport):
        nodes = [
            {
                "number": 7,
                "title": "Cloud Architecture & Development",
                "url": "https://github.com/orgs/org/projects/7",
                "closed": False,
                "shortDescription": "Web Team sprints",
            },
            {"number": 2, "title": "Old", "url": "https://github.com/orgs/org/projects/2", "closed": True, "shortDescription": None},
        ]
        t = transport(
            [
                (
                    _gql("projectsV2"),
                    _json(
                        200,
                        {
                            "data": {"organization": {"projectsV2": {"nodes": nodes}}, "user": None},
                            "errors": [{"type": "NOT_FOUND", "message": "no user"}],
                        },
                    ),
                )
            ]
        )
        tool = _by_name(mt.make_github_tools(USER, "github-conn"), "list_github_projects")
        out = _payload(await _call(tool, owner="org"))
        req = t.requests[0]
        assert req.headers["Authorization"] == "Bearer github-token"
        assert _gql_body(req)["variables"] == {"owner": "org", "first": mt.MAX_GITHUB_PROJECTS}
        assert out["count"] == 2
        assert out["projects"][0] == {
            "project": "org/7",
            "title": "Cloud Architecture & Development",
            "closed": False,
            "description": "Web Team sprints",
            "url": "https://github.com/orgs/org/projects/7",
        }
        assert out["projects"][1]["closed"] is True

    @pytest.mark.asyncio
    async def test_list_projects_unknown_owner_is_an_error(self, transport):
        transport(
            [(_gql("projectsV2"), _json(200, {"data": {"organization": None, "user": None}, "errors": [{"type": "NOT_FOUND", "message": "x"}]}))]
        )
        tool = _by_name(mt.make_github_tools(USER, "github-conn"), "list_github_projects")
        assert "No GitHub organization or user named 'nobody'" in _text(await _call(tool, owner="nobody"))

    @pytest.mark.asyncio
    async def test_list_projects_rejects_a_bad_owner_without_a_request(self, transport):
        t = transport([])
        tool = _by_name(mt.make_github_tools(USER, "github-conn"), "list_github_projects")
        assert "organization or user login" in _text(await _call(tool, owner="org/7"))
        assert t.requests == []

    @pytest.mark.asyncio
    async def test_preview_reads_the_board_and_writes_nothing(self, transport):
        t = transport([(_gql("projectV2(number"), _board())])
        tool = _by_name(mt.make_github_tools(USER, "github-conn"), "create_project_tasks")
        out = _payload(await _call(tool, project="org/7", repository="org/platform", tasks=_tasks(2), fields={"App": "boisestate.ai"}))

        assert len(t.requests) == 1
        assert _gql_body(t.requests[0])["variables"] == {"owner": "org", "number": 7, "fields": mt.MAX_PROJECT_FIELDS}
        assert out["status"] == "confirm_required" and out["task_count"] == 2
        assert out["project"] == {"project": "org/7", "title": "Cloud Architecture & Development", "url": "https://github.com/orgs/org/projects/7"}
        assert [p["title"] for p in out["preview"]] == ["Action item 0", "Action item 1"]
        assert out["preview"][0]["fields"] == {"App": "boisestate.ai", "Status": "Triage", "Source": "Meeting"}
        assert out["preview"][0]["labels"] == ["meeting"]
        assert out["field_options"] == {"Status": ["Triage", "Done"], "Source": ["ServiceNow", "Meeting"], "App": ["boisestate.ai", "Majors"]}
        assert "confirm=true" in out["message"]

    @pytest.mark.asyncio
    async def test_a_task_field_overrides_the_shared_field_and_the_board_url_works(self, transport):
        transport([(_gql("projectV2(number"), _board())])
        tool = _by_name(mt.make_github_tools(USER, "github-conn"), "create_project_tasks")
        tasks = _tasks(2)
        tasks[1]["fields"] = {"app": "majors", "Status": "Done"}
        out = _payload(
            await _call(
                tool, project="https://github.com/orgs/org/projects/7", repository="org/platform", tasks=tasks, fields={"App": "boisestate.ai"}
            )
        )
        assert out["preview"][0]["fields"] == {"App": "boisestate.ai", "Status": "Triage", "Source": "Meeting"}
        assert out["preview"][1]["fields"] == {"App": "Majors", "Status": "Done", "Source": "Meeting"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs, fragment",
        [
            ({"project": "7"}, "owner/number"),
            ({"project": "org/platform"}, "owner/number"),
            ({"repository": "platform"}, "owner/name"),
            ({"repository": "org/platform/extra"}, "owner/name"),
            ({"tasks": []}, "non-empty"),
            ({"tasks": "not a list"}, "non-empty"),
            ({"tasks": _tasks(26)}, "At most 25"),
            ({"tasks": [{"body": "no title"}]}, "Task 1 has no title"),
            ({"tasks": [{"title": "t" * 300}]}, "over 256"),
            ({"tasks": [{"title": "ok", "labels": "bug"}]}, "labels must be a list"),
            ({"tasks": [{"title": "ok"}, "nope"]}, "Task 2 is not an object"),
            ({"tasks": [{"title": "ok", "fields": "Triage"}]}, "Task 1's fields must be an object"),
            ({"fields": {"Status": 3}}, "`fields` must be an object"),
        ],
    )
    async def test_invalid_input_is_an_error_without_a_request(self, transport, kwargs, fragment):
        t = transport([])
        tool = _by_name(mt.make_github_tools(USER, "github-conn"), "create_project_tasks")
        args = {"project": "org/7", "repository": "org/platform", "tasks": _tasks(1), "confirm": True, **kwargs}
        assert fragment in _text(await _call(tool, **args))
        assert t.requests == []

    @pytest.mark.asyncio
    async def test_unknown_field_or_option_is_refused_before_any_write(self, transport):
        t = transport([(_gql("projectV2(number"), _board)])
        tool = _by_name(mt.make_github_tools(USER, "github-conn"), "create_project_tasks")
        text = _text(await _call(tool, project="org/7", repository="org/platform", tasks=_tasks(1), fields={"Apps": "Majors"}, confirm=True))
        assert text.startswith("Task 1:") and "no single-select field named 'Apps'" in text
        tasks = _tasks(2)
        tasks[1]["fields"] = {"Priority": "P1"}
        text = _text(await _call(tool, project="org/7", repository="org/platform", tasks=tasks, confirm=True))
        assert text.startswith("Task 2:") and "'Priority'" in text
        assert all(r.url.path == "/graphql" for r in t.requests) and len(t.requests) == 2

    @pytest.mark.asyncio
    async def test_closed_or_missing_board_is_refused(self, transport):
        transport([(_gql("projectV2(number"), _board(closed=True))])
        tool = _by_name(mt.make_github_tools(USER, "github-conn"), "create_project_tasks")
        assert "is closed" in _text(await _call(tool, project="org/7", repository="org/platform", tasks=_tasks(1)))

        transport(
            [
                (
                    _gql("projectV2(number"),
                    _json(200, {"data": {"organization": {"projectV2": None}, "user": None}, "errors": [{"type": "NOT_FOUND", "message": "x"}]}),
                )
            ]
        )
        text = _text(await _call(tool, project="org/99", repository="org/platform", tasks=_tasks(1)))
        assert "No GitHub project org/99" in text and "list_github_projects" in text

    @pytest.mark.asyncio
    async def test_missing_project_scope_names_the_fix(self, transport):
        transport(
            [
                (
                    _gql("projectV2(number"),
                    _json(
                        200,
                        {"data": None, "errors": [{"type": "INSUFFICIENT_SCOPES", "message": "Your token has not been granted the required scopes"}]},
                    ),
                )
            ]
        )
        tool = _by_name(mt.make_github_tools(USER, "github-conn"), "create_project_tasks")
        result = await _call(tool, project="org/7", repository="org/platform", tasks=_tasks(1))
        text = _text(result)
        assert "`project` OAuth scope" in text and "Admin → Connectors" in text
        assert not looks_like_auth_failure(result)

    @pytest.mark.asyncio
    async def test_confirm_creates_each_issue_adds_it_to_the_board_and_sets_fields(self, transport):
        counter = iter(range(101, 200))

        def created():
            n = next(counter)
            return _json(
                201, {"number": n, "node_id": f"I_{n}", "title": f"Action item {n - 101}", "html_url": f"https://github.com/org/platform/issues/{n}"}
            )

        items = iter(["PVTI_a", "PVTI_b"])
        t = transport(
            [
                (_gql("projectV2(number"), _board()),
                (_is_issue_post, created),
                (_gql("addProjectV2ItemById"), lambda: _json(200, {"data": {"addProjectV2ItemById": {"item": {"id": next(items)}}}})),
                (_gql("updateProjectV2ItemFieldValue"), _json(200, {"data": {"f0": {"projectV2Item": {"id": "x"}}}})),
            ]
        )
        tool = _by_name(mt.make_github_tools(USER, "github-conn"), "create_project_tasks")
        out = _payload(await _call(tool, project="org/7", repository="org/platform", tasks=_tasks(2), fields={"App": "boisestate.ai"}, confirm=True))

        assert out["status"] == "created" and out["failed"] == [] and out["warnings"] == []
        assert out["created"] == [
            {
                "number": 101,
                "title": "Action item 0",
                "url": "https://github.com/org/platform/issues/101",
                "on_board": True,
                "fields": {"App": "boisestate.ai", "Status": "Triage", "Source": "Meeting"},
            },
            {
                "number": 102,
                "title": "Action item 1",
                "url": "https://github.com/org/platform/issues/102",
                "on_board": True,
                "fields": {"App": "boisestate.ai", "Status": "Triage", "Source": "Meeting"},
            },
        ]
        assert "2 issue(s)" in out["message"] and "2 added to" in out["message"]

        # board lookup, then per task: REST POST, add item, set fields
        assert [r.url.path for r in t.requests] == ["/graphql", "/repos/org/platform/issues", "/graphql", "/graphql"] * 1 + [
            "/repos/org/platform/issues",
            "/graphql",
            "/graphql",
        ]
        issue_posts = [json.loads(r.content) for r in t.requests if _is_issue_post(r)]
        assert issue_posts[0] == {"title": "Action item 0", "body": _tasks(1)[0]["body"], "labels": ["meeting"]}
        assert "fields" not in issue_posts[0] and "assignees" not in issue_posts[0]
        add_vars = _gql_body(t.requests[2])["variables"]
        assert add_vars == {"project": PROJECT_ID, "content": "I_101"}
        field_vars = _gql_body(t.requests[3])["variables"]
        assert field_vars["project"] == PROJECT_ID and field_vars["item"] == "PVTI_a"
        assert {field_vars["f0"]: field_vars["o0"], field_vars["f1"]: field_vars["o1"], field_vars["f2"]: field_vars["o2"]} == {
            "F_app": "O_ai",
            "F_status": "O_triage",
            "F_source": "O_meeting",
        }

    @pytest.mark.asyncio
    async def test_401_before_anything_is_created_is_retryable(self, transport):
        transport([(lambda r: True, _json(401, {"message": "Bad credentials"}))])
        tool = _by_name(mt.make_github_tools(USER, "github-conn"), "create_project_tasks")
        # On the board lookup...
        assert looks_like_auth_failure(await _call(tool, project="org/7", repository="org/platform", tasks=_tasks(2), confirm=True))
        # ...and on the first issue POST.
        transport([(_gql("projectV2(number"), _board()), (_is_issue_post, _json(401, {"message": "Bad credentials"}))])
        assert looks_like_auth_failure(await _call(tool, project="org/7", repository="org/platform", tasks=_tasks(2), confirm=True))

    @pytest.mark.asyncio
    async def test_failure_after_a_creation_is_partial_never_retryable(self, transport):
        posts = iter(
            [
                _json(201, {"number": 1, "node_id": "I_1", "title": "Action item 0", "html_url": "u1"}),
                _json(401, {"message": "Bad credentials"}),
                _json(422, {"message": "Validation Failed"}),
            ]
        )
        transport(
            [
                (_gql("projectV2(number"), _board()),
                (_is_issue_post, lambda: next(posts)),
                (_gql("addProjectV2ItemById"), _json(200, {"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_1"}}}})),
                (_gql("updateProjectV2ItemFieldValue"), _json(200, {"data": {}})),
            ]
        )
        tool = _by_name(mt.make_github_tools(USER, "github-conn"), "create_project_tasks")
        result = await _call(tool, project="org/7", repository="org/platform", tasks=_tasks(3), confirm=True)
        # A hook-driven retry would re-create issue 0, so this must not look like a 401.
        assert not looks_like_auth_failure(result)
        out = _payload(result)
        assert out["status"] == "partial"
        assert [c["number"] for c in out["created"]] == [1] and out["created"][0]["on_board"] is True
        assert [f["status"] for f in out["failed"]] == [401, 422]
        assert "1 issue(s)" in out["message"] and "2 could not" in out["message"]

    @pytest.mark.asyncio
    async def test_board_step_failures_are_warnings_beside_the_created_issue(self, transport):
        adds = iter(
            [
                _json(200, {"errors": [{"type": "FORBIDDEN", "message": "Resource not accessible by integration"}]}),
                _json(200, {"data": {"addProjectV2ItemById": {"item": {"id": "PVTI_2"}}}}),
            ]
        )
        counter = iter([1, 2])
        transport(
            [
                (_gql("projectV2(number"), _board()),
                (
                    _is_issue_post,
                    lambda: _json(201, {"number": (n := next(counter)), "node_id": f"I_{n}", "title": f"Action item {n - 1}", "html_url": f"u{n}"}),
                ),
                (_gql("addProjectV2ItemById"), lambda: next(adds)),
                (_gql("updateProjectV2ItemFieldValue"), _json(401, {"message": "Bad credentials"})),
            ]
        )
        tool = _by_name(mt.make_github_tools(USER, "github-conn"), "create_project_tasks")
        result = await _call(tool, project="org/7", repository="org/platform", tasks=_tasks(2), confirm=True)
        assert not looks_like_auth_failure(result)
        out = _payload(result)
        assert out["status"] == "partial" and out["failed"] == []
        assert [(c["number"], c["on_board"], c["fields"]) for c in out["created"]] == [(1, False, {}), (2, True, {})]
        assert [(w["number"], w["stage"]) for w in out["warnings"]] == [(1, "add_to_project"), (2, "set_fields")]
        assert "Resource not accessible" in out["warnings"][0]["detail"]
        assert "401" in out["warnings"][1]["detail"]
        assert "2 were created but not fully placed" in out["message"]

    @pytest.mark.asyncio
    async def test_missing_connector_on_the_write_tool(self):
        tool = _by_name(mt.make_github_tools(USER, None), "create_project_tasks")
        assert "github_issues_local" in _text(await _call(tool, project="org/7", repository="org/platform", tasks=_tasks(1)))
