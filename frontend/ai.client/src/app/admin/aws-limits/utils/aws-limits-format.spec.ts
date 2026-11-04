import { describe, expect, it } from 'vitest';
import { formatLimitValue, formatPercent, unitAndPeriod, usageHeaders } from './aws-limits-format';

describe('aws-limits-format', () => {
  it('never rounds real use down to nothing', () => {
    expect(formatPercent(0.02)).toBe('<0.1%');
    expect(formatPercent(0)).toBe('0%');
    expect(formatPercent(41.25)).toBe('41.3%');
    expect(formatPercent(null)).toBe('N/A');
    expect(formatLimitValue(0.004)).toBe('<0.01');
    expect(formatLimitValue(0)).toBe('0');
    expect(formatLimitValue(408203)).toBe('408,203');
  });

  it('describes unit and period', () => {
    expect(unitAndPeriod({ unit: 'None', periodUnit: 'SECOND', periodValue: 1 })).toBe('per second');
    expect(unitAndPeriod({ unit: 'Count', periodUnit: null, periodValue: null })).toBe('Count');
  });

  it('names the usage column after what the group measures', () => {
    expect(usageHeaders([{ usageBasis: 'peak' }, { usageBasis: null }], 7)).toEqual({
      usage: '7-day peak', utilization: 'Peak utilization', perRow: false,
    });
    expect(usageHeaders([{ usageBasis: 'current' }], 7)).toEqual({ usage: 'Current count', utilization: 'Utilization', perRow: false });
    expect(usageHeaders([{ usageBasis: 'current' }, { usageBasis: 'peak' }], 7)).toEqual({
      usage: 'Usage', utilization: 'Utilization', perRow: true,
    });
  });
});
