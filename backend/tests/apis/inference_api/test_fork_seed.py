"""A fork's first turn starts from its parent's live history (rewind/fork spec §7).

The stored copy restores to different bytes than the parent sent (long-term
memory context is sent, never stored), so only the live list can give the fork
a prompt-cache hit on its shared prefix. These pin when the seed is used, and
every case where it must not be.
"""

from types import SimpleNamespace

import pytest

from apis.inference_api.chat import service
from apis.shared.sessions.fork import ForkOrigin

USER = "user-1"
PARENT = "parent-session"
CHILD = "child-session"


@pytest.fixture(autouse=True)
def _fork_on(monkeypatch):
    monkeypatch.setenv("SESSION_FORK_ENABLED", "true")
    service.clear_agent_cache()
    yield
    service.clear_agent_cache()


def _conversation(turns, with_context=False):
    messages = []
    for i in range(turns):
        content = [{"text": f"question {i}"}]
        if with_context:
            content.insert(0, {"text": "<user_context>likes concise answers</user_context>"})
        messages.append({"role": "user", "content": content})
        messages.append({"role": "assistant", "content": [{"text": f"answer {i}"}]})
    return messages


def _agent(messages, *, origin=None, offset=0, rewind_epoch=0):
    session_manager = SimpleNamespace(
        _live_offset=offset,
        _live_conversation=SimpleNamespace(rewind_epoch=rewind_epoch),
        _pending_fork_marker=None,
        fork_origin=lambda: origin,
    )
    return SimpleNamespace(agent=SimpleNamespace(messages=messages), session_manager=session_manager)


def _cache_parent(parent):
    service._agent_cache[(PARENT, USER, "k")] = parent


def test_seeds_from_the_parents_live_bytes_including_what_storage_lost():
    live = _conversation(3, with_context=True)
    _cache_parent(_agent(live))
    restored = _conversation(2)  # the stored copy: no <user_context>
    child = _agent(restored, origin=ForkOrigin(PARENT, message_index=4, cut=4))

    service._seed_forked_conversation(child, CHILD, USER)

    assert child.agent.messages == live[:4]
    assert child.agent.messages[0]["content"][0]["text"].startswith("<user_context>")
    # A copy: the fork's next turn must not write into the parent's thread.
    assert child.agent.messages is not live
    child.agent.messages.append({"role": "user", "content": [{"text": "new"}]})
    assert len(live) == 6
    assert child.session_manager._pending_fork_marker == {"forkedFrom": PARENT, "forkSeeded": True}


def test_keeps_the_stored_copy_when_the_parent_is_not_in_this_process():
    restored = _conversation(2)
    child = _agent(restored, origin=ForkOrigin(PARENT, message_index=4, cut=4))

    service._seed_forked_conversation(child, CHILD, USER)

    assert child.agent.messages is restored
    assert child.session_manager._pending_fork_marker == {"forkedFrom": PARENT, "forkSeeded": False}


@pytest.mark.parametrize(
    "parent_kwargs, origin_kwargs",
    [
        ({"offset": 2}, {}),  # the parent compacted since the fork
        ({"rewind_epoch": 1}, {}),  # the parent was rewound since the fork
        ({}, {"parent_rewind_epoch": 1}),  # the fork was cut against a rewind the live list does not reflect
    ],
)
def test_keeps_the_stored_copy_when_the_coordinates_differ(parent_kwargs, origin_kwargs):
    _cache_parent(_agent(_conversation(3, with_context=True), **parent_kwargs))
    restored = _conversation(2)
    child = _agent(restored, origin=ForkOrigin(PARENT, message_index=4, cut=4, **origin_kwargs))

    service._seed_forked_conversation(child, CHILD, USER)

    assert child.agent.messages is restored


def test_keeps_the_stored_copy_when_the_turns_disagree():
    live = _conversation(3)
    live[2]["content"][-1]["text"] = "a different question"
    _cache_parent(_agent(live))
    restored = _conversation(2)
    child = _agent(restored, origin=ForkOrigin(PARENT, message_index=4, cut=4))

    service._seed_forked_conversation(child, CHILD, USER)

    assert child.agent.messages is restored


def test_never_seeds_after_the_forks_first_turn():
    _cache_parent(_agent(_conversation(3, with_context=True)))
    restored = _conversation(3)  # the fork's own turn already landed
    child = _agent(restored, origin=ForkOrigin(PARENT, message_index=4, cut=4))

    service._seed_forked_conversation(child, CHILD, USER)

    assert child.agent.messages is restored
    assert child.session_manager._pending_fork_marker is None


def test_never_seeds_from_another_users_parent():
    service._agent_cache[(PARENT, "someone-else", "k")] = _agent(_conversation(3, with_context=True))
    restored = _conversation(2)
    child = _agent(restored, origin=ForkOrigin(PARENT, message_index=4, cut=4))

    service._seed_forked_conversation(child, CHILD, USER)

    assert child.agent.messages is restored


def test_a_session_that_is_not_a_fork_is_untouched():
    restored = _conversation(2)
    child = _agent(restored)

    service._seed_forked_conversation(child, CHILD, USER)

    assert child.agent.messages is restored
    assert child.session_manager._pending_fork_marker is None


def test_does_nothing_with_the_flag_off(monkeypatch):
    monkeypatch.setenv("SESSION_FORK_ENABLED", "false")
    _cache_parent(_agent(_conversation(3, with_context=True)))
    restored = _conversation(2)
    child = _agent(restored, origin=ForkOrigin(PARENT, message_index=4, cut=4))

    service._seed_forked_conversation(child, CHILD, USER)

    assert child.agent.messages is restored
