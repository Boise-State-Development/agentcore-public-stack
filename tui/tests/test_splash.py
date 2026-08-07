"""Startup banner tests.

The banner is cosmetic, so the properties worth testing are the ones that stop
it becoming an obstacle: it is gated correctly, it is always skippable, it never
traps focus, and it always ends.

Most tests set ``animation_level = "none"`` — Textual's own reduced-motion
switch — so they exercise the static path and add no wall-clock time. One test
deliberately runs the animated path to prove the ``styles.animate`` calls accept
the values we pass.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agentcore_tui import __version__, state
from agentcore_tui.app import ChatApp
from agentcore_tui.config import Config
from agentcore_tui.screens import Splash
from agentcore_tui.screens.splash import (
    CROSS_DURATION,
    HINT_DELAY,
    HINT_FADE,
    HOLD,
    STATIC_HOLD,
    SUBTITLE,
    WORDMARK_DELAY,
    WORDMARK_FADE,
)
from agentcore_tui.widgets import Composer

from .conftest import build_app, make_config, ok_handler, rendered_text


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point state at a temp file so tests never read or write the real one."""
    target = tmp_path / "state.json"
    monkeypatch.setattr(state, "state_path", lambda: target)
    return target


def banner_config(*, banner: bool = True, force: bool = False) -> Config:
    return make_config(banner=banner, force_banner=force)


def build_banner_app(*, banner: bool = True, force: bool = False, animate: bool = False) -> ChatApp:
    app = build_app(ok_handler(), config=banner_config(banner=banner, force=force))
    if not animate:
        app.animation_level = "none"
    return app


class TestGating:
    async def test_shown_on_first_run(self, isolated_state: Path) -> None:
        app = build_banner_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, Splash)

    async def test_records_the_version_it_showed(self, isolated_state: Path) -> None:
        app = build_banner_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
        assert state.banner_shown_version(isolated_state) == __version__

    async def test_not_shown_again_for_the_same_version(self, isolated_state: Path) -> None:
        state.record_banner_shown(__version__, isolated_state)
        app = build_banner_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not isinstance(app.screen, Splash)

    async def test_shown_again_after_a_version_change(self, isolated_state: Path) -> None:
        state.record_banner_shown("0.0.1-something-older", isolated_state)
        app = build_banner_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, Splash)

    async def test_suppressed_when_disabled(self, isolated_state: Path) -> None:
        app = build_banner_app(banner=False)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert not isinstance(app.screen, Splash)
        # A suppressed banner must not claim to have been shown, or enabling it
        # later would silently do nothing until the next release.
        assert state.banner_shown_version(isolated_state) is None

    async def test_force_replays_an_already_seen_banner(self, isolated_state: Path) -> None:
        """This is what `--banner` buys: seeing it on demand."""
        state.record_banner_shown(__version__, isolated_state)
        app = build_banner_app(force=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, Splash)

    async def test_shown_even_when_configuration_is_incomplete(self, isolated_state: Path) -> None:
        """The setup-help path is still a successful launch."""
        app = build_app(ok_handler(), config=Config(base_url="", api_key=None, banner=True))
        app.animation_level = "none"
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, Splash)

    async def test_unwritable_state_does_not_break_startup(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A read-only home means the banner replays, not that the app fails."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        monkeypatch.setattr(state, "state_path", lambda: blocker / "state.json")

        app = build_banner_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, Splash)
            await pilot.press("space")
            await pilot.pause()
            assert not isinstance(app.screen, Splash)


class TestSkipping:
    async def test_any_key_dismisses_it(self, isolated_state: Path) -> None:
        app = build_banner_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, Splash)
            await pilot.press("j")
            await pilot.pause()
            assert not isinstance(app.screen, Splash)

    async def test_escape_dismisses_it(self, isolated_state: Path) -> None:
        app = build_banner_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, Splash)

    async def test_the_skip_keypress_is_not_typed_into_the_composer(self, isolated_state: Path) -> None:
        """Dismissing must not leak a stray character into the prompt."""
        app = build_banner_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            assert app.chat.composer.text == ""

    async def test_composer_regains_focus_afterwards(self, isolated_state: Path) -> None:
        app = build_banner_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            assert isinstance(app.focused, Composer)

    async def test_typing_works_immediately_after_dismissal(self, isolated_state: Path) -> None:
        app = build_banner_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            await pilot.press("h", "i")
            await pilot.pause()
            assert app.chat.composer.text == "hi"

    async def test_repeated_keypresses_dismiss_only_once(self, isolated_state: Path) -> None:
        """A second `dismiss` on a popped screen would raise."""
        app = build_banner_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space", "space", "space")
            await pilot.pause()
            assert not isinstance(app.screen, Splash)


class TestRendering:
    async def test_wordmark_and_subtitle_are_on_screen(self, isolated_state: Path) -> None:
        app = build_banner_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            frame = rendered_text(app)
            assert SUBTITLE in frame
            # The monogram's last row is distinctive enough to prove the art
            # rendered rather than being clipped away.
            assert "|____/" in frame

    async def test_dismissing_reveals_the_ready_chat_screen(self, isolated_state: Path) -> None:
        app = build_banner_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            frame = rendered_text(app)
            assert SUBTITLE not in frame
            assert "Ready" in frame

    async def test_mascot_is_centred_when_motion_is_disabled(self, isolated_state: Path) -> None:
        """With no crossing animation the mascot must be placed deliberately,
        not left at the flush-left position the animation starts from."""
        app = build_banner_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            mascot = app.screen.query_one("#splash-mascot")
            centre = mascot.region.x + mascot.region.width // 2
            assert abs(centre - 40) <= 1

    async def test_wordmark_and_subtitle_are_horizontally_centred(self, isolated_state: Path) -> None:
        app = build_banner_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            for selector in ("#splash-wordmark", "#splash-subtitle", "#splash-hint"):
                region = app.screen.query_one(selector).region
                centre = region.x + region.width // 2
                assert abs(centre - 40) <= 1, f"{selector} centred at {centre}"

    async def test_animated_path_runs_without_error(self, isolated_state: Path) -> None:
        """Exercises `styles.animate` for real: a bad value type raises here and
        nowhere else, because every other test runs with motion disabled."""
        app = build_banner_app(animate=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, Splash)
            assert app.animation_level == "full"
            mascot = app.screen.query_one("#splash-mascot")
            # Mid-flight the mascot sits at a horizontal offset and no vertical one.
            assert mascot.styles.offset is not None
            await pilot.press("space")
            await pilot.pause()
            assert not isinstance(app.screen, Splash)

    async def test_it_ends_on_its_own_without_input(self, isolated_state: Path) -> None:
        """Nobody should have to press a key to get to the prompt."""
        app = build_banner_app()
        async with app.run_test() as pilot:
            await pilot.pause()
            assert isinstance(app.screen, Splash)
            # Derived from the constant rather than hardcoded, so retiming the
            # banner cannot silently turn this into a test of nothing.
            await pilot.pause(STATIC_HOLD + 0.4)
            assert not isinstance(app.screen, Splash)

    async def test_hint_and_wordmark_start_hidden_when_animating(self, isolated_state: Path) -> None:
        """The staged fade is the point: everything arriving at once would make
        the slower pacing read as a stall rather than a reveal."""
        app = build_banner_app(animate=True)
        async with app.run_test() as pilot:
            await pilot.pause()
            screen = app.screen
            assert screen.query_one("#splash-wordmark").styles.opacity < 1.0
            assert screen.query_one("#splash-hint").styles.opacity < 1.0
            await pilot.press("space")
            await pilot.pause()

    def test_the_sequence_stays_within_a_few_seconds(self) -> None:
        """A guard on intent, not behaviour: this is shown once per version, so
        it can be leisurely — but it is still startup, not a title screen."""
        assert CROSS_DURATION + HOLD <= 4.0
        assert STATIC_HOLD <= 4.0
        # The hint must appear before the banner ends, or it is decoration.
        assert HINT_DELAY + HINT_FADE < CROSS_DURATION + HOLD
        # The wordmark must finish fading before the mascot leaves.
        assert WORDMARK_DELAY + WORDMARK_FADE <= CROSS_DURATION
