"""Tests for scoped tool identifiers (apis.shared.tools.scoped_ids).

A bare catalog id means "the whole MCP server"; a ``cat::tool`` id means one
tool of that server. These helpers are the single source of truth for that
format, shared by app_api validation and the agents runtime.
"""

from apis.shared.tools.scoped_ids import (
    SCOPE_DELIMITER,
    base_tool_id,
    base_tool_ids,
    collect_tool_name_filters,
    is_scoped_tool_id,
    make_scoped_tool_id,
    parse_scoped_tool_id,
)


def test_is_scoped_tool_id():
    assert is_scoped_tool_id("fetch_url_content::fetch") is True
    assert is_scoped_tool_id("fetch_url_content") is False
    assert is_scoped_tool_id("gateway_class_search") is False


def test_make_scoped_tool_id_roundtrips():
    scoped = make_scoped_tool_id("gateway_class_search", "search")
    assert scoped == f"gateway_class_search{SCOPE_DELIMITER}search"
    assert parse_scoped_tool_id(scoped) == ("gateway_class_search", "search")


def test_parse_bare_id():
    assert parse_scoped_tool_id("fetch_url_content") == ("fetch_url_content", None)


def test_parse_scoped_id():
    assert parse_scoped_tool_id("server::do_thing") == ("server", "do_thing")


def test_parse_strips_whitespace_and_treats_empty_name_as_whole_server():
    assert parse_scoped_tool_id("server::  ") == ("server", None)
    assert parse_scoped_tool_id("server:: do_thing ") == ("server", "do_thing")


def test_parse_only_splits_on_first_delimiter():
    # Defensive: an (unlikely) tool name containing the delimiter is preserved.
    assert parse_scoped_tool_id("server::a::b") == ("server", "a::b")


def test_base_tool_id():
    assert base_tool_id("server::tool") == "server"
    assert base_tool_id("server") == "server"


def test_base_tool_ids_dedupes_order_preserving():
    ids = ["server::a", "server::b", "other", "server", "other::x"]
    assert base_tool_ids(ids) == ["server", "other"]


def test_collect_filters_subset_and_whole():
    filters = collect_tool_name_filters(["a", "b::x", "b::y", "local_tool"])
    assert filters == {"b": {"x", "y"}, "a": None, "local_tool": None}


def test_collect_filters_whole_server_wins_over_subset():
    # A bare id alongside scoped ids for the same server means the whole server
    # is selected — the subset is subsumed, not intersected.
    filters = collect_tool_name_filters(["c::z", "c"])
    assert filters == {"c": None}


def test_collect_filters_empty():
    assert collect_tool_name_filters([]) == {}
