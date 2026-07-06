import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { of, throwError } from 'rxjs';
import { ScheduleService } from './schedule.service';
import { ScheduleApiService } from './schedule-api.service';
import { ScheduledPrompt } from '../models/schedule.model';

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

describe('ScheduleService', () => {
  let service: ScheduleService;
  let mockApi: {
    list: ReturnType<typeof vi.fn>;
    get: ReturnType<typeof vi.fn>;
    create: ReturnType<typeof vi.fn>;
    update: ReturnType<typeof vi.fn>;
    pause: ReturnType<typeof vi.fn>;
    resume: ReturnType<typeof vi.fn>;
    delete: ReturnType<typeof vi.fn>;
  };

  beforeEach(() => {
    TestBed.resetTestingModule();
    mockApi = {
      list: vi.fn(),
      get: vi.fn(),
      create: vi.fn(),
      update: vi.fn(),
      pause: vi.fn(),
      resume: vi.fn(),
      delete: vi.fn(),
    };
    TestBed.configureTestingModule({
      providers: [ScheduleService, { provide: ScheduleApiService, useValue: mockApi }],
    });
    service = TestBed.inject(ScheduleService);
  });

  it('loads schedules and marks the feature accessible', async () => {
    const schedule = stubSchedule();
    mockApi.list.mockReturnValue(of({ schedules: [schedule] }));

    await service.loadSchedules();

    expect(service.schedules$()).toEqual([schedule]);
    expect(service.accessible$()).toBe(true);
    expect(service.loading$()).toBe(false);
  });

  it('marks the feature inaccessible (not an error) on a 403', async () => {
    mockApi.list.mockReturnValue(throwError(() => ({ status: 403 })));

    await service.loadSchedules();

    expect(service.accessible$()).toBe(false);
    expect(service.schedules$()).toEqual([]);
    expect(service.error$()).toBeNull();
  });

  it('marks the feature inaccessible on a 404 (kill switch off)', async () => {
    mockApi.list.mockReturnValue(throwError(() => ({ status: 404 })));

    await service.loadSchedules();

    expect(service.accessible$()).toBe(false);
  });

  it('surfaces a real error for non-gating failures', async () => {
    mockApi.list.mockReturnValue(throwError(() => new Error('network blip')));

    await expect(service.loadSchedules()).rejects.toThrow('network blip');
    expect(service.error$()).toBe('network blip');
  });

  it('creates a schedule and appends it locally', async () => {
    const created = stubSchedule();
    mockApi.create.mockReturnValue(of(created));

    const result = await service.createSchedule({
      label: created.label,
      promptText: created.promptText,
      cadence: created.cadence,
      hourLocal: created.hourLocal,
      timezone: created.timezone,
    });

    expect(result).toEqual(created);
    expect(service.schedules$()).toEqual([created]);
  });

  it('replaces the schedule in the list on update', async () => {
    const original = stubSchedule();
    mockApi.list.mockReturnValue(of({ schedules: [original] }));
    await service.loadSchedules();

    const updated = { ...original, label: 'Renamed' };
    mockApi.update.mockReturnValue(of(updated));

    const result = await service.updateSchedule(original.scheduleId, { label: 'Renamed' });

    expect(result.label).toBe('Renamed');
    expect(service.schedules$()).toEqual([updated]);
  });

  it('removes the schedule from the list on delete', async () => {
    const schedule = stubSchedule();
    mockApi.list.mockReturnValue(of({ schedules: [schedule] }));
    await service.loadSchedules();

    mockApi.delete.mockReturnValue(of(undefined));
    await service.deleteSchedule(schedule.scheduleId);

    expect(service.schedules$()).toEqual([]);
  });

  it('pause/resume update the schedule state locally', async () => {
    const schedule = stubSchedule();
    mockApi.list.mockReturnValue(of({ schedules: [schedule] }));
    await service.loadSchedules();

    mockApi.pause.mockReturnValue(of({ ...schedule, state: 'paused' }));
    await service.pauseSchedule(schedule.scheduleId);
    expect(service.schedules$()[0].state).toBe('paused');

    mockApi.resume.mockReturnValue(of({ ...schedule, state: 'active' }));
    await service.resumeSchedule(schedule.scheduleId);
    expect(service.schedules$()[0].state).toBe('active');
  });
});
