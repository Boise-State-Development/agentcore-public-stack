import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { PulsatingLoaderComponent } from './pulsating-loader.component';

interface LoaderInputs {
  notice: string | null;
  status: string | null;
  statusTool: string | null;
  startedAt: number | null;
}

/**
 * Driven through `setInput` on the component itself rather than a host
 * wrapper: mutating host fields after the first change-detection pass trips
 * NG0100 in dev mode, which is a harness artifact rather than anything the
 * component does wrong.
 */
function render(
  props: Partial<LoaderInputs> = {},
): ComponentFixture<PulsatingLoaderComponent> {
  const fixture = TestBed.createComponent(PulsatingLoaderComponent);
  setInputs(fixture, { notice: null, status: null, statusTool: null, startedAt: null, ...props });
  return fixture;
}

function setInputs(
  fixture: ComponentFixture<PulsatingLoaderComponent>,
  props: Partial<LoaderInputs>,
) {
  for (const [key, value] of Object.entries(props)) {
    fixture.componentRef.setInput(key, value);
  }
  fixture.detectChanges();
}

const textOf = (fixture: { nativeElement: HTMLElement }) =>
  (fixture.nativeElement.textContent ?? '').replace(/\s+/g, ' ').trim();

/** Just the state phrase, without the bullet separators or the timer. */
const stateOf = (fixture: { nativeElement: HTMLElement }) =>
  (fixture.nativeElement.querySelector('.state')?.textContent ?? '')
    .replace(/\s+/g, ' ')
    .trim();

describe('PulsatingLoaderComponent', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  describe('what it says', () => {
    it('shows the live state from agent_status', () => {
      expect(stateOf(render({ status: 'Thinking' }))).toBe('Thinking\u2026');
    });

    it('shows the running tool by its real name', () => {
      // The identifier is the most accurate label available while a tool runs,
      // and is the same one the tool rail and admin catalog use.
      const fixture = render({ status: 'Running', statusTool: 'list_assignments' });
      expect(stateOf(fixture)).toBe('Running list_assignments\u2026');
    });

    it('renders the tool name as an identifier, not prose', () => {
      const fixture = render({ status: 'Running', statusTool: 'list_assignments' });
      const mono = fixture.nativeElement.querySelector('.font-mono');
      expect(mono?.textContent?.trim()).toBe('list_assignments');
    });

    it('invents nothing when there is no state yet', () => {
      // Regression guard: this component used to cycle twenty fabricated
      // phrases ("Pondering", "Cross-referencing") that looked identical
      // whether the model was generating, waiting on a tool, or hung.
      //
      // The fallback is "Thinking", not a vaguer hedge: on a cold start the
      // gap before the first agent_status can run several seconds, and that
      // gap is the only thing the user sees.
      expect(stateOf(render())).toBe('Thinking\u2026');
    });

    it('lets a notice outrank the state', () => {
      // A retry in progress is the more important truth.
      const fixture = render({
        notice: 'The model is busy. Retrying…',
        status: 'Thinking',
      });
      const text = textOf(fixture);
      expect(text).toContain('The model is busy. Retrying');
      expect(text).not.toContain('Thinking');
    });

    it('does not double the punctuation a notice already carries', () => {
      // Every notice string ends in its own punctuation.
      expect(stateOf(render({ notice: 'Still working\u2026' }))).toBe('Still working\u2026');
      expect(stateOf(render({ notice: 'The model is busy. Retrying \u2014 attempt 2.' }))).toBe(
        'The model is busy. Retrying \u2014 attempt 2.',
      );
    });
  });

  describe('layout', () => {
    it('orders the line pulse, timer, state', () => {
      const fixture = render({ status: 'Thinking', startedAt: Date.now() });
      const row = fixture.nativeElement.querySelector('[role="status"]')!;
      const kinds = [...row.children].map(el =>
        el.classList.contains('pulse-dot')
          ? 'dot'
          : el.classList.contains('sep')
            ? 'sep'
            : el.classList.contains('state')
              ? 'state'
              : 'timer',
      );
      expect(kinds).toEqual(['dot', 'sep', 'timer', 'sep', 'state']);
    });

    it('drops the second bullet when there is no timer', () => {
      // A dangling separator next to nothing reads as a rendering bug.
      const fixture = render({ status: 'Thinking' });
      expect(fixture.nativeElement.querySelectorAll('.sep').length).toBe(1);
    });

    it('shimmers the state on the healthy path', () => {
      const state = render({ status: 'Thinking' }).nativeElement.querySelector('.state');
      expect(state?.classList.contains('shimmer')).toBe(true);
    });

    it('does not shimmer a notice', () => {
      // A warning that shimmers reads as decoration rather than a warning.
      const state = render({ notice: 'Still working…' }).nativeElement.querySelector('.state');
      expect(state?.classList.contains('shimmer')).toBe(false);
      expect(state?.classList.contains('is-notice')).toBe(true);
    });

    it('rebuilds the state node when the state changes, so it can animate', () => {
      // `@for ... track` over the visible text is what makes the enter
      // keyframe run; a plain interpolation would mutate text in place and
      // never animate.
      const fixture = render({ status: 'Thinking', startedAt: Date.now() });
      const first = fixture.nativeElement.querySelector('.state');

      setInputs(fixture, { status: 'Running', statusTool: 'browse_web' });

      expect(fixture.nativeElement.querySelector('.state')).not.toBe(first);
    });

    it('keeps the same state node while only the timer ticks', () => {
      // The state must not re-animate once a second.
      const fixture = render({ status: 'Thinking', startedAt: Date.now() });
      const first = fixture.nativeElement.querySelector('.state');

      vi.advanceTimersByTime(3000);
      fixture.detectChanges();

      expect(fixture.nativeElement.querySelector('.state')).toBe(first);
    });
  });

  describe('elapsed timer', () => {
    it('counts up in seconds', () => {
      const fixture = render({ status: 'Thinking', startedAt: Date.now() });
      expect(textOf(fixture)).toContain('0s');

      vi.advanceTimersByTime(3000);
      fixture.detectChanges();
      expect(textOf(fixture)).toContain('3s');
    });

    it('switches to minutes past sixty seconds', () => {
      const fixture = render({ status: 'Thinking', startedAt: Date.now() });

      vi.advanceTimersByTime(64_000);
      fixture.detectChanges();
      expect(textOf(fixture)).toContain('1m 4s');
    });

    it('is hidden when no start time is known', () => {
      // A zero that never moves is worse than no timer.
      expect(textOf(render({ status: 'Thinking' }))).not.toContain('0s');
    });

    it('never counts backwards from a clock skew', () => {
      const fixture = render({ status: 'Thinking', startedAt: Date.now() + 5000 });
      expect(textOf(fixture)).toContain('0s');
    });

    it('stops ticking when destroyed', () => {
      const fixture = render({ status: 'Thinking', startedAt: Date.now() });
      const clearSpy = vi.spyOn(globalThis, 'clearInterval');

      fixture.destroy();

      expect(clearSpy).toHaveBeenCalled();
    });
  });

  describe('the dot', () => {
    it('is a plain pulse on the healthy path', () => {
      const dot = render({ status: 'Thinking' }).nativeElement.querySelector('.pulse-dot');
      expect(dot).not.toBeNull();
      expect(dot?.classList.contains('is-notice')).toBe(false);
    });

    it('changes colour for a notice', () => {
      // The dot is the part a user tracks peripherally; changing only the text
      // leaves the indicator looking routine during an outage.
      const dot = render({ notice: 'Still working…' }).nativeElement.querySelector('.pulse-dot');
      expect(dot?.classList.contains('is-notice')).toBe(true);
    });
  });

  describe('accessibility', () => {
    it('announces the state politely', () => {
      const status = render({ notice: 'Still working…' }).nativeElement.querySelector(
        '[role="status"]',
      );
      expect(status?.getAttribute('aria-live')).toBe('polite');
      expect(status?.getAttribute('aria-label')).toContain('Still working');
    });

    it('includes the tool name in the accessible name', () => {
      const status = render({
        status: 'Running',
        statusTool: 'list_assignments',
      }).nativeElement.querySelector('[role="status"]');
      expect(status?.getAttribute('aria-label')).toBe('Running list_assignments');
    });

    it('keeps the ticking timer out of the announcement', () => {
      // A per-second re-announcement would be noise for a screen reader.
      const fixture = render({ status: 'Thinking', startedAt: Date.now() });
      const timer = fixture.nativeElement.querySelector('.tabular-nums');
      expect(timer?.getAttribute('aria-hidden')).toBe('true');
    });
  });
});
