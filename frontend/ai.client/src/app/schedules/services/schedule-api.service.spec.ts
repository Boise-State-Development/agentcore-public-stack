import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { ScheduleApiService } from './schedule-api.service';
import { ConfigService } from '../../services/config.service';
import { ScheduledPrompt } from '../models/schedule.model';

const BASE = 'http://localhost:8000/schedules';

function stubSchedule(overrides: Partial<ScheduledPrompt> = {}): ScheduledPrompt {
  return {
    scheduleId: 'sched-abc123def456',
    assistantId: null,
    label: 'Morning Briefing',
    promptText: 'Summarize my classes',
    cadence: 'daily',
    hourLocal: 7,
    weekday: null,
    timezone: 'America/Boise',
    state: 'active',
    stateReason: null,
    nextRunAt: '2026-07-06T14:00:00Z',
    lastRunAt: null,
    lastRunStatus: null,
    lastRunSessionId: null,
    lastError: null,
    runsToday: 0,
    maxRunsPerDay: 24,
    enabledTools: null,
    deliverEmail: false,
    createdAt: '2026-07-05T00:00:00Z',
    updatedAt: '2026-07-05T00:00:00Z',
    ...overrides,
  };
}

describe('ScheduleApiService', () => {
  let service: ScheduleApiService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        ScheduleApiService,
        { provide: ConfigService, useValue: { appApiUrl: signal('http://localhost:8000') } },
      ],
    });
    service = TestBed.inject(ScheduleApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.match(() => true);
    TestBed.resetTestingModule();
  });

  it('lists schedules', async () => {
    const schedules = [stubSchedule()];
    const promise = firstValueFrom(service.list());

    await vi.waitFor(() => {
      const req = httpMock.expectOne(BASE);
      expect(req.request.method).toBe('GET');
      req.flush({ schedules });
    });

    expect(await promise).toEqual({ schedules });
  });

  it('creates a schedule', async () => {
    const created = stubSchedule();
    const promise = firstValueFrom(service.create({
      label: 'Morning Briefing',
      promptText: 'Summarize my classes',
      cadence: 'daily',
      hourLocal: 7,
      timezone: 'America/Boise',
    }));

    await vi.waitFor(() => {
      const req = httpMock.expectOne(BASE);
      expect(req.request.method).toBe('POST');
      req.flush(created);
    });

    expect(await promise).toEqual(created);
  });

  it('updates a schedule via PATCH', async () => {
    const updated = stubSchedule({ label: 'Updated' });
    const promise = firstValueFrom(service.update('sched-abc123def456', { label: 'Updated' }));

    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${BASE}/sched-abc123def456`);
      expect(req.request.method).toBe('PATCH');
      req.flush(updated);
    });

    expect(await promise).toEqual(updated);
  });

  it('pauses a schedule', async () => {
    const paused = stubSchedule({ state: 'paused' });
    const promise = firstValueFrom(service.pause('sched-abc123def456'));

    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${BASE}/sched-abc123def456/pause`);
      expect(req.request.method).toBe('POST');
      req.flush(paused);
    });

    expect(await promise).toEqual(paused);
  });

  it('resumes a schedule', async () => {
    const resumed = stubSchedule({ state: 'active' });
    const promise = firstValueFrom(service.resume('sched-abc123def456'));

    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${BASE}/sched-abc123def456/resume`);
      expect(req.request.method).toBe('POST');
      req.flush(resumed);
    });

    expect(await promise).toEqual(resumed);
  });

  it('deletes a schedule', async () => {
    const promise = firstValueFrom(service.delete('sched-abc123def456'));

    await vi.waitFor(() => {
      const req = httpMock.expectOne(`${BASE}/sched-abc123def456`);
      expect(req.request.method).toBe('DELETE');
      req.flush(null);
    });

    await promise;
  });
});
