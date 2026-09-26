import { TestBed } from '@angular/core/testing';
import { ContextMeterComponent, formatTokens } from './context-meter.component';
import { ChatStateService } from '../../services/chat/chat-state.service';
import { QuotaStatusService } from '../../../services/quota/quota-status.service';
import type { QuotaStatus } from '../../../services/quota/quota-status.model';
import type { ContextBreakdown } from '../../services/models/content-types';

function quota(overrides: Partial<QuotaStatus> = {}): QuotaStatus {
  return {
    configured: true,
    unlimited: false,
    tierName: 'Standard',
    matchedBy: 'jwt_role:Faculty',
    monthlyLimit: 10,
    currentUsage: 2.5,
    remaining: 7.5,
    usagePercentage: 25,
    periodType: 'monthly',
    resetInfo: 'Quota resets in 12 day(s)',
    hasActiveOverride: false,
    ...overrides,
  };
}

interface Harness {
  segments: () => { key: string; label: string; tokens: number; pct: number; children: { label: string }[] }[];
  quotaInfo: () => { pctLabel: string; usageLabel: string; limitLabel: string; remainingLabel: string; periodLabel: string } | null;
  quotaUnlimited: () => boolean;
  quotaPctClass: () => string;
  triggerAriaLabel: () => string;
  ringEmpty: () => boolean;
  ringStrokeClass: () => string;
  open: () => boolean;
  toggle: () => void;
  close: () => void;
}

describe('ContextMeterComponent', () => {
  let quotaValue: QuotaStatus | undefined;
  let breakdown: ContextBreakdown | null;
  let contextTokens: number;
  let contextWindow: number;

  function build(): { c: Harness; el: HTMLElement; detect: () => void } {
    const chatStub = {
      costDollars: () => 0.4175,
      costDollarsFor: () => 0.4175,
      contextTokens: () => contextTokens,
      contextTokensFor: () => contextTokens,
      contextWindowSize: () => contextWindow,
      contextWindowFor: () => contextWindow,
      contextPct: () => (contextTokens / contextWindow) * 100,
      contextPctFor: () => (contextTokens / contextWindow) * 100,
      contextBreakdown: () => breakdown,
      contextBreakdownFor: () => breakdown,
    };
    const quotaStub = { status: { value: () => quotaValue } };
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        { provide: ChatStateService, useValue: chatStub },
        { provide: QuotaStatusService, useValue: quotaStub },
      ],
    });
    const fixture = TestBed.createComponent(ContextMeterComponent);
    fixture.detectChanges();
    return {
      c: fixture.componentInstance as unknown as Harness,
      el: fixture.nativeElement as HTMLElement,
      detect: () => fixture.detectChanges(),
    };
  }

  beforeEach(() => {
    quotaValue = quota();
    breakdown = null;
    contextTokens = 20_000;
    contextWindow = 200_000;
  });

  afterEach(() => TestBed.resetTestingModule());

  describe('formatTokens', () => {
    it('abbreviates thousands and millions', () => {
      expect(formatTokens(950)).toBe('950');
      expect(formatTokens(12_516)).toBe('12.5k');
      expect(formatTokens(200_000)).toBe('200k');
      expect(formatTokens(1_000_000)).toBe('1M');
      expect(formatTokens(-5)).toBe('0');
    });
  });

  describe('breakdown', () => {
    it('is empty without a breakdown, so the bar shows usage alone', () => {
      expect(build().c.segments()).toEqual([]);
    });

    it('takes Messages as the residual against the billed total', () => {
      breakdown = {
        total: 19_500,
        partitions: [
          { key: 'system', label: 'System instructions', tokens: 3_000 },
          { key: 'tools', label: 'Tools', tokens: 12_000 },
          { key: 'messages', label: 'Messages', tokens: 4_500 },
        ],
      };
      const segs = build().c.segments();
      expect(segs.map((s) => [s.key, s.tokens])).toEqual([
        ['system', 3_000],
        ['tools', 12_000],
        ['messages', 5_000],
      ]);
      expect(segs.reduce((sum, s) => sum + s.tokens, 0)).toBe(contextTokens);
      expect(segs[1].pct).toBeCloseTo(6);
    });

    it('shows the breakdown as measured when the fixed partitions exceed the billed total', () => {
      contextTokens = 10_000;
      breakdown = {
        total: 19_500,
        partitions: [
          { key: 'system', label: 'System instructions', tokens: 3_000 },
          { key: 'tools', label: 'Tools', tokens: 12_000 },
          { key: 'messages', label: 'Messages', tokens: 4_500 },
        ],
      };
      expect(build().c.segments().find((s) => s.key === 'messages')?.tokens).toBe(4_500);
    });

    it('drops empty partitions and sorts children largest first', () => {
      breakdown = {
        total: 20_000,
        partitions: [
          { key: 'system', label: 'System instructions', tokens: 3_000 },
          { key: 'skills', label: 'Skills', tokens: 0 },
          {
            key: 'tools',
            label: 'Tools',
            tokens: 12_000,
            children: [
              { key: 'builtin', label: 'Built-in', tokens: 2_000 },
              { key: 'mcp:canvas', label: 'Canvas', tokens: 10_000 },
            ],
          },
          { key: 'messages', label: 'Messages', tokens: 5_000 },
        ],
      };
      const segs = build().c.segments();
      expect(segs.map((s) => s.key)).toEqual(['system', 'tools', 'messages']);
      expect(segs[1].children.map((c) => c.label)).toEqual(['Canvas', 'Built-in']);
    });

    it('renders an unknown partition key rather than dropping it', () => {
      breakdown = {
        total: 20_000,
        partitions: [
          { key: 'future', label: 'Something new', tokens: 1_000 },
          { key: 'messages', label: 'Messages', tokens: 19_000 },
        ],
      };
      expect(build().c.segments()[0].label).toBe('Something new');
    });
  });

  describe('ring', () => {
    it('signals urgency by colour alone, never a percentage on the line', () => {
      const calm = build();
      expect(calm.c.ringStrokeClass()).toContain('success');
      const calmText = calm.el.querySelector('button')?.textContent?.trim();

      contextTokens = 150_000; // 75%
      const filling = build();
      expect(filling.c.ringStrokeClass()).toContain('warning');
      expect(filling.el.querySelector('button')?.textContent?.trim()).toBe(calmText);
      expect(filling.el.querySelector('button')?.textContent?.trim()).toBe('');

      contextTokens = 190_000; // 95%
      expect(build().c.ringStrokeClass()).toContain('danger');
    });

    it('summarizes context and cost for assistive tech', () => {
      expect(build().c.triggerAriaLabel()).toBe(
        'Context window 10% full (20k of 200k tokens), conversation cost $0.4175. Show details',
      );
    });
  });

  describe('before anything is measured', () => {
    beforeEach(() => {
      contextTokens = 0;
      contextWindow = 0;
    });

    it('still renders the trigger, so it never shifts the model picker in later', () => {
      const { c, el } = build();
      expect(el.querySelector('button')).not.toBeNull();
      expect(el.querySelector('svg')).not.toBeNull();
      // No stray round-cap dot on an empty ring.
      expect(c.ringEmpty()).toBe(true);
    });

    it('opens to an empty context section above the cost', () => {
      const { c, el, detect } = build();
      c.toggle();
      detect();
      const text = el.querySelector('#context-meter-panel')?.textContent ?? '';
      expect(text).toContain('Not measured yet');
      expect(text).toContain('This conversation');
      expect(c.triggerAriaLabel()).toContain('not measured yet');
    });
  });

  describe('panel', () => {
    it('opens on press and closes on Escape', () => {
      const { c, el, detect } = build();
      expect(el.querySelector('#context-meter-panel')).toBeNull();

      c.toggle();
      detect();
      expect(c.open()).toBe(true);
      expect(el.querySelector('#context-meter-panel')?.textContent).toContain('This conversation');
      expect(el.querySelector('button')?.getAttribute('aria-expanded')).toBe('true');

      c.close();
      detect();
      expect(el.querySelector('#context-meter-panel')).toBeNull();
    });

    it('lists each partition and the free space', () => {
      breakdown = {
        total: 20_000,
        partitions: [
          { key: 'system', label: 'System instructions', tokens: 3_000 },
          { key: 'messages', label: 'Messages', tokens: 17_000 },
        ],
      };
      const { c, el, detect } = build();
      c.toggle();
      detect();
      const text = el.querySelector('#context-meter-panel')?.textContent ?? '';
      expect(text).toContain('System instructions');
      expect(text).toContain('Free space');
      expect(text).toContain('180k');
      expect(text).toContain('$0.4175');
    });
  });

  describe('quota', () => {
    it('exposes quota labels for a normal tier', () => {
      const q = build().c.quotaInfo()!;
      expect(q.pctLabel).toBe('25%');
      expect(q.usageLabel).toBe('$2.50');
      expect(q.limitLabel).toBe('$10.00');
      expect(q.remainingLabel).toBe('$7.50');
      expect(q.periodLabel).toBe('Monthly');
    });

    it('turns the percentage red at/above 90%', () => {
      quotaValue = quota({ usagePercentage: 95, currentUsage: 9.5, remaining: 0.5 });
      expect(build().c.quotaPctClass()).toContain('danger');
    });

    it('reports unlimited with no figures', () => {
      quotaValue = quota({ unlimited: true, monthlyLimit: null });
      const c = build().c;
      expect(c.quotaInfo()).toBeNull();
      expect(c.quotaUnlimited()).toBe(true);
    });

    it('says daily for a daily quota', () => {
      quotaValue = quota({ periodType: 'daily' });
      expect(build().c.quotaInfo()!.periodLabel).toBe('Daily');
    });
  });
});
