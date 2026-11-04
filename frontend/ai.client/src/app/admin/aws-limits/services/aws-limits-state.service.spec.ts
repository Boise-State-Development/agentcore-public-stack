import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ConfigService } from '../../../services/config.service';
import { AwsLimit, AwsLimitGroup, AwsLimitsDashboard } from '../models';
import { AwsLimitsStateService, sortByUtilization, utilizationLevel } from './aws-limits-state.service';

const BASE = 'http://api.test/admin/aws-limits';

function limit(overrides: Partial<AwsLimit> = {}): AwsLimit {
  return {
    serviceCode: 'lambda',
    serviceName: 'AWS Lambda',
    quotaCode: 'L-B99A9384',
    quotaName: 'Concurrent executions',
    description: null,
    appliedValue: 1000,
    defaultValue: 1000,
    unit: 'Count',
    periodValue: null,
    periodUnit: null,
    adjustable: true,
    globalQuota: false,
    hasUsageMetric: true,
    usage: 500,
    usageSource: null,
    usageBasis: 'peak',
    usageNote: null,
    utilization: 50,
    consoleUrl: 'https://console',
    requestable: true,
    ...overrides,
  };
}

function group(id: string, limits: AwsLimit[], overrides: Partial<AwsLimitGroup> = {}): AwsLimitGroup {
  return { id, label: id, description: '', limits, docLinks: [], error: null, usageError: null, fetchedAt: null, ...overrides };
}

function dashboard(groups: AwsLimitGroup[], overrides: Partial<AwsLimitsDashboard> = {}): AwsLimitsDashboard {
  return {
    region: 'us-west-2',
    generatedAt: '2026-11-03T10:00:00Z',
    asOf: '2026-11-03T09:56:00Z',
    allBedrock: false,
    canRequestIncrease: true,
    groups,
    summary: { monitored: 3, atOrAbove80: 1, atOrAbove95: 0, openRequests: 0 },
    autoManagement: null,
    guidanceUrl: 'https://docs',
    usageWindowDays: 7,
    ...overrides,
  };
}

describe('AwsLimitsStateService', () => {
  let service: AwsLimitsStateService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: ConfigService, useValue: { appApiUrl: signal('http://api.test') } },
      ],
    });
    service = TestBed.inject(AwsLimitsStateService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('loads the dashboard and takes canRequestIncrease from the server', async () => {
    const loading = service.load();
    http.expectOne(BASE).flush(dashboard([group('jobs', [limit()])], { canRequestIncrease: false }));
    await loading;
    expect(service.canRequestIncrease()).toBe(false);
    expect(service.visibleGroups().map(g => g.id)).toEqual(['jobs']);
  });

  it('asks for every Bedrock quota when the toggle is on, and refresh bypasses the cache', async () => {
    const toggled = service.setShowAllBedrock(true);
    const call = http.expectOne(r => r.url === BASE);
    expect(call.request.params.get('all_bedrock')).toBe('true');
    call.flush(dashboard([]));
    await toggled;

    const refreshing = service.refresh();
    const again = http.expectOne(r => r.url === BASE);
    expect(again.request.params.get('refresh')).toBe('true');
    expect(again.request.params.get('all_bedrock')).toBe('true');
    again.flush(dashboard([]));
    await refreshing;
    // The history is read when its dialog opens, not on every refresh.
    http.expectNone(r => r.url === `${BASE}/requests`);
  });

  it('shows the API error and keeps going', async () => {
    const loading = service.load();
    http.expectOne(BASE).flush({ detail: 'Not found' }, { status: 404, statusText: 'Not Found' });
    await loading;
    expect(service.error()).toBe('Not found');
    expect(service.loading()).toBe(false);
  });

  it('filters by group, adjustability and the 80% threshold, sorted highest first', async () => {
    const loading = service.load();
    http.expectOne(BASE).flush(
      dashboard([
        group('jobs', [
          limit({ quotaCode: 'a', quotaName: 'A', utilization: 50 }),
          limit({ quotaCode: 'b', quotaName: 'B', utilization: 97 }),
          limit({ quotaCode: 'c', quotaName: 'C', utilization: null, usage: null, adjustable: false }),
        ]),
        group('data', [limit({ quotaCode: 'd', quotaName: 'D', utilization: 10 })]),
        group('kb', [], { docLinks: [{ label: 'S3 Vectors limits', url: 'https://x' }] }),
      ]),
    );
    await loading;

    // Start from no row filters; the defaults are covered below.
    service.setFilters({ adjustableOnly: false, hideUnused: false });
    expect(service.visibleGroups()[0].limits.map(l => l.quotaCode)).toEqual(['b', 'a', 'c']);
    expect(service.visibleGroups().map(g => g.id)).toEqual(['jobs', 'data', 'kb']);

    service.setFilters({ nearLimitOnly: true });
    expect(service.visibleGroups().map(g => [g.id, g.limits.map(l => l.quotaCode)])).toEqual([['jobs', ['b']]]);

    service.setFilters({ nearLimitOnly: false, adjustableOnly: true, group: 'jobs' });
    expect(service.visibleGroups().map(g => g.limits.map(l => l.quotaCode))).toEqual([['b', 'a']]);
  });

  it('starts with Adjustable only and Hide unused on', async () => {
    expect(service.filters()).toEqual({ group: '', adjustableOnly: true, nearLimitOnly: false, hideUnused: true });
    const loading = service.load();
    http.expectOne(BASE).flush(
      dashboard([
        group('jobs', [
          limit({ quotaCode: 'used' }),
          limit({ quotaCode: 'fixed', adjustable: false }),
          limit({ quotaCode: 'idle', usage: 0, utilization: 0 }),
        ]),
        group('kb', [], { docLinks: [{ label: 'S3 Vectors limits', url: 'https://x' }] }),
      ]),
    );
    await loading;
    expect(service.visibleGroups().map(g => [g.id, g.limits.map(l => l.quotaCode)])).toEqual([['jobs', ['used']]]);
  });

  it('hides rows with a peak of 0 or nothing measured', async () => {
    const loading = service.load();
    http.expectOne(BASE).flush(
      dashboard([
        group('runtime', [
          limit({ quotaCode: 'used', usage: 3, utilization: 1 }),
          limit({ quotaCode: 'idle', usage: 0, utilization: 0 }),
          limit({ quotaCode: 'unmeasured', usage: null, utilization: null, usageBasis: null, usageNote: 'Not measured by AWS' }),
        ]),
        group('fine_tuning', [limit({ quotaCode: 'gpu', usage: 0, utilization: null, usageNote: 'Limit is 0.' })]),
      ]),
    );
    await loading;

    service.setFilters({ hideUnused: true });
    expect(service.visibleGroups().map(g => [g.id, g.limits.map(l => l.quotaCode)])).toEqual([['runtime', ['used']]]);
  });

  it('adds a submitted request to the history at once, then re-reads', async () => {
    const record = {
      requestId: 'r-1', serviceCode: 'lambda', serviceName: null, quotaCode: 'L-1', quotaName: 'Q',
      desiredValue: 2000, status: 'PENDING', requestType: null, caseId: null, created: null,
      lastUpdated: null, unit: null, requestedBy: 'admin@example.edu', consoleUrl: 'c',
    };
    const done = service.recordSubmitted(record);
    expect(service.history()[0].requestId).toBe('r-1');
    http.expectOne(r => r.url === `${BASE}/requests`).flush({ region: 'us-west-2', requests: [record], asOf: 'x' });
    http.expectOne(r => r.url === BASE).flush(dashboard([]));
    await done;
    expect(service.history().length).toBe(1);
  });
});

describe('utilization helpers', () => {
  it('uses the 80% and 95% thresholds', () => {
    expect(utilizationLevel(null)).toBe('none');
    expect(utilizationLevel(79.9)).toBe('ok');
    expect(utilizationLevel(80)).toBe('warning');
    expect(utilizationLevel(94.9)).toBe('warning');
    expect(utilizationLevel(95)).toBe('danger');
  });

  it('puts rows without usage last', () => {
    const rows = [limit({ quotaName: 'x', utilization: null }), limit({ quotaName: 'y', utilization: 1 })];
    expect(sortByUtilization(rows).map(r => r.quotaName)).toEqual(['y', 'x']);
  });
});
