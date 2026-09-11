import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { PulsatingLoaderComponent } from './pulsating-loader.component';

@Component({
  imports: [PulsatingLoaderComponent],
  template: `<app-pulsating-loader
    [notice]="notice"
    [status]="status"
    [statusTool]="statusTool"
    [startedAt]="startedAt"
  />`,
})
class HostComponent {
  notice: string | null = null;
  status: string | null = null;
  statusTool: string | null = null;
  startedAt: number | null = null;
}

function render(props: Partial<HostComponent> = {}) {
  const fixture = TestBed.createComponent(HostComponent);
  Object.assign(fixture.componentInstance, props);
  fixture.detectChanges();
  return fixture;
}

const textOf = (fixture: { nativeElement: HTMLElement }) =>
  (fixture.nativeElement.textContent ?? '').replace(/\s+/g, ' ').trim();

describe('PulsatingLoaderComponent', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  describe('what it says', () => {
    it('shows the live state from agent_status', () => {
      expect(textOf(render({ status: 'Thinking' }))).toContain('Thinking');
    });

    it('shows the running tool by its real name', () => {
      // The identifier is the most accurate label available while a tool runs,
      // and is the same one the tool rail and admin catalog use.
      const fixture = render({ status: 'Running', statusTool: 'list_assignments' });
      expect(textOf(fixture)).toContain('Running list_assignments');
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
      expect(textOf(render())).toBe('Thinking');
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
