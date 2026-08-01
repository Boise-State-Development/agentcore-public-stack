"""CI determinism guard for the Bedrock prompt-cache prefix.

Bedrock prompt caching is exact-prefix-match: if the system prompt or
toolConfig differs between two turns of the same session, the cache is
re-written at a premium. A prod audit (session aecd387d, $1.60) traced 75%
of spend to exactly that — skill records reaching the ``<available_skills>``
block in nondeterministic order, and RBAC grant unions materialized from
sets (hash-randomized iteration order).

This guard builds the chat-agent surface twice from *shuffled* skill and
tool record orders and asserts the production prefix fingerprints
(``PrefixFingerprintHook`` — the same hashes persisted on every cost row)
are identical. It complements the targeted sorting fixes in
``apis.shared.skills.repository``, ``apis.shared.rbac.service``, and
``agents.main_agent.skills.strands_mapping``: any future assembly stage
that reintroduces order instability fails here.
"""

import random
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from strands import Agent, tool
from strands.hooks import BeforeInvocationEvent

from apis.shared.rbac.models import EffectivePermissions

from agents.main_agent.session.hooks.prefix_fingerprint import (
    PrefixFingerprintHook,
    get_prefix_fingerprint,
    reset_prefix_fingerprints,
)
from agents.main_agent.skills import strands_mapping as sm
from apis.shared.rbac.service import AppRoleService


# ---------------------------------------------------------------------------
# Fixture data: skill records + local tools
# ---------------------------------------------------------------------------

def _skill_record(skill_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        skill_id=skill_id,
        display_name=skill_id.replace("_", " ").title(),
        description=f"Description for {skill_id}.",
        instructions=f"# {skill_id}\nInstructions body.",
        allowed_tools=[],
        skill_metadata={},
        resources=[],
        status="active",
    )


SKILL_RECORDS = [
    _skill_record("zeta_skill"),
    _skill_record("alpha_skill"),
    _skill_record("midway_skill"),
    _skill_record("beta_skill"),
]


@tool
def alpha_tool(query: str) -> str:
    """Alpha tool."""
    return query


@tool
def beta_tool(query: str) -> str:
    """Beta tool."""
    return query


@tool
def gamma_tool(query: str) -> str:
    """Gamma tool."""
    return query


TOOLS_BY_ID = {"alpha_tool": alpha_tool, "beta_tool": beta_tool, "gamma_tool": gamma_tool}


def _role(role_id: str, tools: list, skills: list, priority: int = 10) -> SimpleNamespace:
    # `effective_permissions` uses the real dataclass rather than a namespace:
    # a hand-rolled stand-in silently loses any field added to the real type,
    # and the merge under test reads every axis off it.
    return SimpleNamespace(
        role_id=role_id,
        priority=priority,
        effective_permissions=EffectivePermissions(
            tools=tools, models=[], skills=skills, quota_tier=None
        ),
    )


# ---------------------------------------------------------------------------
# Agent assembly mirroring the production pipeline stages under test:
# RBAC merge (grant union → enabled ids) → skills runtime (records →
# AgentSkills plugin) → Agent construction → plugin system-prompt injection
# → PrefixFingerprintHook (the persisted hashes).
# ---------------------------------------------------------------------------

async def _build_and_fingerprint(monkeypatch, rng: random.Random) -> dict:
    # RBAC seam: roles and their granted lists in shuffled order — the union
    # used to be materialized from a set, so order leaked hash randomization.
    role_a_tools = ["alpha_tool", "beta_tool"]
    role_b_tools = ["gamma_tool", "beta_tool"]
    role_skills = [r.skill_id for r in SKILL_RECORDS]
    rng.shuffle(role_a_tools)
    rng.shuffle(role_b_tools)
    rng.shuffle(role_skills)
    roles = [
        _role("role-a", role_a_tools, role_skills[:2]),
        _role("role-b", role_b_tools, role_skills[2:]),
    ]
    rng.shuffle(roles)
    merged = AppRoleService._merge_permissions(None, user_id="user-1", roles=roles)

    # Skills seam: fetch returns records in shuffled (nondeterministic) order.
    shuffled_records = list(SKILL_RECORDS)
    rng.shuffle(shuffled_records)
    monkeypatch.setattr(sm, "fetch_active_skill_records", lambda ids: shuffled_records)
    plugin, read_tool = sm.build_skills_runtime(merged.skills)

    tools = [TOOLS_BY_ID[tool_id] for tool_id in merged.tools if tool_id in TOOLS_BY_ID]
    tools.append(read_tool)

    model = MagicMock()
    model.config = {}
    agent = Agent(
        model=model,
        system_prompt="You are the campus assistant.",
        tools=tools,
        plugins=[plugin],
    )

    # Drive the plugin's system-prompt injection the way a real invocation
    # does, then capture the same fingerprints production persists per call.
    await plugin._on_before_invocation(BeforeInvocationEvent(agent=agent))
    reset_prefix_fingerprints(agent)
    PrefixFingerprintHook()._on_before_model_call(SimpleNamespace(agent=agent))

    fingerprint = get_prefix_fingerprint(agent, 0)
    assert fingerprint is not None, "fingerprint hook failed to capture"
    return fingerprint


@pytest.mark.asyncio
async def test_agent_prefix_identical_across_shuffled_record_orders(monkeypatch):
    fp_a = await _build_and_fingerprint(monkeypatch, random.Random(1234))
    fp_b = await _build_and_fingerprint(monkeypatch, random.Random(987654))

    assert fp_a["systemPromptHash"] == fp_b["systemPromptHash"], (
        "System prompt differs between two builds from shuffled skill/role "
        "record orders — a nondeterministic assembly stage is back and will "
        "invalidate the Bedrock prompt cache every turn."
    )
    assert fp_a["toolConfigHash"] == fp_b["toolConfigHash"], (
        "toolConfig differs between two builds from shuffled tool-grant "
        "orders — a nondeterministic assembly stage is back and will "
        "invalidate the Bedrock prompt cache every turn."
    )


@pytest.mark.asyncio
async def test_skills_xml_actually_present_in_fingerprinted_prompt(monkeypatch):
    # Guard the guard: if AgentSkills ever stops injecting before the hook
    # captures, the system-prompt assertion above would trivially pass.
    monkeypatch.setattr(sm, "fetch_active_skill_records", lambda ids: list(SKILL_RECORDS))
    plugin, read_tool = sm.build_skills_runtime([r.skill_id for r in SKILL_RECORDS])
    model = MagicMock()
    model.config = {}
    agent = Agent(model=model, system_prompt="Base.", tools=[read_tool], plugins=[plugin])
    await plugin._on_before_invocation(BeforeInvocationEvent(agent=agent))
    prompt_text = str(agent.system_prompt)
    assert "available_skills" in prompt_text
    assert "alpha-skill" in prompt_text
