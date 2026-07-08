import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { GrantService } from './grant.service';
import { GrantApiService } from './grant-api.service';

describe('GrantService', () => {
  let service: GrantService;
  let mockApi: {
    status: ReturnType<typeof vi.fn>;
    enable: ReturnType<typeof vi.fn>;
    revoke: ReturnType<typeof vi.fn>;
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockApi = { status: vi.fn(), enable: vi.fn(), revoke: vi.fn() };
    TestBed.configureTestingModule({
      providers: [GrantService, { provide: GrantApiService, useValue: mockApi }],
    });
    service = TestBed.inject(GrantService);
  });

  it('defaults to disabled before any load', () => {
    expect(service.status$()).toEqual({ enabled: false });
  });

  it('loads the grant status', async () => {
    mockApi.status.mockReturnValue(of({ enabled: true, grantId: 'hlg-1' }));

    await service.loadStatus();

    expect(service.status$()).toEqual({ enabled: true, grantId: 'hlg-1' });
  });

  it('falls back to disabled (without throwing) when status load fails', async () => {
    mockApi.status.mockReturnValue(throwError(() => new Error('boom')));

    await service.loadStatus();

    expect(service.status$()).toEqual({ enabled: false });
    expect(service.error$()).toBe('boom');
  });

  it('enables the grant and updates status', async () => {
    mockApi.enable.mockReturnValue(of({ enabled: true, grantId: 'hlg-2' }));

    const result = await service.enable();

    expect(result).toEqual({ enabled: true, grantId: 'hlg-2' });
    expect(service.status$()).toEqual({ enabled: true, grantId: 'hlg-2' });
  });

  it('propagates an enable failure (e.g. 409 no session to pin)', async () => {
    mockApi.enable.mockReturnValue(throwError(() => new Error('409')));

    await expect(service.enable()).rejects.toThrow('409');
  });

  it('revokes and resets status to disabled', async () => {
    mockApi.status.mockReturnValue(of({ enabled: true, grantId: 'hlg-1' }));
    await service.loadStatus();

    mockApi.revoke.mockReturnValue(of({ revoked: true }));
    await service.revoke();

    expect(service.status$()).toEqual({ enabled: false });
  });
});
