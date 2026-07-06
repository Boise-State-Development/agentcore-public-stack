import { Injectable, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import {
  CreateScheduleRequest,
  ScheduledPrompt,
  UpdateScheduleRequest,
} from '../models/schedule.model';
import { ScheduleApiService } from './schedule-api.service';

/**
 * Signal-based state for the schedules feature. Mirrors AssistantService's
 * shape: a private mutable signal, a readonly public view, and async
 * methods that round-trip through the API service and keep local state in
 * sync so the list page never needs a manual reload after a mutation.
 */
@Injectable({
  providedIn: 'root',
})
export class ScheduleService {
  private apiService = inject(ScheduleApiService);

  private schedules = signal<ScheduledPrompt[]>([]);
  private loading = signal<boolean>(false);
  private error = signal<string | null>(null);
  /**
   * Set once the schedules list call has resolved (success OR a
   * capability-shaped failure). Lets the page distinguish "still loading"
   * from "loaded, and the feature isn't available to this user" without a
   * separate capability signal (see gating note in schedules.page.ts).
   */
  private accessible = signal<boolean | null>(null);

  readonly schedules$ = this.schedules.asReadonly();
  readonly loading$ = this.loading.asReadonly();
  readonly error$ = this.error.asReadonly();
  readonly accessible$ = this.accessible.asReadonly();

  async loadSchedules(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);

    try {
      const response = await firstValueFrom(this.apiService.list());
      this.schedules.set(response?.schedules ?? []);
      this.accessible.set(true);
    } catch (err: unknown) {
      const status = (err as { status?: number } | null)?.status;
      if (status === 403 || status === 404) {
        // Kill switch off or caller lacks the scheduled-runs capability —
        // fail gracefully rather than surfacing an error state.
        this.accessible.set(false);
        this.schedules.set([]);
        return;
      }
      const errorMessage = err instanceof Error ? err.message : 'Failed to load schedules';
      this.error.set(errorMessage);
      throw err;
    } finally {
      this.loading.set(false);
    }
  }

  async createSchedule(request: CreateScheduleRequest): Promise<ScheduledPrompt> {
    this.error.set(null);
    try {
      const schedule = await firstValueFrom(this.apiService.create(request));
      this.schedules.update((current) => [...current, schedule]);
      return schedule;
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to create schedule';
      this.error.set(errorMessage);
      throw err;
    }
  }

  async updateSchedule(scheduleId: string, request: UpdateScheduleRequest): Promise<ScheduledPrompt> {
    this.error.set(null);
    try {
      const updated = await firstValueFrom(this.apiService.update(scheduleId, request));
      this.replaceInList(updated);
      return updated;
    } catch (err) {
      const errorMessage = err instanceof Error ? err.message : 'Failed to update schedule';
      this.error.set(errorMessage);
      throw err;
    }
  }

  async pauseSchedule(scheduleId: string): Promise<ScheduledPrompt> {
    const updated = await firstValueFrom(this.apiService.pause(scheduleId));
    this.replaceInList(updated);
    return updated;
  }

  async resumeSchedule(scheduleId: string): Promise<ScheduledPrompt> {
    const updated = await firstValueFrom(this.apiService.resume(scheduleId));
    this.replaceInList(updated);
    return updated;
  }

  async deleteSchedule(scheduleId: string): Promise<void> {
    await firstValueFrom(this.apiService.delete(scheduleId));
    this.schedules.update((current) => current.filter((s) => s.scheduleId !== scheduleId));
  }

  async getSchedule(scheduleId: string): Promise<ScheduledPrompt | undefined> {
    const cached = this.schedules().find((s) => s.scheduleId === scheduleId);
    if (cached) {
      return cached;
    }
    const schedule = await firstValueFrom(this.apiService.get(scheduleId));
    return schedule;
  }

  private replaceInList(schedule: ScheduledPrompt): void {
    this.schedules.update((current) => {
      const idx = current.findIndex((s) => s.scheduleId === schedule.scheduleId);
      if (idx === -1) {
        return [...current, schedule];
      }
      const next = [...current];
      next[idx] = schedule;
      return next;
    });
  }
}
