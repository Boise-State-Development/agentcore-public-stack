"""The read gate agrees with the write gate for share editors.

``get_assistant_with_access_check`` (the read that loads the Agent Designer) used to
gate purely on visibility, while ``resolve_assistant_permission`` (the PUT) gates on
share records. An editor share therefore saved fine but loaded as a viewer on a PUBLIC
agent, and as a 403 on a PRIVATE one. These run the real service against moto.
"""

import pytest

from apis.shared.assistants.service import (
    create_assistant,
    get_assistant_with_access_check,
    resolve_assistant_permission,
    share_assistant,
)

OWNER = "owner-1"
EDITOR_EMAIL = "editor@test.edu"
VIEWER_EMAIL = "viewer@test.edu"


@pytest.fixture(autouse=True)
def _env(assistants_table, monkeypatch):
    monkeypatch.setenv("S3_ASSISTANTS_VECTOR_STORE_INDEX_NAME", "test-index")


async def _agent(visibility: str) -> str:
    created = await create_assistant(
        owner_id=OWNER, owner_name="Owner", name="Bot",
        description="d", instructions="secret instructions", visibility=visibility,
    )
    await share_assistant(created.assistant_id, OWNER, [EDITOR_EMAIL], permission="editor")
    await share_assistant(created.assistant_id, OWNER, [VIEWER_EMAIL], permission="viewer")
    return created.assistant_id


@pytest.mark.asyncio
@pytest.mark.parametrize("visibility", ["PRIVATE", "SHARED", "PUBLIC"])
async def test_designer_read_matches_write_for_editor(visibility):
    agent_id = await _agent(visibility)
    _, write_perm = await resolve_assistant_permission(agent_id, "editor-1", EDITOR_EMAIL)
    assistant, read_perm = await get_assistant_with_access_check(
        agent_id, "editor-1", EDITOR_EMAIL, resolve_public_share=True
    )
    assert write_perm == "editor"
    assert assistant is not None
    assert read_perm == "editor"


@pytest.mark.asyncio
async def test_private_editor_share_resolves_without_the_flag():
    agent_id = await _agent("PRIVATE")
    assistant, perm = await get_assistant_with_access_check(agent_id, "editor-1", EDITOR_EMAIL)
    assert assistant is not None
    assert perm == "editor"


@pytest.mark.asyncio
async def test_private_viewer_share_is_still_denied():
    agent_id = await _agent("PRIVATE")
    assistant, perm = await get_assistant_with_access_check(
        agent_id, "viewer-1", VIEWER_EMAIL, resolve_public_share=True
    )
    assert (assistant, perm) == (None, None)


@pytest.mark.asyncio
async def test_private_stranger_is_denied():
    agent_id = await _agent("PRIVATE")
    assistant, perm = await get_assistant_with_access_check(agent_id, "x", "stranger@test.edu")
    assert (assistant, perm) == (None, None)


@pytest.mark.asyncio
async def test_public_without_flag_stays_viewer_for_editor():
    # The chat-turn path does not opt in, so it pays no share read and sees "viewer".
    agent_id = await _agent("PUBLIC")
    assistant, perm = await get_assistant_with_access_check(agent_id, "editor-1", EDITOR_EMAIL)
    assert assistant is not None
    assert perm == "viewer"


@pytest.mark.asyncio
async def test_public_stranger_is_viewer_with_flag():
    agent_id = await _agent("PUBLIC")
    assistant, perm = await get_assistant_with_access_check(
        agent_id, "x", "stranger@test.edu", resolve_public_share=True
    )
    assert assistant is not None
    assert perm == "viewer"


@pytest.mark.asyncio
async def test_public_viewer_share_with_flag_is_viewer():
    agent_id = await _agent("PUBLIC")
    _, perm = await get_assistant_with_access_check(
        agent_id, "viewer-1", VIEWER_EMAIL, resolve_public_share=True
    )
    assert perm == "viewer"


@pytest.mark.asyncio
async def test_owner_is_owner_everywhere():
    for visibility in ("PRIVATE", "SHARED", "PUBLIC"):
        agent_id = await _agent(visibility)
        _, perm = await get_assistant_with_access_check(
            agent_id, OWNER, "owner@test.edu", resolve_public_share=True
        )
        assert perm == "owner"
