"""Startup banner: a mascot crosses the viewport, then the wordmark lands.

Shown once per installed version (see :mod:`..state`) or on demand with
``--banner``. Three properties matter more than the animation itself:

* **It never blocks.** ``on_mount`` starts the animation and returns; the app
  finishes its own setup underneath. Nothing about startup waits on it.
* **It is always skippable.** Any key or click ends it immediately. A flourish
  that cannot be dismissed becomes an irritant for anyone who launches the
  client dozens of times a day.
* **It always ends.** A watchdog timer dismisses the screen even if an
  animation callback is missed, so a cosmetic feature can never wedge the app.

The art is deliberately isolated in module-level constants: the layout measures
whatever it is given, so a fork can substitute its own mascot and wordmark of
any size without touching the animation.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import ClassVar

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Center, Middle
from textual.css.scalar import ScalarOffset
from textual.screen import Screen
from textual.widgets import Static

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Art
# ---------------------------------------------------------------------------

#: PLACEHOLDER. A motion streak standing in for the bronco until the real art
#: lands. Any number of rows and columns works — the splash measures this to
#: decide how far off-screen to start and end the run.
MASCOT: tuple[str, ...] = (
    r"   ,,,",
    r" >>>===",
    r"   '''",
)

#: The "APS" monogram. Plain ASCII on purpose: box-drawing and half-block
#: glyphs are the first thing to break in a terminal with an incomplete font,
#: and this is the one frame every user sees.
WORDMARK: tuple[str, ...] = (
    r"    _    ____  ____ ",
    r"   / \  |  _ \/ ___|",
    r"  / _ \ | |_) \___ \ ",
    r" / ___ \|  __/ ___) |",
    r"/_/   \_\_|   |____/ ",
)

SUBTITLE = "A g e n t C o r e   P u b l i c   S t a c k"

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
#
# Paced for something seen once per installed version, not once per launch.
# The whole sequence runs about three seconds, which is long enough to read the
# wordmark and short enough that nobody reaches for the skip. It is skippable
# regardless.

#: Seconds for the mascot to cross the viewport.
CROSS_DURATION = 1.5

#: When the wordmark begins fading up, and how long that takes. It starts while
#: the mascot is still in flight so the mascot reads as revealing it rather than
#: as an unrelated first act.
WORDMARK_DELAY = 0.55
WORDMARK_FADE = 0.9

#: The skip hint arrives last — offering it immediately invites skipping the
#: thing we just chose to show.
HINT_DELAY = 1.4
HINT_FADE = 0.5

#: How long the finished frame is held once the mascot has left, so the
#: wordmark is actually readable rather than glimpsed.
HOLD = 1.5

#: Reduced-motion path. There is no crossing to wait for, but the text still
#: needs reading time, so this is close to the animated total.
STATIC_HOLD = 1.8

#: Extra grace before the watchdog fires. Only reached if an animation callback
#: is lost; the visible timing is driven by the animation itself.
WATCHDOG_MARGIN = 1.5


def _art_width(art: tuple[str, ...]) -> int:
    return max((len(line) for line in art), default=0)


class Splash(Screen[None]):
    """The startup banner. Dismisses itself; the caller only supplies a callback."""

    BINDINGS: ClassVar[list[BindingType]] = [
        # Bound explicitly as well as caught in `on_key` so the footer-less
        # screen still responds to the key users reach for first.
        Binding("escape", "skip", "Skip", show=False),
    ]

    def __init__(
        self,
        *,
        cross_duration: float = CROSS_DURATION,
        hold: float = HOLD,
        static_hold: float = STATIC_HOLD,
        on_finished: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(id="splash")
        self._cross_duration = cross_duration
        self._hold = hold
        self._static_hold = static_hold
        self._on_finished = on_finished
        # Guards re-entry: a keypress landing as the animation completes would
        # otherwise dismiss twice.
        self._finished = False

    def compose(self) -> ComposeResult:
        with Middle():
            yield Static("\n".join(MASCOT), id="splash-mascot")
            with Center():
                yield Static("\n".join(WORDMARK), id="splash-wordmark")
            with Center():
                yield Static(SUBTITLE, id="splash-subtitle")
            with Center():
                yield Static("press any key", id="splash-hint")

    # -- lifecycle -----------------------------------------------------------

    def on_mount(self) -> None:
        mascot = self.query_one("#splash-mascot", Static)
        wordmark = self.query_one("#splash-wordmark", Static)
        subtitle = self.query_one("#splash-subtitle", Static)
        hint = self.query_one("#splash-hint", Static)

        # `animation_level` is Textual's own reduced-motion switch, driven by
        # TEXTUAL_ANIMATIONS. When motion is off, show the finished frame and
        # hold it rather than animating nothing for the same duration.
        if self.app.animation_level != "full":
            # The mascot is laid out flush left so the crossing animation can
            # measure from the edge; with no crossing it has to be centred by
            # hand or it sits in the corner looking like a mistake.
            mascot.styles.offset = (max(0, (self.size.width - _art_width(MASCOT)) // 2), 0)
            self.set_timer(self._static_hold, self._finish)
            return

        for widget in (wordmark, subtitle, hint):
            widget.styles.opacity = 0.0

        width = self.size.width or 80
        travel = _art_width(MASCOT) + 4
        mascot.styles.offset = (-travel, 0)
        # Must be a ScalarOffset, not a plain tuple: `Styles.__textual_animation__`
        # only builds an animation for Scalar/ScalarOffset values, and anything
        # else falls through to the generic path and raises AnimationError.
        #
        # The ignore is an upstream typing gap, not a workaround. `animate` is
        # annotated `str | float | Animatable`, which does not include the
        # Scalar/ScalarOffset branch that `__textual_animation__` exists to
        # handle. Passing an `Offset` would satisfy mypy and then fail at
        # runtime: it is Animatable, so it takes the generic path, which blends
        # against the current ScalarOffset value and has no `blend` to call.
        #
        # `linear` rather than an ease: the run starts and ends off-screen, so
        # an ease-in-out spends its slow phases invisible and crosses the
        # visible middle at maximum speed — the opposite of what is wanted. A
        # constant rate also reads more like a gallop than an accelerating slide.
        mascot.styles.animate(
            "offset",
            value=ScalarOffset.from_offset((width + travel, 0)),  # type: ignore[arg-type]
            duration=self._cross_duration,
            easing="linear",
            level="full",
            on_complete=self._hold_then_finish,
        )

        for widget in (wordmark, subtitle):
            widget.styles.animate(
                "opacity",
                value=1.0,
                duration=WORDMARK_FADE,
                delay=WORDMARK_DELAY,
                level="full",
            )

        hint.styles.animate("opacity", value=1.0, duration=HINT_FADE, delay=HINT_DELAY, level="full")

        # Belt and braces: if `on_complete` never arrives the banner must not
        # become a modal screen the user cannot escape.
        self.set_timer(self._cross_duration + self._hold + WATCHDOG_MARGIN, self._finish)

    def _hold_then_finish(self) -> None:
        """Let the wordmark sit for a beat once the mascot has left."""
        if not self._finished:
            self.set_timer(self._hold, self._finish)

    def _finish(self) -> None:
        """Dismiss once, and only while this screen is still the active one."""
        if self._finished:
            return
        self._finished = True
        if self._on_finished is not None:
            self._on_finished()
        # Guard against dismissing after the app has already moved on (quit
        # during the animation, or a watchdog firing late).
        if self.is_running and self.app.screen is self:
            self.dismiss(None)

    # -- skipping ------------------------------------------------------------

    def action_skip(self) -> None:
        self._finish()

    def on_key(self, event: events.Key) -> None:
        # Consume the keypress: it was "skip the banner", not input for the
        # composer that is about to receive focus.
        event.stop()
        event.prevent_default()
        self._finish()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        event.stop()
        self._finish()
