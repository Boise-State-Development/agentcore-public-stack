import { describe, expect, it } from 'vitest';
import { LedgerSeries } from '../models';
import {
  TREND_METRICS,
  caveatTone,
  defaultUnit,
  deltaTone,
  formatDelta,
  formatKpiValue,
  formatN,
  rangeEndingToday,
  shortBuild,
  sortTools,
  topGroups,
  trendPoints,
  unitChips,
  unitLabel,
} from './release-ledger.util';
import { readoutFixture, versionsFixture } from '../../../../testing/release-ledger.fixtures';

describe('release ledger formatting', () => {
  it('prints a value under its floor as a dash, never a zero', () => {
    expect(formatKpiValue(null, 'percent')).toBe('—');
    expect(formatKpiValue(undefined, 'usd')).toBe('—');
    expect(formatKpiValue(0, 'percent')).toBe('0.0%');
  });

  it('keeps three significant figures on sub-dollar costs, so a cost per turn still shows its change', () => {
    expect(formatKpiValue(0.00865, 'usd')).toBe('$0.00865');
    expect(formatKpiValue(0.01226, 'usd')).toBe('$0.0123');
    expect(formatKpiValue(3.44, 'usd')).toBe('$3.44');
  });

  it('formats shares, latencies and payloads in their units', () => {
    expect(formatKpiValue(0.109, 'percent')).toBe('10.9%');
    expect(formatKpiValue(0.004, 'percent')).toBe('0.40%');
    expect(formatKpiValue(2714.4, 'ms')).toBe('2,714 ms');
    expect(formatKpiValue(6100, 'chars')).toBe('6,100 chars');
  });

  it('signs a relative delta and dashes a missing one', () => {
    expect(formatDelta(0.4173)).toBe('+41.7%');
    expect(formatDelta(-0.08)).toBe('−8.0%');
    expect(formatDelta(0.0001)).toBe('0.0%');
    expect(formatDelta(null)).toBe('—');
  });

  it('calls a direction only for KPIs that have one, and only past half a percent', () => {
    expect(deltaTone(0.2, 'lower')).toBe('worse');
    expect(deltaTone(-0.2, 'lower')).toBe('better');
    expect(deltaTone(0.2, 'higher')).toBe('better');
    expect(deltaTone(0.2, 'neutral')).toBe('neutral');
    expect(deltaTone(0.001, 'lower')).toBe('neutral');
    expect(deltaTone(null, 'lower')).toBe('none');
  });

  it('prints n as baseline then candidate', () => {
    expect(formatN([956, 117])).toBe('n 956 / 117');
  });

  it('shortens a content hash but keeps a deploy-map label recognisable', () => {
    expect(shortBuild('d5ead33e05f3e229')).toBe('d5ead33e');
    expect(shortBuild('git-0123456789ab')).toBe('git-01234567');
    expect(unitLabel('1.33.0#d5ead33e05f3e229')).toBe('1.33.0 · d5ead33e');
    expect(unitLabel('1.32.0')).toBe('1.32.0');
  });

  it('shows mix shift and missing days in amber, floors and attribution as notes', () => {
    expect(caveatTone('mix_shift')).toBe('warning');
    expect(caveatTone('deploy_in_window')).toBe('warning');
    expect(caveatTone('below_floor')).toBe('note');
    expect(caveatTone('commit_attributed')).toBe('note');
  });
});

describe('version strip chips', () => {
  it('opens on builds when there is a single version to compare (dev between releases)', () => {
    const response = versionsFixture();
    expect(defaultUnit(response)).toBe('build');
    response.versions.push({ ...response.versions[0], appVersion: '1.34.0' });
    expect(defaultUnit(response)).toBe('version');
  });

  it('keys build chips as <appVersion>#<build> and marks deploy-map attribution', () => {
    const chips = unitChips(versionsFixture(), 'build');
    expect(chips.map(c => c.key)).toEqual([
      '1.33.0#git-0123456789ab',
      '1.33.0#f200a6a3514248d3',
      '1.33.0#d5ead33e05f3e229',
    ]);
    expect(chips[0].attributed).toBe(true);
    expect(chips[1].attributed).toBe(false);
    expect(chips[1].deployMarkers).toEqual([{ service: 'runtime', firstSeenAt: '2026-11-06T20:00:00Z' }]);
  });

  it('rolls a version chip up from its builds', () => {
    const [chip] = unitChips(versionsFixture(), 'version');
    expect(chip.key).toBe('1.33.0');
    expect(chip.sublabel).toBe('3 builds');
    expect(chip.deployMarkers.length).toBe(2);
    expect(chip.attributed).toBe(false);
  });
});

describe('trend points', () => {
  const series: LedgerSeries[] = [
    {
      key: '1.33.0#a',
      points: [
        { date: '2026-11-05', totals: { cost: 1, calls: 10 }, kpis: { costPerTurn: { value: 0.1, n: 10 } } },
        { date: '2026-11-06', totals: { cost: 2, calls: 20 }, kpis: { costPerTurn: { value: 0.2, n: 10 } } },
      ],
    },
    {
      key: '1.33.0#b',
      points: [{ date: '2026-11-06', totals: { cost: 3, calls: 5 }, kpis: { costPerTurn: { value: 0.6, n: 5 } } }],
    },
  ];
  const cost = TREND_METRICS.find(m => m.key === 'cost')!;
  const costPerTurn = TREND_METRICS.find(m => m.key === 'costPerTurn')!;

  it('sums an additive total across the units sharing a deploy day', () => {
    expect(trendPoints(series, cost)).toEqual([
      { date: '2026-11-05', totalCost: 1, totalRequests: 10 },
      { date: '2026-11-06', totalCost: 5, totalRequests: 25 },
    ]);
  });

  it('leaves a rate blank on a shared day instead of computing one across builds', () => {
    expect(trendPoints(series, costPerTurn).map(p => p.totalCost)).toEqual([0.1, null]);
  });
});

describe('tables', () => {
  it('sorts tools by the candidate’s estimated input dollars', () => {
    const sorted = sortTools(readoutFixture().tools);
    expect(sorted.map(t => t.toolId)).toEqual(['browse_web', 'fetch_url_content']);
  });

  it('keeps the top twenty groups by candidate cost', () => {
    const groups = Array.from({ length: 25 }, (_, i) => ({
      id: `agent-${i}`,
      calls: [1, 1] as [number, number],
      turns: [1, 1] as [number, number],
      cost: [0, i] as [number, number],
      costPerTurn: { baseline: null, candidate: null, delta: null, n: [1, 1] as [number, number] },
      wastePerTurn: { baseline: null, candidate: null, delta: null, n: [1, 1] as [number, number] },
      toolErrorRate: [null, null] as [null, null],
    }));
    const top = topGroups(groups);
    expect(top.length).toBe(20);
    expect(top[0].id).toBe('agent-24');
  });
});

describe('range', () => {
  it('ends today in UTC and spans the requested days', () => {
    expect(rangeEndingToday(30, new Date('2026-11-07T23:30:00Z'))).toEqual({ from: '2026-10-09', to: '2026-11-07' });
  });
});
