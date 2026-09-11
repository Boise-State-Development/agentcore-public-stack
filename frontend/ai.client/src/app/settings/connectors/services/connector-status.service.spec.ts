import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { ConnectorStatusService } from './connector-status.service';
import { UserConnectorsService } from './user-connectors.service';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';

describe('ConnectorStatusService', () => {
  let getStatus: ReturnType<typeof vi.fn>;
  let completion: ReturnType<typeof signal<any>>;

  beforeEach(() => {
    TestBed.resetTestingModule();
    getStatus = vi.fn().mockResolvedValue({ connected: true });
    completion = signal<any>(null);

    TestBed.configureTestingModule({
      providers: [
        { provide: UserConnectorsService, useValue: { getStatus } },
        {
          provide: OAuthConsentService,
          useValue: { completion, inFlightProviders: signal(new Set<string>()) },
        },
      ],
    });
  });

  afterEach(() => TestBed.resetTestingModule());

  const service = () => TestBed.inject(ConnectorStatusService);

  it('reports unknown before anything has been probed', () => {
    expect(service().stateFor('canvas-faculty')).toBe('unknown');
  });

  it('records a connected provider', async () => {
    const s = service();
    await s.ensure(['canvas-faculty']);
    expect(s.stateFor('canvas-faculty')).toBe('connected');
  });

  it('records a disconnected provider', async () => {
    getStatus.mockResolvedValue({ connected: false });
    const s = service();
    await s.ensure(['gmail-employee']);
    expect(s.stateFor('gmail-employee')).toBe('disconnected');
  });

  it('leaves a failed probe as unknown rather than claiming disconnected', async () => {
    // Telling someone they are disconnected because a status call timed out
    // sends them into a consent flow they do not need.
    getStatus.mockRejectedValue(new Error('timeout'));
    const s = service();
    await s.ensure(['canvas-faculty']);
    expect(s.stateFor('canvas-faculty')).toBe('unknown');
  });

  it('probes each provider once even when several tools share it', async () => {
    const s = service();
    await s.ensure(['google-tasks', 'google-tasks', 'google-tasks', null, undefined]);
    expect(getStatus).toHaveBeenCalledTimes(1);
    expect(getStatus).toHaveBeenCalledWith('google-tasks');
  });

  it('does not re-probe a provider it already knows', async () => {
    const s = service();
    await s.ensure(['canvas-faculty']);
    await s.ensure(['canvas-faculty']);
    expect(getStatus).toHaveBeenCalledTimes(1);
  });

  it('ignores tools with no provider', async () => {
    const s = service();
    await s.ensure([null, undefined, '']);
    expect(getStatus).not.toHaveBeenCalled();
  });

  it('re-probes after invalidate', async () => {
    const s = service();
    await s.ensure(['canvas-faculty']);
    s.invalidate('canvas-faculty');
    await s.ensure(['canvas-faculty']);
    expect(getStatus).toHaveBeenCalledTimes(2);
  });

  it('flips to connected when a consent completes', async () => {
    getStatus.mockResolvedValue({ connected: false });
    const s = service();
    await s.ensure(['gmail-employee']);
    expect(s.stateFor('gmail-employee')).toBe('disconnected');

    completion.set({ status: 'success', providerId: 'gmail-employee' });
    TestBed.tick();

    // Without this the chip still reads "connect" after the user just
    // connected, which looks like the consent silently failed.
    expect(s.stateFor('gmail-employee')).toBe('connected');
  });

  it('ignores a failed consent', async () => {
    getStatus.mockResolvedValue({ connected: false });
    const s = service();
    await s.ensure(['gmail-employee']);

    completion.set({ status: 'error', providerId: 'gmail-employee' });
    TestBed.tick();

    expect(s.stateFor('gmail-employee')).toBe('disconnected');
  });
});
