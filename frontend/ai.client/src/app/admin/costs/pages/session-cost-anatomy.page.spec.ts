import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';
import { SessionCostAnatomyPage } from './session-cost-anatomy.page';
import { AdminCostHttpService } from '../services/admin-cost-http.service';
import { SessionCostAnatomy, SessionProfile } from '../models';

const MOCK_ANATOMY: SessionCostAnatomy = {
  sessionId: 'sess-1',
  totalCost: 0.42,
  totalCacheReadTokens: 20_000,
  totalCacheWriteTokens: 5_000,
  avoidableMissCount: 1,
  partialMissCount: 0,
  partialMissUsd: 0,
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

const MOCK_PROFILE: SessionProfile = {
  sessionId: 'sess-1',
  userId: 'user-9',
  session: {
    sessionId: 'sess-1',
    messageCount: 8,
    modelId: 'us.anthropic.claude-haiku-4-5-20251001-v1:0',
    enabledToolCount: 3,
    agentBound: false,
    lastContextTokens: 24_000,
    contextWindow: 200_000,
    totalCost: 0.42,
    costKnown: true,
    toolCallCount: null,
    compactionCount: null,
    diagnosisCount: 2,
    topDiagnosisSeverity: 'warn',
  },
  callCount: 2,
  peakContextTokens: 24_000,
  compactionThreshold: 100_000,
  writeReadRatio: 0.25,
  attachments: { count: 1, totalBytes: 2_048, byMime: { 'application/pdf': 1 } },
  contextTrajectory: [
    { callIndex: 0, timestamp: '2026-07-19T10:00:00Z', contextTokens: 5_100, cacheStatus: 'first_write' },
    { callIndex: 1, timestamp: '2026-07-19T10:01:00Z', contextTokens: 24_000, cacheStatus: 'miss_avoidable' },
  ],
  modelMix: { 'us.anthropic.claude-haiku-4-5-20251001-v1:0': 2 },
  fingerprintChanges: { systemPrompt: 0, toolConfig: 1, explainedByAgentSwitch: 0 },
  toolCensus: {},
  enabledToolIds: ['browse_web', 'calculator', 'create_artifact'],
  diagnoses: [
    {
      code: 'TOOLCONFIG_MUTATED',
      severity: 'warn',
      headline: 'Tool configuration changed mid-conversation',
      evidence: { distinctToolConfigHashes: 2, callCount: 2 },
      suggestion: 'Check the ordering of the tool list.',
      ref: 'docs/one-pagers/fleet-prefix-spend-anatomy.md',
    },
    {
      code: 'LARGE_TOOLSET',
      severity: 'info',
      headline: 'Large enabled tool set',
      evidence: { enabledToolCount: 3 },
      suggestion: 'Trim.',
      ref: 'docs/one-pagers/cost-effectiveness-roadmap.md',
    },
  ],
  dataCoverage: { toolCensus: false, compactionCount: false, fingerprints: true, cost: true },
};

describe('SessionCostAnatomyPage', () => {
  let getSessionCostAnatomy: ReturnType<typeof vi.fn>;
  let getSessionProfile: ReturnType<typeof vi.fn>;

  function setup(
    mock: ReturnType<typeof vi.fn>,
    profileMock: ReturnType<typeof vi.fn> = vi.fn().mockReturnValue(of(MOCK_PROFILE)),
  ) {
    getSessionCostAnatomy = mock;
    getSessionProfile = profileMock;
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideRouter([]),
        { provide: AdminCostHttpService, useValue: { getSessionCostAnatomy, getSessionProfile } },
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

    expect(page.getStatusClass('hit')).toContain('bg-state-success-100');
    expect(page.getStatusClass('first_write')).toContain('bg-state-info-100');
    expect(page.getStatusClass('miss_ttl_expired')).toContain('bg-state-warning-100');
    expect(page.getStatusClass('miss_avoidable')).toContain('bg-state-danger-100');
    expect(page.getStatusClass('uncached')).toContain('bg-gray-100');
    // Its own colour, between hit and miss: it read from cache and still
    // wasted money, so neither success nor danger would be honest.
    expect(page.getStatusClass('partial_miss')).toContain('bg-category-accent-partial-miss-100');
  });

  it('labels a partial miss as a miss, not a hit', () => {
    const fixture = setup(vi.fn().mockReturnValue(of(MOCK_ANATOMY)));
    const page = fixture.componentInstance;

    expect(page.getStatusLabel('partial_miss')).toBe('Miss (Partial)');
    expect(page.getStatusLabel('miss_avoidable')).toBe('Miss (Avoidable)');
    expect(page.getStatusLabel('hit')).toBe('Hit');
  });

  // ── Profile: what the user was doing, loaded independently of the anatomy ────
  describe('profile', () => {
    it('loads the profile for the routed id and exposes the owning user for the back link', async () => {
      const page = setup(vi.fn().mockReturnValue(of(MOCK_ANATOMY))).componentInstance;
      await vi.waitFor(() => expect(page.profileResource.hasValue()).toBe(true));
      expect(getSessionProfile).toHaveBeenCalledWith('sess-1');
      expect(page.profileUserId()).toBe('user-9');
      expect(page.modelMixLine()).toBe('2 × claude-haiku-4-5');
      expect(page.profileNotFound()).toBe(false);
    });

    it('summarises compaction decisions by kind with the latest summary size', async () => {
      const profile = vi.fn().mockReturnValue(
        of({
          ...MOCK_PROFILE,
          dataCoverage: { ...MOCK_PROFILE.dataCoverage, compactionCount: true, compactionEvents: true, windowTrim: true, prefixTokens: true },
          compactionEventCounts: { forced: 1, applied: 3 },
          lastSummaryTokens: 2_300,
          prefixTokens: { system: 12_000, tools: 48_000 },
          windowTrimCalls: 4,
          windowRemovedMessages: 16,
        }),
      );
      const page = setup(vi.fn().mockReturnValue(of(MOCK_ANATOMY)), profile).componentInstance;
      await vi.waitFor(() => expect(page.profileResource.hasValue()).toBe(true));
      expect(page.compactionEventsLine()).toBe('3 applied · 1 forced · summary 2.3K');
    });

    it('describes one compaction event from its numbers only', () => {
      const page = setup(vi.fn().mockReturnValue(of(MOCK_ANATOMY))).componentInstance;
      expect(
        page.compactionEventTitle({ kind: 'floor_unreachable', checkpoint: 12, summaryTokens: 900, retainedMessages: 30 }),
      ).toBe('floor unreachable · checkpoint 12 · summary 900 · 30 messages retained');
      expect(page.compactionEventTitle({ kind: 'checkpoint' })).toBe('checkpoint');
      expect(
        page.compactionEventTitle({ kind: 'document_offload', documents: 1, documentTokens: 9_000, cacheGapSeconds: 420 }),
      ).toBe('document offload · 1 document · ~9.0K tokens · cache gap 7m');
    });

    it('summarises how the documents were consumed, or falls back when untracked', async () => {
      const profile = vi.fn().mockReturnValue(
        of({
          ...MOCK_PROFILE,
          dataCoverage: { ...MOCK_PROFILE.dataCoverage, documents: true },
          fullDocumentCalls: 2,
          digestOnlyCalls: 1,
          documentReadCalls: 1,
          documentReadPages: 4,
          peakDocumentTokens: 12_000,
        }),
      );
      const page = setup(vi.fn().mockReturnValue(of(MOCK_ANATOMY)), profile).componentInstance;
      await vi.waitFor(() => expect(page.profileResource.hasValue()).toBe(true));
      expect(page.documentsLine()).toBe('2 full · 1 digest-only · read 4 pages in 1 calls · peak ~12.0K');

      const untracked = setup(vi.fn().mockReturnValue(of(MOCK_ANATOMY))).componentInstance;
      await vi.waitFor(() => expect(untracked.profileResource.hasValue()).toBe(true));
      expect(untracked.documentsLine()).toBe('');
    });

    it('badges a call by what the model had of the documents', () => {
      const page = setup(vi.fn().mockReturnValue(of(MOCK_ANATOMY))).componentInstance;
      const base = MOCK_ANATOMY.calls[0];
      expect(page.documentBadge({ ...base })).toBe('');
      expect(page.documentBadge({ ...base, hasDocuments: true, documentCount: 1 })).toBe('doc');
      expect(page.documentBadge({ ...base, hasDocuments: false, documentDigests: 2 })).toBe('digest');
      expect(page.documentBadge({ ...base, hasDocuments: false, documentReads: { calls: 1, pages: 4, bytes: 9 } })).toBe('+4p');
      expect(page.documentDetail({ ...base })).toBe('');
      expect(
        page.documentDetail({
          ...base,
          hasDocuments: true,
          documentCount: 2,
          documentTokens: 12_000,
          documentMime: { pdf: 2 },
          documentsAttached: 2,
          documentReads: { calls: 1, pages: 4, bytes: 9 },
        }),
      ).toBe('2 inline ~12.0K (pdf×2) · 2 attached this turn · document_read ×1 → 4 pages');
    });

    it('survives a missing profile without touching the anatomy', async () => {
      const page = setup(
        vi.fn().mockReturnValue(of(MOCK_ANATOMY)),
        vi.fn().mockReturnValue(throwError(() => new HttpErrorResponse({ status: 404 }))),
      ).componentInstance;
      await vi.waitFor(() => expect(page.profileResource.error()).toBeTruthy());
      await vi.waitFor(() => expect(page.anatomyResource.hasValue()).toBe(true));
      expect(page.profileNotFound()).toBe(true);
      expect(page.profileUserId()).toBeNull();
      expect(page.rows()).toHaveLength(2);
    });

    it('toggles a diagnosis open and closed', () => {
      const page = setup(vi.fn().mockReturnValue(of(MOCK_ANATOMY))).componentInstance;
      expect(page.isDiagnosisOpen('LARGE_TOOLSET')).toBe(false);
      page.toggleDiagnosis('LARGE_TOOLSET');
      expect(page.isDiagnosisOpen('LARGE_TOOLSET')).toBe(true);
      page.toggleDiagnosis('LARGE_TOOLSET');
      expect(page.isDiagnosisOpen('LARGE_TOOLSET')).toBe(false);
    });

    it('renders evidence as humanized label + formatted value', () => {
      const page = setup(vi.fn().mockReturnValue(of(MOCK_ANATOMY))).componentInstance;
      const entries = page.evidenceEntries(MOCK_PROFILE.diagnoses[0]);
      expect(entries).toEqual([
        { key: 'distinctToolConfigHashes', label: 'Distinct tool config hashes', value: '2' },
        { key: 'callCount', label: 'Call count', value: '2' },
      ]);
      expect(page.severityLabel('warn')).toBe('Warning');
      expect(page.chipClass('high')).toContain('danger');
      expect(page.bytes(2_048)).toBe('2.0 KB');
    });

    // ⚠️ Never replace the `navigator` global here (e.g. `vi.stubGlobal('navigator',
    // {...navigator, clipboard})`): a spread drops prototype getters such as
    // `userAgent`, and with the suite running `isolate: false` Angular's forms
    // `DefaultValueAccessor` in a *later* spec file then throws on
    // `navigator.userAgent.toLowerCase()`. Override only the `clipboard`
    // property and put it back.
    function withClipboard<T>(clipboard: unknown, run: () => Promise<T>): Promise<T> {
      const original = Object.getOwnPropertyDescriptor(navigator, 'clipboard');
      Object.defineProperty(navigator, 'clipboard', { value: clipboard, configurable: true });
      return run().finally(() => {
        if (original) Object.defineProperty(navigator, 'clipboard', original);
        else delete (navigator as unknown as { clipboard?: unknown }).clipboard;
      });
    }

    it('copies profile + anatomy as one JSON document and flips the button label', async () => {
      const writeText = vi.fn().mockResolvedValue(undefined);
      await withClipboard({ writeText }, async () => {
        const page = setup(vi.fn().mockReturnValue(of(MOCK_ANATOMY))).componentInstance;
        await vi.waitFor(() => expect(page.profileResource.hasValue()).toBe(true));
        await vi.waitFor(() => expect(page.anatomyResource.hasValue()).toBe(true));

        await page.copyDiagnosticJson();

        expect(writeText).toHaveBeenCalledTimes(1);
        const doc = JSON.parse(writeText.mock.calls[0][0]);
        expect(doc.profile.sessionId).toBe('sess-1');
        expect(doc.anatomy.calls).toHaveLength(2);
        expect(doc._about).toContain('Content-free');
        expect(page.copied()).toBe(true);
      });
    });

    it('does not throw when the clipboard is unavailable', async () => {
      await withClipboard(undefined, async () => {
        const page = setup(vi.fn().mockReturnValue(of(MOCK_ANATOMY))).componentInstance;
        await vi.waitFor(() => expect(page.profileResource.hasValue()).toBe(true));
        await expect(page.copyDiagnosticJson()).resolves.toBeUndefined();
        expect(page.copied()).toBe(false);
      });
    });
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
