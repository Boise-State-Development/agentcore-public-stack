import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { SyncPolicyControlComponent, SyncIntervalSelection } from './sync-policy-control.component';
import { SyncPolicy } from '../models/sync-policy.model';

function stubPolicy(overrides: Partial<SyncPolicy> = {}): SyncPolicy {
  return {
    policyId: 'syn-abc123def456',
    assistantId: 'assistant1',
    sourceType: 'drive_file',
    sourceRef: 'doc-1',
    interval: 'daily',
    state: 'active',
    stateReason: null,
    nextSyncAt: null,
    lastSyncAt: null,
    lastResult: null,
    createdAt: '2026-07-03T00:00:00Z',
    updatedAt: '2026-07-03T00:00:00Z',
    ...overrides,
  };
}

describe('SyncPolicyControlComponent', () => {
  let fixture: ComponentFixture<SyncPolicyControlComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [SyncPolicyControlComponent],
    }).compileComponents();
    fixture = TestBed.createComponent(SyncPolicyControlComponent);
  });

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  function select(): HTMLSelectElement {
    return fixture.nativeElement.querySelector('select') as HTMLSelectElement;
  }

  function buttonLabels(): string[] {
    return Array.from(fixture.nativeElement.querySelectorAll('button')).map((b) =>
      ((b as HTMLButtonElement).textContent ?? '').trim(),
    );
  }

  function statusLine(): string {
    return ((fixture.nativeElement.querySelector('p')?.textContent as string) ?? '').trim();
  }

  it('defaults the select to manual ("Don\'t auto-sync") with no policy and shows no actions', () => {
    fixture.detectChanges();
    expect(select().value).toBe('manual');
    const manualOption = select().querySelector('option[value="manual"]');
    expect(manualOption?.textContent?.trim()).toBe("Don't auto-sync");
    expect(buttonLabels()).toEqual([]);
    expect(fixture.nativeElement.querySelector('p')).toBeNull();
  });

  it('shows "Last synced" for a manual-only source using the document timestamp', () => {
    const twoHoursAgo = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
    // No policy input → manual-only; the timestamp comes from the document.
    fixture.componentRef.setInput('lastSyncedAt', twoHoursAgo);
    fixture.detectChanges();
    expect(statusLine()).toContain('Last synced 2h ago');
    // Relative age is paired with an absolute date/time.
    expect(statusLine()).toMatch(/Last synced 2h ago · .+/);
  });

  it('renders timestamps that carry a legacy "+00:00Z" suffix (invalid ISO)', () => {
    // The backend historically emitted "…+00:00Z" (offset AND Z), which is
    // unparseable by strict `new Date()` and left the status blank. Already-
    // persisted policies must still render, so the control normalizes it.
    const then = new Date(Date.now() - 3 * 60 * 60 * 1000);
    const legacy = then.toISOString().replace('Z', '+00:00Z');
    fixture.componentRef.setInput('lastSyncedAt', legacy);
    fixture.detectChanges();
    expect(statusLine()).toContain('Last synced 3h ago');
    // Not the blank "Last synced" with no time.
    expect(statusLine()).not.toBe('Last synced');
  });

  it('reflects the policy interval in the select', () => {
    fixture.componentRef.setInput('policy', stubPolicy({ interval: 'weekly' }));
    fixture.detectChanges();
    expect(select().value).toBe('weekly');
  });

  it('emits intervalSelected when the user picks a new interval, then reverts the DOM', () => {
    const emitted: SyncIntervalSelection[] = [];
    fixture.componentInstance.intervalSelected.subscribe((v) => emitted.push(v));
    fixture.detectChanges();

    select().value = 'daily';
    select().dispatchEvent(new Event('change'));

    expect(emitted).toEqual(['daily']);
    // The select only moves for real once the page confirms the mutation
    // and re-renders via the policy input.
    expect(select().value).toBe('manual');
  });

  it('does not emit when the selection matches the current interval', () => {
    const emitted: SyncIntervalSelection[] = [];
    fixture.componentInstance.intervalSelected.subscribe((v) => emitted.push(v));
    fixture.componentRef.setInput('policy', stubPolicy({ interval: 'daily' }));
    fixture.detectChanges();

    select().value = 'daily';
    select().dispatchEvent(new Event('change'));

    expect(emitted).toEqual([]);
  });

  it('shows Sync now + Pause for an active policy and emits their events', () => {
    let ranNow = 0;
    let paused = 0;
    fixture.componentInstance.runNow.subscribe(() => ranNow++);
    fixture.componentInstance.pause.subscribe(() => paused++);
    fixture.componentRef.setInput('policy', stubPolicy({ state: 'active' }));
    fixture.detectChanges();

    const labels = buttonLabels();
    expect(labels).toContain('Sync now');
    expect(labels).toContain('Pause');

    const buttons = fixture.nativeElement.querySelectorAll('button');
    (buttons[0] as HTMLButtonElement).click();
    (buttons[1] as HTMLButtonElement).click();
    expect(ranNow).toBe(1);
    expect(paused).toBe(1);
  });

  it('shows Resume for a user-paused policy', () => {
    let resumed = 0;
    fixture.componentInstance.resume.subscribe(() => resumed++);
    fixture.componentRef.setInput('policy', stubPolicy({ state: 'paused_user' }));
    fixture.detectChanges();

    expect(buttonLabels()).toEqual(['Resume']);
    expect(statusLine()).toBe('Paused');

    (fixture.nativeElement.querySelector('button') as HTMLButtonElement).click();
    expect(resumed).toBe(1);
  });

  it('shows the Reconnect affordance — not Resume — for paused_reauth', () => {
    let reconnected = 0;
    fixture.componentInstance.reconnect.subscribe(() => reconnected++);
    fixture.componentRef.setInput(
      'policy',
      stubPolicy({ state: 'paused_reauth', stateReason: 'Google Drive access expired' }),
    );
    fixture.componentRef.setInput('reconnectLabel', 'Google Drive');
    fixture.detectChanges();

    expect(buttonLabels()).toEqual(['Reconnect Google Drive']);
    expect(statusLine()).toBe('Paused — Google Drive access expired');

    (fixture.nativeElement.querySelector('button') as HTMLButtonElement).click();
    expect(reconnected).toBe(1);
  });

  it('shows Resume with the state reason for an error-paused policy', () => {
    fixture.componentRef.setInput(
      'policy',
      stubPolicy({ state: 'paused_error', stateReason: 'source no longer accessible' }),
    );
    fixture.detectChanges();

    expect(buttonLabels()).toEqual(['Resume']);
    expect(statusLine()).toBe('Paused — source no longer accessible');
  });

  it('shows Resume with an inactivity explanation for paused_inactive', () => {
    fixture.componentRef.setInput('policy', stubPolicy({ state: 'paused_inactive' }));
    fixture.detectChanges();

    expect(buttonLabels()).toEqual(['Resume']);
    expect(statusLine()).toContain('inactive');
  });

  it('describes last (relative + absolute) and next sync for an active policy', () => {
    const twoHoursAgo = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
    const inThreeDays = new Date(Date.now() + 3 * 24 * 60 * 60 * 1000).toISOString();
    fixture.componentRef.setInput(
      'policy',
      stubPolicy({ state: 'active', lastSyncAt: twoHoursAgo, nextSyncAt: inThreeDays }),
    );
    fixture.detectChanges();

    // Relative age, an exact date/time, then the next-run estimate.
    expect(statusLine()).toContain('Synced 2h ago');
    expect(statusLine()).toContain('next sync in 3d');
    expect(statusLine()).toMatch(/Synced 2h ago · .+ · next sync in 3d/);
  });

  it('flags a failed last run on the status line', () => {
    fixture.componentRef.setInput(
      'policy',
      stubPolicy({
        state: 'active',
        lastResult: 'failed',
        lastSyncAt: new Date(Date.now() - 60 * 60 * 1000).toISOString(),
      }),
    );
    fixture.detectChanges();

    expect(statusLine()).toContain('Last sync failed 1h ago');
  });

  it('shows a "Saving…" indicator while a mutation is in flight', () => {
    const twoHoursAgo = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
    fixture.componentRef.setInput('policy', stubPolicy({ state: 'active', lastSyncAt: twoHoursAgo }));
    fixture.detectChanges();
    // Not busy: the status line describes the sync, not a save.
    expect(statusLine()).toContain('Synced 2h ago');

    fixture.componentRef.setInput('busy', true);
    fixture.detectChanges();
    // Busy: the saving indicator takes over so the user sees work happening.
    expect(statusLine()).toBe('Saving…');
    expect(fixture.nativeElement.querySelector('[role="status"]')).not.toBeNull();
  });

  it('shows "Saving…" even for a manual source with no policy or status yet', () => {
    // Enabling sync on a manual-only source: no policy exists yet, so without
    // the busy branch there would be no feedback at all.
    fixture.componentRef.setInput('busy', true);
    fixture.detectChanges();
    expect(statusLine()).toBe('Saving…');
  });

  it('disables all controls while busy', () => {
    fixture.componentRef.setInput('policy', stubPolicy({ state: 'active' }));
    fixture.componentRef.setInput('busy', true);
    fixture.detectChanges();

    expect(select().disabled).toBe(true);
    const buttons = Array.from(
      fixture.nativeElement.querySelectorAll('button'),
    ) as HTMLButtonElement[];
    expect(buttons.length).toBeGreaterThan(0);
    for (const button of buttons) {
      expect(button.disabled).toBe(true);
    }
  });
});
