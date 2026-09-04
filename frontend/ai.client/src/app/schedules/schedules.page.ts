import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroPlus, heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import { ScheduleService } from './services/schedule.service';
import { GrantService } from './services/grant.service';
import { ScheduledPrompt } from './models/schedule.model';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../components/confirmation-dialog/confirmation-dialog.component';
import { ToastService } from '../services/toast/toast.service';
import { parseIso } from '../utils/date';

const WEEKDAY_LABELS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

/**
 * Schedules management page — list, create/edit (routed), pause/resume,
 * delete, and the grant-enablement UX (docs/specs/scheduled-runs-phase-b-brief.md
 * §2 B3).
 *
 * Visibility gating: this page (and the nav entry that links to it) is only
 * meaningful for users with the `scheduled-runs` RBAC capability. There is
 * no client-side capability signal yet (see UserPermissions), so gating
 * rides the `/schedules` list call itself — a 403/404 (kill switch off or
 * capability missing) flips `ScheduleService.accessible$` to false and this
 * page renders a graceful "not available" state instead of an error.
 */
@Component({
  selector: 'app-schedules-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, NgIcon],
  providers: [provideIcons({ heroPlus, heroExclamationTriangle })],
  templateUrl: './schedules.page.html',
})
export class SchedulesPage implements OnInit {
  private readonly router = inject(Router);
  private readonly scheduleService = inject(ScheduleService);
  private readonly grantService = inject(GrantService);
  private readonly dialog = inject(Dialog);
  private readonly toast = inject(ToastService);

  readonly schedules = this.scheduleService.schedules$;
  readonly loading = this.scheduleService.loading$;
  readonly accessible = this.scheduleService.accessible$;
  readonly grantStatus = this.grantService.status$;
  readonly grantBusy = this.grantService.loading$;

  readonly busyScheduleIds = signal<Set<string>>(new Set());

  readonly sortedSchedules = computed(() =>
    [...this.schedules()].sort((a, b) => a.label.localeCompare(b.label)),
  );

  readonly needsEnablement = computed(() => !this.grantStatus().enabled);

  async ngOnInit(): Promise<void> {
    await Promise.allSettled([
      this.scheduleService.loadSchedules(),
      this.grantService.loadStatus(),
    ]);
  }

  onCreateNew(): void {
    this.router.navigate(['/schedules/new']);
  }

  onEdit(schedule: ScheduledPrompt): void {
    this.router.navigate(['/schedules', schedule.scheduleId, 'edit']);
  }

  async onEnableScheduledRuns(): Promise<void> {
    try {
      await this.grantService.enable();
      this.toast.success('Scheduled runs enabled', 'Your active schedules can now run unattended.');
    } catch {
      this.toast.error(
        'Could not enable scheduled runs',
        'Please try again, or refresh the page and log in again.',
      );
    }
  }

  /** Re-enable path for a paused_error schedule whose reason is reauth_required. */
  async onReauthenticate(schedule: ScheduledPrompt): Promise<void> {
    try {
      await this.grantService.enable();
      await this.onResume(schedule);
      this.toast.success('Access renewed', `"${schedule.label}" will resume on its normal cadence.`);
    } catch {
      this.toast.error('Could not renew access', 'Please log in again and retry.');
    }
  }

  async onPause(schedule: ScheduledPrompt): Promise<void> {
    this.setBusy(schedule.scheduleId, true);
    try {
      await this.scheduleService.pauseSchedule(schedule.scheduleId);
    } catch {
      this.toast.error('Could not pause schedule');
    } finally {
      this.setBusy(schedule.scheduleId, false);
    }
  }

  async onResume(schedule: ScheduledPrompt): Promise<void> {
    this.setBusy(schedule.scheduleId, true);
    try {
      await this.scheduleService.resumeSchedule(schedule.scheduleId);
    } catch {
      this.toast.error('Could not resume schedule');
    } finally {
      this.setBusy(schedule.scheduleId, false);
    }
  }

  async onDelete(schedule: ScheduledPrompt): Promise<void> {
    const dialogRef = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: 'Delete schedule',
        message: `Are you sure you want to delete "${schedule.label}"? This cannot be undone.`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        destructive: true,
      } as ConfirmationDialogData,
    });

    const confirmed = await firstValueFrom(dialogRef.closed);
    if (!confirmed) {
      return;
    }

    this.setBusy(schedule.scheduleId, true);
    try {
      await this.scheduleService.deleteSchedule(schedule.scheduleId);
      this.toast.success('Schedule deleted');
    } catch {
      this.toast.error('Could not delete schedule');
      this.setBusy(schedule.scheduleId, false);
    }
  }

  isBusy(scheduleId: string): boolean {
    return this.busyScheduleIds().has(scheduleId);
  }

  private setBusy(scheduleId: string, busy: boolean): void {
    this.busyScheduleIds.update((set) => {
      const next = new Set(set);
      if (busy) {
        next.add(scheduleId);
      } else {
        next.delete(scheduleId);
      }
      return next;
    });
  }

  /** "Every day at 7:00 AM (America/Boise)" style summary. */
  cadenceSummary(schedule: ScheduledPrompt): string {
    const time = this.formatHour(schedule.hourLocal);
    switch (schedule.cadence) {
      case 'daily':
        return `Every day at ${time}`;
      case 'weekday':
        return `Every weekday at ${time}`;
      case 'weekly': {
        const day = schedule.weekday !== null && schedule.weekday !== undefined
          ? WEEKDAY_LABELS[schedule.weekday]
          : '';
        return `Every ${day} at ${time}`;
      }
      case 'interval': {
        const value = schedule.intervalValue ?? 0;
        const unit = schedule.intervalUnit ?? 'hours';
        const singular = value === 1 ? unit.replace(/s$/, '') : unit;
        return value === 1 ? `Every ${singular}` : `Every ${value} ${singular}`;
      }
      default:
        return time;
    }
  }

  private formatHour(hour: number): string {
    if (hour === 0) return '12:00 AM';
    if (hour < 12) return `${hour}:00 AM`;
    if (hour === 12) return '12:00 PM';
    return `${hour - 12}:00 PM`;
  }

  nextRunLabel(schedule: ScheduledPrompt): string {
    if (schedule.state !== 'active' || !schedule.nextRunAt) {
      return '—';
    }
    return this.formatRelativeFuture(schedule.nextRunAt);
  }

  private formatRelativeFuture(iso: string): string {
    const then = parseIso(iso).getTime();
    if (Number.isNaN(then)) return '—';
    const diffMins = Math.round((then - Date.now()) / 60_000);
    if (diffMins <= 0) return 'due now';
    if (diffMins < 60) return `in ${diffMins}m`;
    const diffHours = Math.round(diffMins / 60);
    if (diffHours < 24) return `in ${diffHours}h`;
    return `in ${Math.round(diffHours / 24)}d`;
  }

  stateBadgeClasses(schedule: ScheduledPrompt): string {
    const base = 'inline-flex items-center gap-1.5 rounded-2xl px-2.5 py-0.5 text-xs/5 font-medium';
    switch (schedule.state) {
      case 'active':
        return `${base} bg-state-success-100 text-state-success-800 dark:bg-state-success-900/30 dark:text-state-success-300`;
      case 'paused':
        return `${base} bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300`;
      case 'paused_error':
        return `${base} bg-state-warning-100 text-state-warning-800 dark:bg-state-warning-900/30 dark:text-state-warning-300`;
      default:
        return `${base} bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300`;
    }
  }

  stateLabel(schedule: ScheduledPrompt): string {
    switch (schedule.state) {
      case 'active':
        return 'Active';
      case 'paused':
        return 'Paused';
      case 'paused_error':
        return 'Needs attention';
      default:
        return schedule.state;
    }
  }

  /** True for a paused_error schedule whose stateReason marks it as needing re-auth. */
  needsReauth(schedule: ScheduledPrompt): boolean {
    return schedule.state === 'paused_error' && schedule.stateReason === 'reauth_required';
  }

  /** True for a paused_error schedule whose stateReason marks it as needing an OAuth reconnect. */
  needsOAuthReconnect(schedule: ScheduledPrompt): boolean {
    return schedule.state === 'paused_error' && schedule.stateReason === 'oauth_required';
  }

  lastRunLabel(schedule: ScheduledPrompt): string {
    if (!schedule.lastRunAt) {
      return 'Never run';
    }
    const status = schedule.lastRunStatus ?? 'unknown';
    return `${status} · ${this.formatRelativePast(schedule.lastRunAt)}`;
  }

  private formatRelativePast(iso: string): string {
    const then = parseIso(iso).getTime();
    if (Number.isNaN(then)) return '';
    const diffMins = Math.floor((Date.now() - then) / 60_000);
    if (diffMins < 1) return 'just now';
    if (diffMins < 60) return `${diffMins}m ago`;
    const diffHours = Math.floor(diffMins / 60);
    if (diffHours < 24) return `${diffHours}h ago`;
    return `${Math.floor(diffHours / 24)}d ago`;
  }
}
