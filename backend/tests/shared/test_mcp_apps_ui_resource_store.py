"""Tests for the model-initiated MCP App UI-resource store (SEP-1865).

The store reuses the existing `sessions-metadata` table. No DynamoDB in
tests — the no-table path is a silent no-op (matches dev), and a fake table
asserts the record shape, gzip round-trip, the ownership re-check,
last-write-wins keying, and the oversized (compressed) skip.
"""

from __future__ import annotations

import gzip
from decimal import Decimal

from apis.shared.mcp_apps import ui_resource_store as mod
from apis.shared.mcp_apps.ui_resource_store import UiResourceStore


class _FakeTable:
    def __init__(self, items=None) -> None:
        self.items = items or []
        self.puts: list = []

    def put_item(self, Item):  # noqa: N803 - boto3 kwarg name
        self.puts.append(Item)

    def query(self, **kwargs):
        return {"Items": self.items}


def _store_with(table) -> UiResourceStore:
    s = UiResourceStore()  # __init__ sets _table=None without the env var
    s._table = table
    return s


def _store_kwargs(**overrides):
    base = dict(
        user_id="u1",
        session_id="s1",
        tool_use_id="tu1",
        resource_uri="ui://srv/widget",
        html="<h1>hi</h1>",
        mime_type="text/html;profile=mcp-app",
        csp={"connectDomains": ["https://api.test"]},
        permissions={"clipboardWrite": {}},
        sandbox_origin="https://sandbox.example",
    )
    base.update(overrides)
    return base


def test_no_table_is_silent_noop():
    s = UiResourceStore()
    assert s.enabled is False
    # Must not raise.
    s.store(**_store_kwargs())
    assert s.list_for_session(session_id="s1", user_id="u1") == []


def test_store_writes_uires_record_shape():
    table = _FakeTable()
    s = _store_with(table)
    s.store(**_store_kwargs(produced_by_message_index=3))

    assert len(table.puts) == 1
    item = table.puts[0]
    assert item["PK"] == "USER#u1"
    # Keyed by toolUseId (not a random id) so a re-emit overwrites.
    assert item["SK"] == "UIRES#tu1"
    assert item["GSI_PK"] == "SESSION#s1"
    assert item["GSI_SK"].startswith("UIRES#")
    assert item["toolUseId"] == "tu1"
    assert item["resourceUri"] == "ui://srv/widget"
    # HTML is gzipped into a Binary attribute, not stored raw.
    assert "html" not in item
    assert gzip.decompress(item["htmlGz"]).decode("utf-8") == "<h1>hi</h1>"
    assert item["mimeType"] == "text/html;profile=mcp-app"
    assert item["csp"] == {"connectDomains": ["https://api.test"]}
    assert item["permissions"] == {"clipboardWrite": {}}
    assert item["sandboxOrigin"] == "https://sandbox.example"
    assert item["producedByMessageIndex"] == 3
    assert "ttl" in item


def test_store_skips_when_compressed_exceeds_cap(monkeypatch):
    # A real App over the gzipped cap is skipped (a placeholder would frame as
    # a broken iframe). Drive it with a tiny cap so the test is deterministic
    # and doesn't depend on a hard-to-compress multi-MB fixture.
    monkeypatch.setattr(mod, "_MAX_HTML_GZ_BYTES", 5)
    table = _FakeTable()
    s = _store_with(table)
    s.store(**_store_kwargs(html="<h1>bigger than five bytes once gzipped</h1>"))
    assert table.puts == []


def test_store_compresses_large_html_that_old_raw_cap_rejected():
    table = _FakeTable()
    s = _store_with(table)
    # A 450KB App — over DynamoDB's 400KB item limit raw, but highly
    # compressible — now persists via gzip and round-trips intact.
    html = "<div>" + ("padding " * 60_000) + "</div>"
    assert len(html.encode("utf-8")) > 400_000
    s.store(**_store_kwargs(html=html))
    assert len(table.puts) == 1
    stored = table.puts[0]["htmlGz"]
    assert len(stored) < 400_000  # fits a single DynamoDB item
    assert gzip.decompress(stored).decode("utf-8") == html


def test_store_persists_small_icon_but_drops_large_data_uri():
    table = _FakeTable()
    s = _store_with(table)
    # A small icon (e.g. a URL) round-trips; a large base64 data: URI (the
    # auto-fetched server-manifest logo) is dropped to protect the 400KB item
    # limit — reload then falls back to the glyph.
    s.store(**_store_kwargs(server_name="Excalidraw", icon="https://x/i.png"))
    s.store(
        **_store_kwargs(
            tool_use_id="tu2", icon="data:image/png;base64," + ("A" * 200_000)
        )
    )
    assert table.puts[0]["icon"] == "https://x/i.png"
    assert table.puts[0]["serverName"] == "Excalidraw"
    assert table.puts[1]["icon"] == ""  # large data URI not persisted


def test_store_last_write_wins_same_tool_use_id():
    table = _FakeTable()
    s = _store_with(table)
    s.store(**_store_kwargs(html="<v1/>"))
    s.store(**_store_kwargs(html="<v2/>"))
    # Same SK both times → DynamoDB overwrites; both puts target UIRES#tu1.
    assert [p["SK"] for p in table.puts] == ["UIRES#tu1", "UIRES#tu1"]
    assert gzip.decompress(table.puts[-1]["htmlGz"]).decode("utf-8") == "<v2/>"


def test_list_decompresses_and_filters_by_owner():
    items = [
        {
            "PK": "USER#u1",
            "SK": "UIRES#tu1",
            "GSI_PK": "SESSION#s1",
            "GSI_SK": "UIRES#2026-01-01T00:00:00",
            "ttl": 123,
            "userId": "u1",
            "sessionId": "s1",
            "toolUseId": "tu1",
            "resourceUri": "ui://srv/mine",
            "htmlGz": gzip.compress(b"<h1>mine</h1>"),
            "producedByMessageIndex": Decimal("4"),
        },
        {
            "PK": "USER#someone-else",
            "SK": "UIRES#tu2",
            "GSI_PK": "SESSION#s1",
            "GSI_SK": "UIRES#2026-01-01T00:00:01",
            "userId": "other",
            "toolUseId": "tu2",
            "resourceUri": "ui://srv/not-mine",
            "htmlGz": gzip.compress(b"<h1>not mine</h1>"),
        },
    ]
    s = _store_with(_FakeTable(items))
    resources = s.list_for_session(session_id="s1", user_id="u1")

    assert len(resources) == 1
    res = resources[0]
    assert res["resourceUri"] == "ui://srv/mine"
    # Decompressed back to the inline `html` the SSE-event shape carries.
    assert res["html"] == "<h1>mine</h1>"
    assert "htmlGz" not in res
    # Key attributes are stripped from the returned record.
    for k in ("PK", "SK", "GSI_PK", "GSI_SK", "ttl"):
        assert k not in res
    # Decimals are converted back to native ints.
    assert res["producedByMessageIndex"] == 4
    assert isinstance(res["producedByMessageIndex"], int)
