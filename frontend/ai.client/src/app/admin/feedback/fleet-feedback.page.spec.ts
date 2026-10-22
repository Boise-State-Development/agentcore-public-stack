import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { of } from 'rxjs';
import { provideZonelessChangeDetection } from '@angular/core';
import { FleetFeedbackPage } from './fleet-feedback.page';
import { AdminFeedbackHttpService } from './services/admin-feedback-http.service';
import { FleetFeedbackResponse } from './fleet-feedback.models';

function response(over: Partial<FleetFeedbackResponse> = {}): FleetFeedbackResponse {
  return {
    window: { start: '2026-08-19T00:00:00Z', end: '2026-09-18T00:00:00Z', days: 30 },
    totals: { up: 80, down: 40, thumbs: 120 },
    coverage: {
      thumbs: 120, joined: 110, unjoined: 10, joinRate: 0.9167,
      sessionsWithFeedback: 44, sessionsJoined: 44, sessionsOmitted: 0, truncated: false,
    },
    arms: {
      model: [
        { key: 'haiku', up: 40, down: 30, n: 70, downRate: 0.4286, belowFloor: false },
        { key: 'sonnet', up: 38, down: 2, n: 40, downRate: 0.05, belowFloor: false },
        { key: 'nova', up: 1, down: 0, n: 1, downRate: null, belowFloor: true },
      ],
      callsSinceCompaction: [],
      agentSwitch: [],
      turnClass: [],
    },
    reasons: { wrong: 22, tool_failed: 9 },
    minimumN: 20,
    ...over,
  };
}

/** DI stand-in for the HTTP service — a token, not a module mock (house rule). */
class FakeFeedbackHttp {
  lastDays: number | null = null;
  payload: FleetFeedbackResponse = response();
  getFleetFeedback(days: number) {
    this.lastDays = days;
    return of(this.payload);
  }
}

describe('FleetFeedbackPage', () => {
  let fixture: ComponentFixture<FleetFeedbackPage>;
  let http: FakeFeedbackHttp;

  async function render(payload?: FleetFeedbackResponse) {
    http = new FakeFeedbackHttp();
    if (payload) http.payload = payload;
    await TestBed.configureTestingModule({
      imports: [FleetFeedbackPage],
      providers: [
        provideZonelessChangeDetection(),
        { provide: AdminFeedbackHttpService, useValue: http },
      ],
    }).compileComponents();
    fixture = TestBed.createComponent(FleetFeedbackPage);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  const text = () => fixture.nativeElement.textContent as string;

  beforeEach(() => TestBed.resetTestingModule());

  it('defaults to a 30 day window and renders the arm rates', async () => {
    await render();
    expect(http.lastDays).toBe(30);
    expect(text()).toContain('43%'); // haiku, rounded
    expect(text()).toContain('5%');  // sonnet
  });

  it('withholds the rate for an arm under the floor instead of printing one', async () => {
    await render();
    expect(text()).toContain('n < 20');
    // The below-floor arm is still listed with its counts, just not rated.
    expect(text()).toContain('nova');
  });

  it('states the comparison between arms rather than a single fleet score', async () => {
    await render();
    expect(text()).toContain('38 point gap');
    expect(text()).toContain('sonnet');
    expect(text()).toContain('haiku');
  });

  it('never shows a fleet-wide quality percentage', async () => {
    await render();
    // 40 down of 120 thumbs would be 33% — that number must not be rendered
    // anywhere as a headline (spec §9). Only the counts appear.
    const body = text();
    expect(body).toContain('120');
    expect(body).toContain('40');
    expect(body).not.toContain('33%');
  });

  it('reloads with the chosen window when the range is switched', async () => {
    await render();
    const sevenDay = Array.from(fixture.nativeElement.querySelectorAll('button')).find(
      (b): b is HTMLButtonElement => (b as HTMLButtonElement).textContent?.trim() === '7d',
    )!;
    sevenDay.click();
    fixture.detectChanges();
    await fixture.whenStable();
    expect(http.lastDays).toBe(7);
  });

  it('shows an empty state rather than empty tables when nothing was rated', async () => {
    await render(response({
      totals: { up: 0, down: 0, thumbs: 0 },
      arms: { model: [], callsSinceCompaction: [], agentSwitch: [], turnClass: [] },
      reasons: {},
    }));
    expect(text()).toContain('No feedback in this window');
  });

  it('warns when a read cap means the arms rest on a subset', async () => {
    await render(response({
      coverage: {
        thumbs: 2000, joined: 1500, unjoined: 500, joinRate: 0.75,
        sessionsWithFeedback: 400, sessionsJoined: 300, sessionsOmitted: 100, truncated: true,
      },
    }));
    expect(text()).toContain('hit a read cap');
    expect(text()).toContain('100 conversations beyond the join cap');
  });
});
