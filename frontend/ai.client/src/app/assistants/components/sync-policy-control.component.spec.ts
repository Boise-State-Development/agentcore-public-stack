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

  it('defaults the select to "Manual only" with no policy and shows no actions', () => {
    fixture.detectChanges();
    expect(select().value).toBe('manual');
    expect(buttonLabels()).toEqual([]);
    expect(fixture.nativeElement.querySelector('p')).toBeNull();
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

  it('describes last and next sync on the status line for an active policy', () => {
    const twoHoursAgo = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();
    const inThreeDays = new Date(Date.now() + 3 * 24 * 60 * 60 * 1000).toISOString();
    fixture.componentRef.setInput(
      'policy',
      stubPolicy({ state: 'active', lastSyncAt: twoHoursAgo, nextSyncAt: inThreeDays }),
    );
    fixture.detectChanges();

    expect(statusLine()).toBe('Synced 2h ago · next sync in 3d');
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
