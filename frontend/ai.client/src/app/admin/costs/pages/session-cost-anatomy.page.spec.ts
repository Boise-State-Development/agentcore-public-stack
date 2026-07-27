import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { SessionCostAnatomyPage } from './session-cost-anatomy.page';
import { AdminCostHttpService } from '../services/admin-cost-http.service';
import { SessionCostAnatomy } from '../models';

const MOCK_ANATOMY: SessionCostAnatomy = {
  sessionId: 'sess-1',
  totalCost: 0.42,
  totalCacheReadTokens: 20_000,
  totalCacheWriteTokens: 5_000,
  avoidableMissCount: 1,
  wastedUsd: 0.03,
  agentSwitchMissCount: 0,
  agentSwitchUsd: 0,
  cacheEfficiency: 0.8,
  calls: [
    {
      timestamp: '2026-07-19T10:00:00Z',
      messageId: 1,
      modelId: 'us.anthropic.claude-sonnet-5',
      inputTokens: 100,
      outputTokens: 50,
      cacheReadTokens: 0,
      cacheWriteTokens: 5_000,
      cost: 0.1,
      cacheStatus: 'first_write',
      cacheGapSeconds: null,
      wastedUsd: 0,
      prefixFingerprints: { toolConfigHash: 'aaaa1111', systemPromptHash: 'bbbb2222', historyHash: 'cccc3333', messageCount: 2 },
    },
    {
      timestamp: '2026-07-19T10:01:00Z',
      messageId: 2,
      modelId: 'us.anthropic.claude-sonnet-5',
      inputTokens: 120,
      outputTokens: 60,
      cacheReadTokens: 0,
      cacheWriteTokens: 5_200,
      cost: 0.12,
      cacheStatus: 'miss_avoidable',
      cacheGapSeconds: 60,
      wastedUsd: 0.03,
      prefixFingerprints: { toolConfigHash: 'DIFFERENT', systemPromptHash: 'bbbb2222', historyHash: 'cccc3333', messageCount: 4 },
    },
  ],
};

describe('SessionCostAnatomyPage', () => {
  let getSessionCostAnatomy: ReturnType<typeof vi.fn>;

  function setup(mock: ReturnType<typeof vi.fn>) {
    getSessionCostAnatomy = mock;
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: AdminCostHttpService, useValue: { getSessionCostAnatomy } },
      ],
    });
    TestBed.overrideComponent(SessionCostAnatomyPage, {
      set: { template: '<div></div>' },
    });
    const fixture = TestBed.createComponent(SessionCostAnatomyPage);
    fixture.componentRef.setInput('id', 'sess-1');
    fixture.detectChanges();
    return fixture;
  }

  afterEach(() => {
    TestBed.resetTestingModule();
  });

  it('loads the anatomy for the routed session id and annotates fingerprint diffs', async () => {
    const fixture = setup(vi.fn().mockReturnValue(of(MOCK_ANATOMY)));
    const page = fixture.componentInstance;

    await vi.waitFor(() => {
      expect(page.anatomyResource.hasValue()).toBe(true);
    });

    expect(getSessionCostAnatomy).toHaveBeenCalledWith('sess-1');
    const rows = page.rows();
    expect(rows).toHaveLength(2);
    expect(rows[0].changed).toEqual([]);
    // The flipped toolConfigHash is the diagnosis on the miss_avoidable row.
    expect(rows[1].changed).toEqual(['toolConfigHash']);
    expect(page.notFound()).toBe(false);
  });

  it('treats a 404 as "session has no cost rows"', async () => {
    const fixture = setup(
      vi.fn().mockReturnValue(
        throwError(() => new HttpErrorResponse({ status: 404, statusText: 'Not Found' }))
      )
    );
    const page = fixture.componentInstance;

    await vi.waitFor(() => {
      expect(page.anatomyResource.error()).toBeTruthy();
    });
    expect(page.notFound()).toBe(true);
  });

  it('does not report notFound for other errors', async () => {
    const fixture = setup(
      vi.fn().mockReturnValue(
        throwError(() => new HttpErrorResponse({ status: 500, statusText: 'Server Error' }))
      )
    );
    const page = fixture.componentInstance;

    await vi.waitFor(() => {
      expect(page.anatomyResource.error()).toBeTruthy();
    });
    expect(page.notFound()).toBe(false);
  });

  it('toggles row expansion', () => {
    const fixture = setup(vi.fn().mockReturnValue(of(MOCK_ANATOMY)));
    const page = fixture.componentInstance;

    expect(page.isExpanded(0)).toBe(false);
    page.toggleExpand(0);
    expect(page.isExpanded(0)).toBe(true);
    page.toggleExpand(0);
    expect(page.isExpanded(0)).toBe(false);
  });

  it('formats cache efficiency, handling null', () => {
    const fixture = setup(vi.fn().mockReturnValue(of(MOCK_ANATOMY)));
    const page = fixture.componentInstance;

    expect(page.formatEfficiency(null)).toBe('—');
    expect(page.formatEfficiency(0.8)).toBe('80.0%');
  });

  it('formats cache gaps, handling null', () => {
    const fixture = setup(vi.fn().mockReturnValue(of(MOCK_ANATOMY)));
    const page = fixture.componentInstance;

    expect(page.formatGap(null)).toBe('—');
    expect(page.formatGap(undefined)).toBe('—');
    expect(page.formatGap(42)).toBe('42s');
    expect(page.formatGap(60)).toBe('1m');
    expect(page.formatGap(312)).toBe('5m 12s');
  });

  it('maps cache statuses to color-coded badge classes', () => {
    const fixture = setup(vi.fn().mockReturnValue(of(MOCK_ANATOMY)));
    const page = fixture.componentInstance;

    expect(page.getStatusClass('hit')).toContain('bg-green-100');
    expect(page.getStatusClass('first_write')).toContain('bg-blue-100');
    expect(page.getStatusClass('miss_ttl_expired')).toContain('bg-yellow-100');
    expect(page.getStatusClass('miss_avoidable')).toContain('bg-red-100');
    expect(page.getStatusClass('uncached')).toContain('bg-gray-100');
  });

  // ── #756 — explained vs unexplained avoidable misses ──────────────────────────
  describe('agent-switch split', () => {
    async function loadWith(overrides: Partial<SessionCostAnatomy>) {
      const fixture = setup(
        vi.fn().mockReturnValue(of({ ...MOCK_ANATOMY, ...overrides })),
      );
      const page = fixture.componentInstance;
      await vi.waitFor(() => expect(page.anatomyResource.hasValue()).toBe(true));
      return page;
    }

    it('counts every avoidable miss as unexplained when none is a switch', async () => {
      const page = await loadWith({ avoidableMissCount: 3, agentSwitchMissCount: 0 });
      expect(page.unexplainedMisses()).toBe(3);
    });

    it('subtracts the explained subset', async () => {
      // The point of the split: the total stays whole because the money was really
      // spent, and the remainder is what a regression would actually move.
      const page = await loadWith({ avoidableMissCount: 3, agentSwitchMissCount: 2 });
      expect(page.unexplainedMisses()).toBe(1);
    });

    it('reports zero when every miss is a switch', async () => {
      const page = await loadWith({ avoidableMissCount: 2, agentSwitchMissCount: 2 });
      expect(page.unexplainedMisses()).toBe(0);
    });

    it('never reports a negative remainder', async () => {
      // Defensive: the two figures come from separate passes over the same rows, and
      // a nonsense pair must not render as "-4 unexplained".
      const page = await loadWith({ avoidableMissCount: 1, agentSwitchMissCount: 5 });
      expect(page.unexplainedMisses()).toBe(0);
    });

    it('is zero before the resource resolves', async () => {
      const page = setup(vi.fn().mockReturnValue(of(MOCK_ANATOMY))).componentInstance;
      expect(page.unexplainedMisses()).toBe(0);
    });
  });
});
