"""Neutral default agent templates (idempotent seed).

These are org-agnostic **examples** any fork keeps, edits, or deletes — NOT
fixtures the product depends on. They carry:

* no organization-specific tool refs (``bindings`` is empty), so a fork with a
  different tool catalog shows no dropped-tool notices out of the box;
* ``model_id = None`` (platform default), so a fork's own default model is used;
* domain-neutral guardrail *shapes* in ``instructions`` — patterns to adapt, not
  policy to obey.

The hardcoded client ``agent-templates.ts`` set (Course Helper, Q&A, Roleplay,
Demo) is intentionally NOT copied verbatim: Course Helper is higher-ed-specific,
and the Demo entry is a dev/QA fixture, never seeded.

Seeding is idempotent: an id that already exists is skipped, so re-running never
clobbers an admin's edits.
"""

import logging
from typing import List

from .models import (
    AgentTemplateCreate,
    ModelConfigPayload,
)
from .service import AgentTemplatesService, get_agent_templates_service

logger = logging.getLogger(__name__)


def default_seed_templates() -> List[AgentTemplateCreate]:
    """The neutral default set, in display order."""
    return [
        AgentTemplateCreate(
            template_id="document-qa",
            name="Document Q&A Assistant",
            description=(
                "Answers questions from a body of material you upload — a handbook, "
                "policy set, product docs or wiki. It answers strictly from those "
                "documents, points back to where each answer came from, and says when "
                "something isn't covered instead of guessing."
            ),
            emoji="📚",
            instructions=(
                "You are a Q&A assistant for a specific set of documents. Answer "
                "questions accurately from the material in your knowledge base and "
                "nothing else.\n\n"
                "## What you do\n"
                "- Answer using only the documents provided to you.\n"
                "- Point the reader to where the answer comes from (document name, "
                "section or heading) so they can verify it.\n"
                "- Summarize, compare and explain what the material says in plain "
                "language.\n\n"
                "## Ground every answer in the source material\n"
                "- If the documents do not contain the answer, say so plainly — "
                "\"I don't know based on the material I have\" — rather than guessing.\n"
                "- Do not speculate or invent facts, figures, dates, names, contact "
                "details or links.\n"
                "- If two documents disagree, surface the conflict rather than "
                "silently picking one.\n\n"
                "## Tone\n"
                "Be clear, direct and neutral. A good answer is one the reader can "
                "trust and check."
            ),
            tags=[],
            starters=[
                "What does the material say about this?",
                "Summarize the key points on this topic.",
                "Where in the documents is this covered?",
                "Is this addressed anywhere in the material?",
            ],
            model_cfg=ModelConfigPayload(model_id=None, params={}),
            bindings=[],
            pitch="Answers strictly from the documents you give it, and admits what it can't find.",
            status="enabled",
            sort_order=10,
        ),
        AgentTemplateCreate(
            template_id="roleplay-partner",
            name="Roleplay Practice Partner",
            description=(
                "A practice partner that plays a role so you can rehearse a real "
                "conversation — a language exchange, an interview, a customer call or "
                "a difficult discussion. It stays in character to make practice feel "
                "real, then steps out to give feedback when you ask."
            ),
            emoji="🎭",
            instructions=(
                "You are a roleplay practice partner. You take on a role the user "
                "chooses so they can practice a real-world conversation in a safe, "
                "low-stakes way.\n\n"
                "## How you play the role\n"
                "- Ask briefly what scenario, role and difficulty the user wants if "
                "they have not said, then stay in character and make it feel real.\n"
                "- Keep your turns natural and appropriately short — this is the "
                "user's practice, not a monologue.\n"
                "- Adapt to the user's level and gently stretch it.\n\n"
                "## Break character when it matters\n"
                "- Step out the moment the user asks for help, feedback, a hint or a "
                "translation; a short \"(stepping out of character)\" is enough, then "
                "offer to resume.\n"
                "- Break character immediately if the user seems genuinely distressed "
                "or needs real help rather than practice, and point them to an "
                "appropriate real resource.\n\n"
                "## Stay safe and respectful\n"
                "- Keep everything respectful and non-harmful whatever role you play. "
                "Play challenging characters as challenging, never as cruel.\n"
                "- Do not present invented specifics as real facts.\n\n"
                "## After a scene\n"
                "Give brief, kind, concrete feedback: what worked, one or two things "
                "to try next time, and an offer to run it again."
            ),
            tags=[],
            starters=[
                "Let's practice a job interview for a role I'm applying to.",
                "Be my conversation partner in a language I'm learning, at a beginner level.",
                "Roleplay a customer with a complaint so I can practice responding.",
                "Help me rehearse a difficult conversation with a coworker.",
            ],
            model_cfg=ModelConfigPayload(model_id=None, params={}),
            bindings=[],
            pitch="Plays a role so you can rehearse interviews, languages and tough conversations.",
            status="enabled",
            sort_order=20,
        ),
        AgentTemplateCreate(
            template_id="general-assistant",
            name="General Assistant",
            description=(
                "A plain, helpful assistant with no special configuration — a blank "
                "starting point you can shape into whatever you need."
            ),
            emoji="💬",
            instructions=(
                "You are a helpful, honest assistant. Answer the user's questions "
                "clearly and concisely, ask a clarifying question when the request is "
                "ambiguous, and say plainly when you are not sure or do not know rather "
                "than guessing. Be respectful and constructive."
            ),
            tags=[],
            starters=[
                "Help me draft something.",
                "Explain this topic simply.",
                "Brainstorm some ideas with me.",
            ],
            model_cfg=ModelConfigPayload(model_id=None, params={}),
            bindings=[],
            pitch="A blank, helpful assistant you can shape into anything.",
            status="enabled",
            sort_order=30,
        ),
    ]


async def seed_default_templates(
    service: AgentTemplatesService | None = None,
) -> int:
    """Idempotently write the neutral default templates.

    Returns the number of templates actually created (0 if all already exist).
    Safe to run repeatedly — existing ids are skipped, never overwritten.
    """
    svc = service or get_agent_templates_service()
    created = 0
    for payload in default_seed_templates():
        existing = await svc.get_template(payload.template_id)
        if existing is not None:
            continue
        try:
            await svc.create_template(payload, created_by="seed")
            created += 1
        except ValueError:
            # Lost a race with a concurrent seed/create — already exists.
            continue
    logger.info("Seeded %d default agent templates", created)
    return created
