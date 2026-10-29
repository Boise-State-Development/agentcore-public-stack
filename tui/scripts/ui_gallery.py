"""Render every TUI surface to SVG for visual review. Dev tool, not shipped.

    uv run python scripts/ui_gallery.py out/                        # default theme
    uv run python scripts/ui_gallery.py out/ textual-light ansi-dark  # one subdir per theme

To rasterise, open the SVGs in a browser, or with Playwright's chromium from
the frontend's node_modules::

    node -e 'const {chromium}=require("playwright");(async()=>{const b=await chromium.launch();
      const p=await b.newPage();for(const f of process.argv.slice(1)){await p.goto("file://"+f);
      await (await p.$("svg")).screenshot({path:f.replace(".svg",".png")})}await b.close()})()' out/*.svg

Drives the app the way the tests do — MockTransport, no network, no keyring —
and exports a screenshot of each screen in a representative state.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tests"))

from conftest import build_app, make_config, ok_handler, sample_models, send, sse_response, text_stream  # noqa: E402

from agentcore_tui.client.agent_events import (  # noqa: E402
    Artifact,
    CitationEvent,
    ClientToolCall,
    OAuthRequired,
    QuestionOption,
    QuotaWarning,
    ToolApprovalRequired,
    ToolCallRecord,
    UserQuestion,
    UserQuestionRequired,
)
from agentcore_tui.client.auth import SessionAuth  # noqa: E402
from agentcore_tui.client.catalog import CatalogClient, Tool  # noqa: E402
from agentcore_tui.screens.conversations import ConfirmDelete, ConversationList, RenameConversation  # noqa: E402
from agentcore_tui.screens.model_picker import ModelPicker  # noqa: E402
from agentcore_tui.screens.tool_picker import ToolPicker  # noqa: E402
from agentcore_tui.widgets import Notice, ToolCall  # noqa: E402
from agentcore_tui.widgets.agent_content import ArtifactCard, Citations, InterruptNotice, QuotaNotice  # noqa: E402
from agentcore_tui.widgets.prompts import ClientToolPrompt, ToolApprovalPrompt, UserQuestionPrompt  # noqa: E402

SIZE = (110, 34)

ANSWER = """Here's a quick comparison.

## Options

1. **Haiku** — fastest, cheapest; fine for drafts.
2. **Sonnet** — the usual default.

| Model | Latency | Cost |
|---|---|---|
| Haiku | ~1s | low |
| Sonnet | ~3s | medium |

```python
def pick(model: str) -> str:
    return {"fast": "haiku"}.get(model, "sonnet")
```

Use `pick("fast")` when latency matters more than nuance.
"""


async def chat_scenes(out: Path, theme: str | None = None) -> None:
    app = build_app(lambda _r: sse_response(text_stream([ANSWER], usage={"inputTokens": 1842, "outputTokens": 236, "cacheReadInputTokens": 1500})))
    async with app.run_test(size=SIZE) as pilot:
        if theme:
            app.theme = theme
        await pilot.pause()
        (out / "01-empty.svg").write_text(app.export_screenshot())

        await send(pilot, app, "Which model should I use for quick drafts vs. careful analysis?")
        await pilot.pause()

        transcript = app.chat.transcript
        record = ToolCallRecord(tool_use_id="t1", name="web_search", arguments={"query": "claude haiku vs sonnet latency"})
        record.result = '{"results": [{"title": "Model comparison", "url": "https://example.com"}]}'
        tool = ToolCall(record)
        await transcript.mount(tool, before=list(app.chat.query("AssistantMessage"))[-1])
        tool.refresh_from_record()
        running = ToolCall(ToolCallRecord(tool_use_id="t2", name="code_interpreter", progress="Running cell 2"))
        await transcript.mount(running, before=list(app.chat.query("AssistantMessage"))[-1])
        await transcript.mount(Citations([CitationEvent(document_id="d1", file_name="pricing.pdf", text="Haiku is priced at...")]))
        await transcript.mount(
            QuotaNotice(QuotaWarning(message="You have used 80% of your monthly quota.", current_usage=16.0, quota_limit=20.0, remaining=4.0))
        )
        await transcript.mount(ArtifactCard(Artifact(artifact_id="a1", title="Latency chart", content_type="text/html", version=1, action="created")))
        await transcript.mount(
            InterruptNotice(OAuthRequired(provider_id="google", authorization_url="https://accounts.google.com/o/oauth2/auth?x=1"))
        )
        await transcript.mount(Notice("Could not reach the server.", hint="Check your network and try again.", error=True))
        await transcript.mount(
            ClientToolPrompt(
                ClientToolCall(
                    interrupt_id="i1",
                    tool_use_id="t9",
                    tool_name="run_command",
                    arguments={"command": "git status --short", "reason": "To see what changed before summarising."},
                )
            )
        )
        settled = ToolApprovalPrompt(ToolApprovalRequired(interrupt_id="i2", tool_use_id="t8", tool_name="canvas_grade"))
        await transcript.mount(settled)
        settled.settle("✓ Approved canvas_grade")
        await transcript.mount(
            UserQuestionPrompt(
                UserQuestionRequired(
                    interrupt_id="i3",
                    tool_use_id="t7",
                    questions=(
                        UserQuestion(
                            header="Scope",
                            question="How much of the codebase should this cover?",
                            options=(QuestionOption("Just the API", "Routes only"), QuestionOption("Everything")),
                        ),
                    ),
                )
            )
        )
        await pilot.pause()
        transcript.scroll_home(animate=False)
        await pilot.pause()
        (out / "02-transcript-top.svg").write_text(app.export_screenshot())
        transcript.scroll_end(animate=False)
        await pilot.pause()
        (out / "03-transcript-bottom.svg").write_text(app.export_screenshot())
        app.chat.query_one(ClientToolPrompt).focus()
        await pilot.pause()
        (out / "03b-prompt-focused.svg").write_text(app.export_screenshot())

        app.chat.composer.text = "Thanks — now summarise that as a table I can paste into Slack.\nKeep it under ten rows."
        app.chat.status.set_state("Thinking…", busy=True)
        app.chat.composer.add_class("-busy")
        await pilot.pause()
        (out / "04-composing-busy.svg").write_text(app.export_screenshot())
        app.chat.composer.remove_class("-busy")
        app.chat.status.set_state("Ready")

        app.push_screen(ModelPicker(sample_models(), current=sample_models()[0].model_id))
        await pilot.pause()
        (out / "05-model-picker.svg").write_text(app.export_screenshot())
        app.pop_screen()

        tools = [
            Tool(
                tool_id="web_search", name="Web search", description="Search the web with Tavily", category="Research", status="active", enabled=True
            ),
            Tool(
                tool_id="wikipedia", name="Wikipedia", description="Look up encyclopedia articles", category="Research", status="active", enabled=True
            ),
            Tool(
                tool_id="code_interpreter",
                name="Code interpreter",
                description="Run Python in a sandbox",
                category="Compute",
                status="active",
                enabled=False,
            ),
            Tool(
                tool_id="gdrive",
                name="Google Drive",
                description="Search your Drive files",
                category="Workspace",
                status="active",
                enabled=False,
                requires_oauth_provider="google",
            ),
            Tool(tool_id="canvas", name="Canvas", description="Courses and assignments", category="Workspace", status="unhealthy", enabled=True),
        ]
        app.push_screen(ToolPicker(tools))
        await pilot.pause()
        (out / "06-tool-picker.svg").write_text(app.export_screenshot())
        app.pop_screen()

        app.push_screen(ConfirmDelete("Latency comparison notes"))
        await pilot.pause()
        (out / "07-confirm-delete.svg").write_text(app.export_screenshot())
        app.pop_screen()

        app.push_screen(RenameConversation("Latency comparison notes"))
        await pilot.pause()
        (out / "08-rename.svg").write_text(app.export_screenshot())
        app.pop_screen()

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/sessions"):
                sessions = [
                    {
                        "sessionId": "s1",
                        "title": "Latency comparison notes",
                        "lastMessageAt": "2026-10-22T18:00:00Z",
                        "messageCount": 6,
                        "unread": True,
                    },
                    {"sessionId": "s2", "title": "Draft syllabus for CS 101", "lastMessageAt": "2026-10-21T09:00:00Z", "messageCount": 14},
                    {"sessionId": "s3", "title": "Grant budget sanity check", "lastMessageAt": "2026-10-17T09:00:00Z", "messageCount": 3},
                    {"sessionId": "s4", "title": "Untitled", "lastMessageAt": "2026-01-01T09:00:00Z", "messageCount": 0},
                ]
                return httpx.Response(200, json={"sessions": sessions, "nextToken": "more"})
            return httpx.Response(200, json={"messages": [], "nextToken": None})

        api = CatalogClient("https://example.test/api", auth=SessionAuth("s"), client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))

        async def opened(*_: Any) -> None:
            return None

        app.push_screen(ConversationList(lambda: api, on_open=opened))
        await pilot.pause()
        await pilot.pause()
        (out / "09-conversations.svg").write_text(app.export_screenshot())
        app.pop_screen()
        await api.aclose()

    unconfigured = build_app(ok_handler(), config=make_config(api_key="", base_url=""))
    async with unconfigured.run_test(size=SIZE) as pilot:
        if theme:
            unconfigured.theme = theme
        await pilot.pause()
        (out / "10-unconfigured.svg").write_text(unconfigured.export_screenshot())


def main() -> None:
    """``ui_gallery.py OUT [THEME ...]``: one subdirectory per theme, or the default."""
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "ui-gallery")
    themes = sys.argv[2:] or [""]
    for theme in themes:
        target = out / theme if theme else out
        target.mkdir(parents=True, exist_ok=True)
        asyncio.run(chat_scenes(target, theme or None))
        print(f"wrote {len(list(target.glob('*.svg')))} screenshots to {target}")


if __name__ == "__main__":
    main()
