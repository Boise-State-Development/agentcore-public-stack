"""Agent Marketplace Phase 6 — role-seeded default pins, storage side (D9).

Through a real (moto) table, because the stored shape *is* the contract: the resolver on
the user side reads these items on every pin request, and the RBAC repository writes and
deletes in the same partition.

The case worth the whole file is ``test_updating_a_role_preserves_its_default_pins``.
``AppRoleRepository.update_role`` rebuilds a role's mapping items by deleting them first,
and it used to delete *everything* under ``PK=ROLE#{id}`` except the definition. Pins are
deliberately not part of the ``AppRole`` record — a pin is not a permission — so nothing
would have rewritten them: every edit to a role's name or grants would silently empty its
seed list.
"""

import boto3
import pytest
from moto import mock_aws

from apis.shared.assistants.models import RoleAgentPinInput
from apis.shared.assistants.role_pins import (
    MAX_ROLE_PINS,
    delete_role_pins,
    list_pins_for_roles,
    list_role_pins,
    put_role_pins,
)
from apis.shared.rbac.models import AppRole
from apis.shared.rbac.repository import AppRoleRepository

REGION = "us-east-1"
TABLE = "test-app-roles"
ADMIN = "admin-1"


def _inputs(*specs):
    """``("ast-001", True)`` or just ``"ast-001"`` → pin inputs, in order."""
    out = []
    for spec in specs:
        agent_id, locked = spec if isinstance(spec, tuple) else (spec, False)
        out.append(RoleAgentPinInput(agent_id=agent_id, locked=locked))
    return out


@pytest.fixture()
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("DYNAMODB_APP_ROLES_TABLE_NAME", TABLE)
    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName=TABLE,
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield ddb.Table(TABLE)


# ── the stored shape ─────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_a_role_with_no_pins_reads_as_empty(table):
    assert await list_role_pins("faculty") == []


@pytest.mark.asyncio
async def test_pins_are_stored_beside_the_grant_items(table):
    await put_role_pins("faculty", _inputs("ast-001"), updated_by=ADMIN)

    item = table.get_item(Key={"PK": "ROLE#faculty", "SK": "AGENT_PIN#ast-001"})["Item"]
    assert item["order"] == 0
    assert item["locked"] is False
    assert item["createdBy"] == ADMIN
    assert "createdAt" in item


@pytest.mark.asyncio
async def test_the_saved_order_is_the_list_order(table):
    await put_role_pins("faculty", _inputs("ast-003", "ast-001", "ast-002"), updated_by=ADMIN)

    pins = await list_role_pins("faculty")
    assert [pin.agent_id for pin in pins] == ["ast-003", "ast-001", "ast-002"]
    assert [pin.order for pin in pins] == [0, 1, 2]


@pytest.mark.asyncio
async def test_saving_replaces_the_whole_list(table):
    await put_role_pins("faculty", _inputs("ast-001", "ast-002"), updated_by=ADMIN)
    await put_role_pins("faculty", _inputs("ast-002"), updated_by=ADMIN)

    assert [pin.agent_id for pin in await list_role_pins("faculty")] == ["ast-002"]


@pytest.mark.asyncio
async def test_reordering_keeps_when_an_agent_was_seeded(table):
    """``createdAt`` records the seed, not the last drag — otherwise the audit trail lies."""
    await put_role_pins("faculty", _inputs("ast-001", "ast-002"), updated_by=ADMIN)
    original = {pin.agent_id: pin.created_at for pin in await list_role_pins("faculty")}

    await put_role_pins("faculty", _inputs("ast-002", "ast-001"), updated_by="admin-2")

    after = {pin.agent_id: pin for pin in await list_role_pins("faculty")}
    assert after["ast-001"].created_at == original["ast-001"]
    assert after["ast-001"].created_by == ADMIN
    assert after["ast-002"].order == 0


@pytest.mark.asyncio
async def test_lock_round_trips(table):
    await put_role_pins("faculty", _inputs(("ast-001", True), "ast-002"), updated_by=ADMIN)

    pins = {pin.agent_id: pin.locked for pin in await list_role_pins("faculty")}
    assert pins == {"ast-001": True, "ast-002": False}


@pytest.mark.asyncio
async def test_pins_are_per_role(table):
    await put_role_pins("faculty", _inputs("ast-001"), updated_by=ADMIN)
    await put_role_pins("staff", _inputs("ast-002"), updated_by=ADMIN)

    by_role = await list_pins_for_roles(["faculty", "staff", "student"])
    assert [pin.agent_id for pin in by_role["faculty"]] == ["ast-001"]
    assert [pin.agent_id for pin in by_role["staff"]] == ["ast-002"]
    assert by_role["student"] == []


# ── refusals ─────────────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_past_the_ceiling_is_refused(table):
    too_many = _inputs(*[f"ast-{index:03d}" for index in range(MAX_ROLE_PINS + 1)])

    with pytest.raises(ValueError) as excinfo:
        await put_role_pins("faculty", too_many, updated_by=ADMIN)

    assert str(MAX_ROLE_PINS) in str(excinfo.value)
    assert await list_role_pins("faculty") == []


@pytest.mark.asyncio
async def test_the_same_agent_twice_is_refused(table):
    """A duplicate would collapse to one item on write and renumber everything after it."""
    with pytest.raises(ValueError):
        await put_role_pins("faculty", _inputs("ast-001", "ast-001"), updated_by=ADMIN)


# ── the partition it shares with the grants ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_updating_a_role_preserves_its_default_pins(table):
    """The trap: ``update_role`` rebuilds mapping items, and a pin is not one of them."""
    repository = AppRoleRepository(table_name=TABLE)
    role = AppRole(
        role_id="faculty",
        display_name="Faculty",
        description="Teaching staff",
        jwt_role_mappings=["Faculty"],
        granted_tools=["web_search"],
    )
    await repository.create_role(role)
    await put_role_pins("faculty", _inputs("ast-001", "ast-002"), updated_by=ADMIN)

    role.display_name = "Faculty (renamed)"
    role.granted_tools = ["web_search", "calculator"]
    await repository.update_role(role)

    assert [pin.agent_id for pin in await list_role_pins("faculty")] == ["ast-001", "ast-002"]
    # And the grants it *does* own were still rebuilt.
    assert (await repository.get_role("faculty")).granted_tools == ["web_search", "calculator"]


@pytest.mark.asyncio
async def test_deleting_a_role_takes_its_default_pins_with_it(table):
    repository = AppRoleRepository(table_name=TABLE)
    await repository.create_role(
        AppRole(role_id="temp", display_name="Temp", description="", jwt_role_mappings=["Temp"])
    )
    await put_role_pins("temp", _inputs("ast-001"), updated_by=ADMIN)

    await repository.delete_role("temp")

    assert await list_role_pins("temp") == []


@pytest.mark.asyncio
async def test_delete_role_pins_clears_the_list(table):
    await put_role_pins("faculty", _inputs("ast-001", "ast-002"), updated_by=ADMIN)

    await delete_role_pins("faculty")

    assert await list_role_pins("faculty") == []


# ── #748 — locked-seed friction, not a cap ───────────────────────────────────────────
class TestCountLockedOutside:
    """What the admin console cannot work out for itself: what *other* roles lock.

    Deliberately a count and not a limit. Pins merge as a union across every role a user
    matches and a lock from any one of them wins, so a per-role cap would not bound what
    an individual sees — and the union itself is uncappable, because role membership
    resolves per user from Entra claims. Reporting is the honest thing this surface can do.
    """

    @staticmethod
    def _roles(*role_ids):
        """Patch the admin service's role listing to exactly these ids."""
        from unittest.mock import AsyncMock, patch

        from apis.shared.rbac.models import AppRole as _AppRole

        service = AsyncMock()
        service.list_roles = AsyncMock(
            return_value=[
                _AppRole(role_id=rid, display_name=rid, description="") for rid in role_ids
            ]
        )
        return patch(
            "apis.shared.rbac.admin_service.get_app_role_admin_service",
            return_value=service,
        )

    @pytest.mark.asyncio
    async def test_counts_locked_seeds_on_other_roles_only(self, table):
        from apis.shared.assistants.role_pins import count_locked_outside

        await put_role_pins("faculty", _inputs(("ast-1", True), ("ast-2", True)), ADMIN)
        await put_role_pins("staff", _inputs(("ast-3", True), "ast-4"), ADMIN)

        with self._roles("faculty", "staff"):
            total, roles = await count_locked_outside("faculty")

        # Only staff's single locked seed — faculty's own two are the caller's business.
        assert (total, roles) == (1, 1)

    @pytest.mark.asyncio
    async def test_unlocked_seeds_elsewhere_do_not_count(self, table):
        from apis.shared.assistants.role_pins import count_locked_outside

        await put_role_pins("staff", _inputs("ast-3", "ast-4"), ADMIN)

        with self._roles("faculty", "staff"):
            assert await count_locked_outside("faculty") == (0, 0)

    @pytest.mark.asyncio
    async def test_reports_the_spread_across_roles(self, table):
        """Two roles locking one each is a different shape from one locking two."""
        from apis.shared.assistants.role_pins import count_locked_outside

        await put_role_pins("staff", _inputs(("ast-1", True)), ADMIN)
        await put_role_pins("student", _inputs(("ast-2", True), ("ast-3", True)), ADMIN)

        with self._roles("faculty", "staff", "student"):
            assert await count_locked_outside("faculty") == (3, 2)

    @pytest.mark.asyncio
    async def test_a_lone_role_has_nothing_outside_it(self, table):
        from apis.shared.assistants.role_pins import count_locked_outside

        await put_role_pins("faculty", _inputs(("ast-1", True)), ADMIN)

        with self._roles("faculty"):
            assert await count_locked_outside("faculty") == (0, 0)

    @pytest.mark.asyncio
    async def test_a_failure_to_list_roles_never_breaks_the_page(self, table):
        """Friction copy is advisory — it must not take the pins surface down with it."""
        from unittest.mock import AsyncMock, patch

        from apis.shared.assistants.role_pins import count_locked_outside

        service = AsyncMock()
        service.list_roles = AsyncMock(side_effect=RuntimeError("dynamo is having a day"))
        with patch(
            "apis.shared.rbac.admin_service.get_app_role_admin_service",
            return_value=service,
        ):
            assert await count_locked_outside("faculty") == (0, 0)
