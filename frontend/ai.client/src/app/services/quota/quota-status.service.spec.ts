import { TestBed } from '@angular/core/testing';
import { QuotaStatusService } from './quota-status.service';
import { ConfigService } from '../config.service';
import { HttpClient } from '@angular/common/http';
import { of } from 'rxjs';
import type { QuotaStatus } from './quota-status.model';

function makeStatus(overrides: Partial<QuotaStatus> = {}): QuotaStatus {
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

describe('QuotaStatusService', () => {
  let httpGet: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    httpGet = vi.fn().mockReturnValue(of(makeStatus()));
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        QuotaStatusService,
        { provide: HttpClient, useValue: { get: httpGet } },
        { provide: ConfigService, useValue: { appApiUrl: () => 'http://api.test' } },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  it('fetches from /costs/quota-status on the configured api base', async () => {
    const service = TestBed.inject(QuotaStatusService);
    const result = await service.fetch();

    expect(httpGet).toHaveBeenCalledWith('http://api.test/costs/quota-status');
    expect(result.monthlyLimit).toBe(10);
    expect(result.usagePercentage).toBe(25);
  });
});
