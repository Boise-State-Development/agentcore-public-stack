"""The coordinator sizes the 1h static prefix from the context breakdown (PR-5)."""

from types import SimpleNamespace

from agents.main_agent.streaming.stream_coordinator import StreamCoordinator


class _Strands:
    pass


def _wrapper(long_ttl):
    return SimpleNamespace(model_config=SimpleNamespace(long_ttl_static_prefix=lambda: long_ttl))


def _with_breakdown(system, tools):
    from agents.main_agent.session.hooks import context_attribution as ca
    agent = _Strands()
    setattr(agent, ca._BREAKDOWN_ATTR, {
        "total": system + tools + 500,
        "partitions": [
            {"key": "system", "label": "System prompt", "tokens": system},
            {"key": "tools", "label": "Tools", "tokens": tools},
            {"key": "messages", "label": "Messages", "tokens": 500},
        ],
    })
    return agent


def test_returns_system_plus_tools_when_arm_is_on():
    assert StreamCoordinator._long_ttl_static_prefix_tokens(_wrapper(True), _with_breakdown(8_000, 3_000)) == 11_000


def test_none_when_arm_is_off():
    assert StreamCoordinator._long_ttl_static_prefix_tokens(_wrapper(False), _with_breakdown(8_000, 3_000)) is None


def test_none_without_breakdown_or_wrapper():
    assert StreamCoordinator._long_ttl_static_prefix_tokens(_wrapper(True), _Strands()) is None
    assert StreamCoordinator._long_ttl_static_prefix_tokens(None, _with_breakdown(1, 1)) is None
    assert StreamCoordinator._long_ttl_static_prefix_tokens(_wrapper(True), None) is None
