"""PrefixFingerprintHook — per-model-call prompt-cache prefix hashes."""

from types import SimpleNamespace

from agents.main_agent.session.hooks.prefix_fingerprint import (
    PrefixFingerprintHook,
    get_prefix_fingerprint,
    reset_prefix_fingerprints,
)


def _agent(tool_specs=None, system_prompt="You are helpful.", messages=None):
    return SimpleNamespace(
        tool_registry=SimpleNamespace(
            get_all_tool_specs=lambda: tool_specs if tool_specs is not None else []
        ),
        system_prompt=system_prompt,
        messages=messages if messages is not None else [],
    )


def _fire(agent):
    PrefixFingerprintHook()._on_before_model_call(SimpleNamespace(agent=agent))


def _msg(role, text):
    return {"role": role, "content": [{"text": text}]}


class TestFingerprintCapture:
    def test_appends_one_entry_per_model_call(self):
        agent = _agent(messages=[_msg("user", "hi")])
        reset_prefix_fingerprints(agent)
        _fire(agent)
        agent.messages.append(_msg("assistant", "hello"))
        agent.messages.append(_msg("user", "more"))
        _fire(agent)

        first = get_prefix_fingerprint(agent, 0)
        second = get_prefix_fingerprint(agent, 1)
        assert first is not None and second is not None
        assert first["messageCount"] == 1
        assert second["messageCount"] == 3
        # Latest-entry fallback used by single-call persistence paths.
        assert get_prefix_fingerprint(agent, None) == second

    def test_fingerprint_shape(self):
        agent = _agent(tool_specs=[{"name": "t1"}], messages=[_msg("user", "hi")])
        reset_prefix_fingerprints(agent)
        _fire(agent)
        fp = get_prefix_fingerprint(agent, 0)
        assert set(fp) == {
            "toolConfigHash",
            "systemPromptHash",
            "historyHash",
            "messageCount",
        }
        assert all(len(fp[k]) == 16 for k in ("toolConfigHash", "systemPromptHash", "historyHash"))

    def test_history_hash_excludes_newest_message(self):
        # Two calls whose histories share the same prefix but different
        # newest message must produce the SAME historyHash: for a cache hit
        # the prior prefix is what must match.
        shared_prefix = [_msg("user", "hi"), _msg("assistant", "hello")]
        a = _agent(messages=shared_prefix + [_msg("user", "question A")])
        b = _agent(messages=shared_prefix + [_msg("user", "question B")])
        reset_prefix_fingerprints(a)
        reset_prefix_fingerprints(b)
        _fire(a)
        _fire(b)
        assert (
            get_prefix_fingerprint(a, 0)["historyHash"]
            == get_prefix_fingerprint(b, 0)["historyHash"]
        )

    def test_tool_order_flip_changes_tool_config_hash(self):
        specs = [{"name": "alpha"}, {"name": "beta"}]
        a = _agent(tool_specs=list(specs))
        b = _agent(tool_specs=list(reversed(specs)))
        reset_prefix_fingerprints(a)
        reset_prefix_fingerprints(b)
        _fire(a)
        _fire(b)
        assert (
            get_prefix_fingerprint(a, 0)["toolConfigHash"]
            != get_prefix_fingerprint(b, 0)["toolConfigHash"]
        )

    def test_structured_system_prompt_supported(self):
        # AgentSkills' block-level injection can turn system_prompt into a
        # list of SystemContentBlock dicts.
        agent = _agent(system_prompt=[{"text": "base"}, {"text": "<available_skills/>"}])
        reset_prefix_fingerprints(agent)
        _fire(agent)
        assert get_prefix_fingerprint(agent, 0)["systemPromptHash"]


class TestLifecycleAndSafety:
    def test_reset_clears_previous_turn(self):
        agent = _agent()
        reset_prefix_fingerprints(agent)
        _fire(agent)
        reset_prefix_fingerprints(agent)
        assert get_prefix_fingerprint(agent, 0) is None
        assert get_prefix_fingerprint(agent, None) is None

    def test_out_of_range_index_returns_none(self):
        agent = _agent()
        reset_prefix_fingerprints(agent)
        _fire(agent)
        assert get_prefix_fingerprint(agent, 5) is None

    def test_never_raises_on_broken_agent(self):
        class _Broken:
            @property
            def tool_registry(self):
                raise RuntimeError("boom")

        broken = _Broken()
        _fire(broken)  # must swallow
        assert get_prefix_fingerprint(broken, None) is None

    def test_works_without_prior_reset(self):
        agent = _agent()
        _fire(agent)
        assert get_prefix_fingerprint(agent, 0) is not None
