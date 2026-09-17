import { describe, it, expect, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import { UserConversationsComponent } from './user-conversations.component';
import { AdminCostHttpService } from '../services/admin-cost-http.service';
import { UserSessionsResponse } from '../models';

const RESPONSE: UserSessionsResponse = {
  userId: 'u1',
  period: '2026-09',
  userPeriodCost: 5.12,
  total: 93,
  unknownCostCount: 2,
  sessions: [
    {
      sessionId: 'bb7c0571-afec-481d-96fb',
      createdAt: '2026-09-01T00:00:00Z',
      lastMessageAt: '2026-09-04T14:33:00Z',
      messageCount: 3,
      modelId: 'us.anthropic.claude-haiku-4-5-20251001-v1:0',
      enabledToolCount: 7,
      agentBound: false,
      lastContextTokens: 42_772,
      contextWindow: 200_000,
      contextShare: 0.2139,
      totalCost: 0.29,
      costKnown: true,
      shareOfUserPeriod: 5.61,
      wastedUsd: 0,
      diagnosisCount: 0,
      topDiagnosisSeverity: null,
    },
    {
      sessionId: 'deadbeef-0000',
      agentBound: true,
      costKnown: false,
      totalCost: null,
      diagnosisCount: 1,
      topDiagnosisSeverity: 'info',
      lastContextTokens: 120_000,
      contextWindow: 200_000,
      contextShare: 0.6,
    },
  ],
};

describe('UserConversationsComponent', () => {
  let getUserSessions: ReturnType<typeof vi.fn>;

  function setup(mock: ReturnType<typeof vi.fn>) {
    getUserSessions = mock;
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideRouter([]), { provide: AdminCostHttpService, useValue: { getUserSessions } }],
    });
    TestBed.overrideComponent(UserConversationsComponent, { set: { template: '<div></div>' } });
    const fixture = TestBed.createComponent(UserConversationsComponent);
    fixture.componentRef.setInput('userId', 'u1');
    fixture.detectChanges();
    return fixture;
  }

  afterEach(() => TestBed.resetTestingModule());

  it('loads the user’s sessions with the default scope and sort', async () => {
    const fixture = setup(vi.fn().mockReturnValue(of(RESPONSE)));
    const c = fixture.componentInstance;
    await vi.waitFor(() => expect(c.sessionsResource.hasValue()).toBe(true));
    expect(getUserSessions).toHaveBeenCalledWith('u1', { allTime: false, sort: 'cost', limit: 100 });
    expect(c.summaryLine()).toBe('Showing 2 of 93 conversations · $5.12 recorded this month · 2 with unrecorded cost');
  });

  it('reloads when the scope or sort changes', async () => {
    const fixture = setup(vi.fn().mockReturnValue(of(RESPONSE)));
    const c = fixture.componentInstance;
    await vi.waitFor(() => expect(c.sessionsResource.hasValue()).toBe(true));

    c.onScopeChange({ target: { value: 'all' } } as unknown as Event);
    fixture.detectChanges();
    await vi.waitFor(() => expect(getUserSessions).toHaveBeenCalledWith('u1', { allTime: true, sort: 'cost', limit: 100 }));

    c.onSortChange({ target: { value: 'recent' } } as unknown as Event);
    fixture.detectChanges();
    await vi.waitFor(() => expect(getUserSessions).toHaveBeenCalledWith('u1', { allTime: true, sort: 'recent', limit: 100 }));
  });

  it('navigates to the session anatomy on open', () => {
    const fixture = setup(vi.fn().mockReturnValue(of(RESPONSE)));
    const router = TestBed.inject(Router);
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);
    fixture.componentInstance.open(RESPONSE.sessions[0]);
    expect(navigate).toHaveBeenCalledWith(['/admin/costs/sessions', 'bb7c0571-afec-481d-96fb']);
  });

  it('formats cells content-free: short id, model tail, context bar, severity dot', () => {
    const c = setup(vi.fn().mockReturnValue(of(RESPONSE))).componentInstance;
    const [known, unknown] = RESPONSE.sessions;
    expect(c.short(known.sessionId)).toBe('bb7c0571');
    expect(c.modelShort(known.modelId)).toBe('claude-haiku-4-5');
    expect(c.modelShort(null)).toBe('—');
    expect(c.tokens(42_772)).toBe('42.8K');
    expect(c.contextWidth(known)).toBe(21);
    expect(c.contextBarClass(known)).toContain('success');
    expect(c.contextBarClass(unknown)).toContain('danger');
    expect(c.dot(unknown.topDiagnosisSeverity)).toContain('info');
    expect(c.currency(0.29)).toBe('$0.29');
  });

  it('surfaces a load error', async () => {
    const fixture = setup(vi.fn().mockReturnValue(throwError(() => new Error('boom'))));
    await vi.waitFor(() => expect(fixture.componentInstance.sessionsResource.error()).toBeTruthy());
    expect(fixture.componentInstance.summaryLine()).toBe('');
  });

  it('lists deleted conversations and says their cost still counts', async () => {
    const fixture = setup(
      vi.fn().mockReturnValue(
        of({
          ...RESPONSE,
          total: 2,
          deletedSessionCount: 1,
          deletedSessionCost: 2.25,
          sessions: [RESPONSE.sessions[0], { ...RESPONSE.sessions[0], sessionId: 'gone-1234-5678', status: 'deleted', totalCost: 2.25 }],
        }),
      ),
    );
    const c = fixture.componentInstance;
    await vi.waitFor(() => expect(c.sessionsResource.hasValue()).toBe(true));
    expect(c.summaryLine()).toBe('2 conversations · $5.12 recorded this month · 2 with unrecorded cost · 1 deleted ($2.25 still counted in the total)');
    // Deleted rows stay in the list, flagged, rather than being dropped.
    const deleted = (c.sessionsResource.value()?.sessions ?? []).filter((s) => s.status === 'deleted');
    expect(deleted.map((s) => s.sessionId)).toEqual(['gone-1234-5678']);
  });
});
