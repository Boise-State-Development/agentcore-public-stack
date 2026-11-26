"""The deployment the inventory's turns run in: production-sized, seeded through real code.

Everything is written through the same code a deployment uses (the bootstrap seeder,
the repositories, the Projects service) into :class:`FakeAws` before recording starts,
so every row has the shape the turn path reads.

What makes it production-sized:

* the bootstrap catalog: every default tool (24 catalog rows, all granted by the
  default role), the default models, quota tier and the bundled skills;
* a user with long personal instructions (a production-sized system prompt);
* an Agent with a knowledge base, long instructions and tool bindings;
* a Shared Project with its harness and shared memory space;
* a PDF attachment on the first turn.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List

from tests.apis.inference_api.turn_io.recorder import FakeAws

PREFIX = "tio"
ACCOUNT = "123456789012"
REGION = "us-east-1"
MEMORY_ID = "tio_memory-AbCdEf1234"

USER_ID = "user-tio-0001"
USER_EMAIL = "pat@example.edu"
USER_NAME = "Pat Example"

CHAT_SESSION = "sess-tio-chat-000000000000001"
PROJECT_SESSION = "sess-tio-project-0000000000001"
FRESH_SESSION = "sess-tio-fresh-00000000000001"
PREPARE_SESSION = "sess-tio-prepare-000000000001"
UPLOAD_ID = "20261101T000000-tio-upload-0001"

# Cut to the 4,000-character cap on saved instructions (MAX_PERSONAL_INSTRUCTIONS_CHARS): a full one.
PERSONAL_INSTRUCTIONS = "\n".join(
    f"{i}. When I ask about planning, include owners, dates and the risks you see; "
    "prefer short tables over long prose, and say when you are unsure."
    for i in range(1, 50)
)
AGENT_INSTRUCTIONS = "\n".join(
    f"Rule {i}: answer from the registrar knowledge base first, cite the page, and "
    "escalate anything about holds or financial aid to a human advisor."
    for i in range(1, 40)
)

# A minimal one-page PDF.
PDF_BYTES = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
    b"/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
    b"4 0 obj<</Length 44>>stream\nBT /F1 12 Tf 72 720 Td (Q3 plan) Tj ET\nendstream\nendobj\n"
    b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
    b"xref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000052 00000 n \n"
    b"0000000101 00000 n \n0000000229 00000 n \n0000000322 00000 n \n"
    b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n392\n%%EOF\n"
)


def environment() -> dict:
    """The Runtime's environment for this deployment (derived names + the explicit ones)."""
    from apis.shared.config.runtime_environment import derived_environment_manifest

    env = {variable.name: variable.derive(PREFIX, ACCOUNT) for variable in derived_environment_manifest()}
    env.update(
        {
            "PROJECT_PREFIX": PREFIX,
            "AWS_ACCOUNT_ID": ACCOUNT,
            "AWS_REGION": REGION,
            "AWS_DEFAULT_REGION": REGION,
            "AGENTCORE_MEMORY_ID": MEMORY_ID,
            "SKILLS_ENABLED": "true",
            "PROJECTS_ENABLED": "true",
            "MEMORY_SPACES_ENABLED": "true",
        }
    )
    return env


@dataclass
class World:
    fake: FakeAws
    tool_ids: List[str]
    skill_ids: List[str]
    model_id: str
    agent_id: str
    project_harness_id: str


def _seed_bootstrap() -> List[str]:
    scripts = str(Path(__file__).resolve().parents[4] / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import seed_bootstrap_data as seed

    quotas, models, roles = f"{PREFIX}-user-quotas", f"{PREFIX}-managed-models", f"{PREFIX}-app-roles"
    for result in (
        seed.seed_default_quota_tier(table_name=quotas, region=REGION),
        seed.seed_default_quota_assignment(table_name=quotas, region=REGION, tier_id="default"),
        seed.seed_default_models(table_name=models, region=REGION),
        seed.seed_system_admin_role(table_name=roles, region=REGION),
        seed.seed_default_role(table_name=roles, region=REGION),
        seed.seed_default_tools(table_name=roles, region=REGION),
        seed.seed_example_skills(table_name=roles, region=REGION),
        seed.seed_skill_creator(table_name=roles, region=REGION),
    ):
        assert not result.failed, (result.category, result.details)
    return [tool["toolId"] for tool in seed.DEFAULT_TOOLS], [seed.EXAMPLE_SKILL_ID]


async def build_world(fake: FakeAws) -> World:
    from apis.shared.assistants.models import AgentBinding
    from apis.shared.assistants.service import create_assistant
    from apis.shared.auth.models import User
    from apis.shared.files.models import FileMetadata, FileStatus
    from apis.shared.files.repository import FileUploadRepository
    from apis.shared.projects.service import ProjectService
    from apis.shared.user_settings.repository import UserSettingsRepository

    tool_ids, skill_ids = _seed_bootstrap()

    from apis.shared.models.managed_models import list_managed_models

    catalog = await list_managed_models()
    default = next((m for m in catalog if getattr(m, "is_default", False)), catalog[0])
    model_id = default.model_id

    await UserSettingsRepository().update_settings(USER_ID, {"personalInstructions": PERSONAL_INSTRUCTIONS[:4000], "memoryMode": "on"})

    agent = await create_assistant(
        owner_id=USER_ID,
        owner_name=USER_NAME,
        name="Registrar",
        description="Answers registration questions from the registrar's handbook.",
        instructions=AGENT_INSTRUCTIONS,
        bindings=[AgentBinding(kind="tool", ref=t) for t in ("calculator", "fetch_url_content", "create_artifact")],
    )
    fake.vectors = [
        {
            "key": f"doc-1#chunk-{i}",
            "distance": 0.1 + i / 100,
            "metadata": {
                "assistant_id": agent.assistant_id,
                "document_id": "doc-1",
                "source": "handbook.pdf",
                "text": f"Section {i}: register in the student center before the add/drop deadline.",
            },
        }
        for i in range(5)
    ]

    user = User(email=USER_EMAIL, user_id=USER_ID, name=USER_NAME, roles=["Staff"], raw_token="tok")
    project = await ProjectService().create_project(user, "Q3 Planning", "The team's quarter plan")

    bucket = f"{PREFIX}-user-file-uploads-{ACCOUNT}"
    s3_key = f"users/{USER_ID}/{CHAT_SESSION}/{UPLOAD_ID}/q3-plan.pdf"
    fake.s3[(bucket, s3_key)] = PDF_BYTES
    await FileUploadRepository().create_file(
        FileMetadata(
            upload_id=UPLOAD_ID,
            user_id=USER_ID,
            session_id=CHAT_SESSION,
            filename="q3-plan.pdf",
            mime_type="application/pdf",
            size_bytes=len(PDF_BYTES),
            s3_key=s3_key,
            s3_bucket=bucket,
            status=FileStatus.READY,
        )
    )

    return World(
        fake=fake,
        tool_ids=tool_ids,
        skill_ids=skill_ids,
        model_id=model_id,
        agent_id=agent.assistant_id,
        project_harness_id=project.harness_agent_id,
    )
