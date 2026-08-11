"""Tests for per-surface system-prompt composition.

The defect these guard against shipped for months: the shared prompt told *every*
client to click "the gear icon next to the message input" and offered KaTeX and
Mermaid, so terminal users were directed to a control that does not exist and
handed two renderers a terminal draws as literal noise.

Two properties matter more than the wording, and both are asserted structurally
rather than by quoting whole paragraphs, so editing the prose does not break the
suite:

* the **web** surface keeps every instruction it had before the split;
* the **terminal** surface contains no browser control, no browser renderer, and
  does name the keys the TUI actually binds.
"""

from __future__ import annotations

import pytest

from agents.main_agent.core.system_prompt_builder import (
    DEFAULT_CLIENT_SURFACE,
    DEFAULT_SYSTEM_PROMPT,
    PLATFORM_SAFETY_FLOOR,
    SHARED_SYSTEM_PROMPT,
    SURFACE_GUIDANCE,
    TERMINAL_SURFACE_GUIDANCE,
    WEB_SURFACE_GUIDANCE,
    SystemPromptBuilder,
    compose_base_prompt,
)

#: Anything that only exists in a browser. A terminal prompt containing one of
#: these is telling the user about a control or renderer they do not have.
BROWSER_ONLY_MARKERS = ("gear icon", "settings panel", "KaTeX", "Mermaid", "&#36;", "click ")


class TestComposition:
    def test_web_is_the_default(self) -> None:
        assert DEFAULT_CLIENT_SURFACE == "web"
        assert compose_base_prompt("web") == DEFAULT_SYSTEM_PROMPT

    def test_an_absent_surface_means_web(self) -> None:
        """An older client that predates the field is by definition the browser."""
        assert compose_base_prompt(None) == compose_base_prompt("web")

    def test_an_unknown_surface_degrades_to_web(self) -> None:
        """A client naming a surface this build has not heard of should get a
        slightly wrong interface section, not a failed turn."""
        assert compose_base_prompt("holodeck") == compose_base_prompt("web")

    @pytest.mark.parametrize("surface", sorted(SURFACE_GUIDANCE))
    def test_every_surface_includes_the_shared_prompt(self, surface: str) -> None:
        """The institutional identity, academic-integrity and safety guidance are
        not negotiable per client."""
        composed = compose_base_prompt(surface)
        assert SHARED_SYSTEM_PROMPT in composed
        assert "boisestate.ai" in composed
        assert "Academic Integrity" in composed

    @pytest.mark.parametrize("surface", sorted(SURFACE_GUIDANCE))
    def test_every_surface_gets_exactly_one_interface_section(self, surface: str) -> None:
        """Two would contradict each other; none leaves the agent guessing."""
        assert compose_base_prompt(surface).count("INTERFACE —") == 1

    @pytest.mark.parametrize("surface", sorted(SURFACE_GUIDANCE))
    def test_every_surface_answers_the_same_questions(self, surface: str) -> None:
        """A new surface must not silently omit an answer.

        Tools have to be togglable somehow, and the agent has to know what it may
        not render, or it falls back to guessing.
        """
        guidance = SURFACE_GUIDANCE[surface]
        assert "tool" in guidance.lower()
        assert "render" in guidance.lower()


class TestSharedPromptIsSurfaceNeutral:
    @pytest.mark.parametrize("marker", BROWSER_ONLY_MARKERS)
    def test_the_shared_prompt_names_no_browser_control_or_renderer(self, marker: str) -> None:
        """This is the regression. These lines used to live in the shared text,
        which is how they reached the terminal."""
        assert marker not in SHARED_SYSTEM_PROMPT

    def test_the_shared_prompt_still_explains_missing_tools(self) -> None:
        """Only the *where to toggle* was surface-specific; the reasoning was not,
        and moving it wholesale would have lost it for both clients."""
        assert "HANDLING MISSING TOOLS" in SHARED_SYSTEM_PROMPT
        assert "Spreadsheet Analysis" in SHARED_SYSTEM_PROMPT

    def test_the_shared_prompt_defers_to_the_interface_section(self) -> None:
        """So the agent looks somewhere real instead of inventing a control."""
        assert "INTERFACE section" in SHARED_SYSTEM_PROMPT


class TestWebSurfaceIsUnchanged:
    """The split must not be a behaviour change for the browser."""

    @pytest.mark.parametrize(
        "instruction",
        [
            "gear icon next to the message input",
            "KaTeX",
            "HTML entity &#36;",
            "Mermaid",
        ],
    )
    def test_web_keeps_every_instruction_it_had(self, instruction: str) -> None:
        assert instruction in compose_base_prompt("web")

    def test_web_keeps_the_worked_example(self) -> None:
        """The example names a browser control, so it moved into the web block
        rather than being deleted."""
        web = compose_base_prompt("web")
        assert "Open the settings panel" in web
        assert "=SUM(NET_AMOUNT)" in web


class TestTerminalSurface:
    @pytest.mark.parametrize(
        "recommendation",
        [
            "you may use KaTeX",
            "you may use Mermaid",
            "gear icon",
            "Open the settings panel",
            "HTML entity &#36;",
        ],
    )
    def test_no_browser_affordance_is_recommended(self, recommendation: str) -> None:
        """Absence of the *recommendation*, not of the word.

        The block deliberately names browser affordances in order to rule them
        out — "there is no settings panel" and "never tell the user to click
        Allow" are the useful sentences, and a test that banned the words would
        have forced them out in favour of silence, which the model fills in with
        its priors.
        """
        assert recommendation not in TERMINAL_SURFACE_GUIDANCE

    def test_it_says_plainly_that_the_browser_controls_do_not_exist(self) -> None:
        assert "no settings panel" in TERMINAL_SURFACE_GUIDANCE
        assert "no mouse" in TERMINAL_SURFACE_GUIDANCE

    def test_katex_and_mermaid_are_explicitly_forbidden(self) -> None:
        """Silence is not enough. The model has strong priors toward emitting
        both, so the block has to say no rather than merely not say yes."""
        assert "Do NOT emit KaTeX" in TERMINAL_SURFACE_GUIDANCE
        assert "Do NOT emit Mermaid" in TERMINAL_SURFACE_GUIDANCE

    @pytest.mark.parametrize(("key", "purpose"), [("F3", "tool"), ("F4", "conversation"), ("F2", "model")])
    def test_it_names_the_keys_the_tui_actually_binds(self, key: str, purpose: str) -> None:
        """These must track `ChatScreen.BINDINGS`. If a binding moves and this
        does not, the agent starts giving confidently wrong instructions — which
        is the exact failure the split exists to end."""
        assert key in TERMINAL_SURFACE_GUIDANCE

    def test_it_says_consent_cannot_be_completed_here(self) -> None:
        """A terminal cannot present an OAuth screen, so 'click Allow' is wrong
        advice; the turn is genuinely paused until the web app."""
        assert "web app" in TERMINAL_SURFACE_GUIDANCE
        assert "Never tell the user to click Allow" in TERMINAL_SURFACE_GUIDANCE

    def test_it_warns_about_width(self) -> None:
        assert "80 columns" in TERMINAL_SURFACE_GUIDANCE

    def test_it_permits_the_markdown_that_does_render(self) -> None:
        """Textual renders headings, lists, tables and fenced code. Forbidding
        markdown wholesale would throw away the useful part."""
        assert "fenced code" in TERMINAL_SURFACE_GUIDANCE


class TestBuilder:
    def test_surface_selects_the_interface_section(self) -> None:
        assert "F3" in SystemPromptBuilder(surface="terminal").build(include_date=False)
        assert "gear icon" in SystemPromptBuilder(surface="web").build(include_date=False)

    def test_an_explicit_base_prompt_wins_over_the_surface(self) -> None:
        """A caller supplying its own text has already decided what to say."""
        built = SystemPromptBuilder(base_prompt="just this", surface="terminal").build(include_date=False)
        assert built == "just this"

    def test_the_date_is_still_appended(self) -> None:
        assert "Current date:" in SystemPromptBuilder(surface="terminal").build(include_date=True)


class TestCustomPromptsStillGetTheInterfaceSection:
    """An assistant or a custom prompt still runs in a terminal.

    Telling that user to click a gear icon is wrong regardless of who wrote the
    instructions, so the interface section is appended on this path too.
    """

    def test_the_safety_floor_still_comes_first(self) -> None:
        built = SystemPromptBuilder.from_user_prompt("Be terse.", surface="terminal").build(include_date=False)
        assert built.startswith(PLATFORM_SAFETY_FLOOR)

    def test_the_user_portion_is_still_wrapped(self) -> None:
        built = SystemPromptBuilder.from_user_prompt("Be terse.", surface="terminal").build(include_date=False)
        assert "<user_instructions>\nBe terse.\n</user_instructions>" in built

    def test_the_terminal_section_is_appended(self) -> None:
        built = SystemPromptBuilder.from_user_prompt("Be terse.", surface="terminal").build(include_date=False)
        assert "F3" in built
        assert "gear icon" not in built

    def test_the_interface_section_comes_after_the_user_instructions(self) -> None:
        """What the client can physically render is a fact about the world, not a
        preference — an assistant author must not be able to talk the agent into
        emitting KaTeX to a terminal by putting instructions later in the prompt.
        """
        built = SystemPromptBuilder.from_user_prompt("Use KaTeX for all maths.", surface="terminal").build(include_date=False)
        assert built.index("</user_instructions>") < built.index("INTERFACE")

    def test_web_custom_prompts_get_the_web_section(self) -> None:
        built = SystemPromptBuilder.from_user_prompt("Be terse.", surface="web").build(include_date=False)
        assert WEB_SURFACE_GUIDANCE in built

    def test_an_absent_surface_still_gets_one(self) -> None:
        built = SystemPromptBuilder.from_user_prompt("Be terse.").build(include_date=False)
        assert "INTERFACE" in built
