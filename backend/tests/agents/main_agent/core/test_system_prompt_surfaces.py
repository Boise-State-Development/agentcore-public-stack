"""Tests for per-surface system-prompt composition.

The web app is the primary client and ``DEFAULT_SYSTEM_PROMPT`` is written for
it. Two properties are asserted here:

* a **web** turn receives exactly the prompt it would without surfaces —
  byte-for-byte, so neither its behaviour nor its prompt-cache prefix moves;
* a **terminal** turn receives that same prompt plus a block that overrides its
  browser-only instructions, and names the keys the TUI actually binds.

Terminal assertions check for the presence of overrides rather than the absence
of web wording, because the web wording is the shared prompt's to own.
"""

from __future__ import annotations

import pytest

from agents.main_agent.core.system_prompt_builder import (
    DEFAULT_CLIENT_SURFACE,
    DEFAULT_SYSTEM_PROMPT,
    PLATFORM_SAFETY_FLOOR,
    SURFACE_GUIDANCE,
    TERMINAL_SURFACE_GUIDANCE,
    SystemPromptBuilder,
    compose_base_prompt,
)


class TestWebIsUntouched:
    def test_web_is_the_default(self) -> None:
        assert DEFAULT_CLIENT_SURFACE == "web"

    @pytest.mark.parametrize("surface", [None, "web", "holodeck"])
    def test_web_absent_and_unknown_surfaces_get_the_default_prompt_exactly(self, surface) -> None:
        assert compose_base_prompt(surface) == DEFAULT_SYSTEM_PROMPT

    def test_the_web_builder_matches_the_surfaceless_builder(self) -> None:
        assert SystemPromptBuilder(surface="web").build(include_date=False) == SystemPromptBuilder().build(include_date=False)

    @pytest.mark.parametrize("surface", [None, "web"])
    def test_web_custom_prompts_are_unchanged(self, surface) -> None:
        assert SystemPromptBuilder.from_user_prompt("Be terse.", surface=surface).build(include_date=False) == SystemPromptBuilder.from_user_prompt(
            "Be terse."
        ).build(include_date=False)
        built = SystemPromptBuilder.from_user_prompt("Be terse.", surface=surface).build(include_date=False)
        assert "INTERFACE" not in built


class TestTerminalSurface:
    def test_it_extends_the_default_prompt_rather_than_replacing_it(self) -> None:
        """Institutional identity and safety guidance are not negotiable per client."""
        composed = compose_base_prompt("terminal")
        assert composed.startswith(DEFAULT_SYSTEM_PROMPT)
        assert composed.endswith(TERMINAL_SURFACE_GUIDANCE)

    def test_every_non_web_surface_has_a_block(self) -> None:
        for surface, guidance in SURFACE_GUIDANCE.items():
            if surface != "web":
                assert guidance.startswith("INTERFACE —")

    def test_it_says_it_overrides_the_web_instructions(self) -> None:
        """The default prompt names web controls; the block must say it wins."""
        assert "overrides the web-app instructions above" in TERMINAL_SURFACE_GUIDANCE

    @pytest.mark.parametrize(
        ("in_default", "in_terminal"),
        [
            ("Customize → Tools", "Customize → Tools"),
            ('"Preview" button', "Preview button"),
            ("KaTeX", "KaTeX"),
            ("Mermaid", "Mermaid"),
        ],
    )
    def test_every_web_only_feature_in_the_default_prompt_is_overridden(self, in_default: str, in_terminal: str) -> None:
        """If the default prompt names it, the terminal block has to address it.
        Fails on the first side when the web prompt drops the feature (then the
        override can go), and on the second when the override is missing."""
        assert in_default in DEFAULT_SYSTEM_PROMPT
        assert in_terminal in TERMINAL_SURFACE_GUIDANCE

    def test_it_says_plainly_that_the_browser_controls_do_not_exist(self) -> None:
        assert "no mouse" in TERMINAL_SURFACE_GUIDANCE
        assert "no sidebar" in TERMINAL_SURFACE_GUIDANCE

    def test_katex_and_mermaid_are_explicitly_forbidden(self) -> None:
        """The default prompt permits both, so the block has to say no."""
        assert "Do NOT emit KaTeX" in TERMINAL_SURFACE_GUIDANCE
        assert "Do NOT emit Mermaid" in TERMINAL_SURFACE_GUIDANCE

    @pytest.mark.parametrize(("key", "purpose"), [("F3", "tool"), ("F4", "conversation"), ("F2", "model")])
    def test_it_names_the_keys_the_tui_actually_binds(self, key: str, purpose: str) -> None:
        """These must track `ChatScreen.BINDINGS`; a stale key means confidently
        wrong instructions."""
        assert key in TERMINAL_SURFACE_GUIDANCE

    def test_it_says_consent_cannot_be_completed_here(self) -> None:
        assert "web app" in TERMINAL_SURFACE_GUIDANCE
        assert "Never tell the user to click Allow" in TERMINAL_SURFACE_GUIDANCE

    def test_it_warns_about_width(self) -> None:
        assert "80 columns" in TERMINAL_SURFACE_GUIDANCE

    def test_it_permits_the_markdown_that_does_render(self) -> None:
        assert "fenced code" in TERMINAL_SURFACE_GUIDANCE


class TestBuilder:
    def test_surface_selects_the_block(self) -> None:
        assert "F3" in SystemPromptBuilder(surface="terminal").build(include_date=False)
        assert "F3" not in SystemPromptBuilder(surface="web").build(include_date=False)

    def test_an_explicit_base_prompt_wins_over_the_surface(self) -> None:
        built = SystemPromptBuilder(base_prompt="just this", surface="terminal").build(include_date=False)
        assert built == "just this"

    def test_the_date_is_still_appended(self) -> None:
        assert "Current date:" in SystemPromptBuilder(surface="terminal").build(include_date=True)


class TestTerminalCustomPrompts:
    """An assistant or custom prompt still runs in a terminal, so the block is
    appended on this path too — after the user portion, so an author cannot talk
    the agent into emitting KaTeX to a terminal."""

    def test_the_safety_floor_still_comes_first(self) -> None:
        built = SystemPromptBuilder.from_user_prompt("Be terse.", surface="terminal").build(include_date=False)
        assert built.startswith(PLATFORM_SAFETY_FLOOR)

    def test_the_user_portion_is_still_wrapped(self) -> None:
        built = SystemPromptBuilder.from_user_prompt("Be terse.", surface="terminal").build(include_date=False)
        assert "<user_instructions>\nBe terse.\n</user_instructions>" in built

    def test_the_block_comes_after_the_user_instructions(self) -> None:
        built = SystemPromptBuilder.from_user_prompt("Use KaTeX for all maths.", surface="terminal").build(include_date=False)
        assert built.index("</user_instructions>") < built.index("INTERFACE")
