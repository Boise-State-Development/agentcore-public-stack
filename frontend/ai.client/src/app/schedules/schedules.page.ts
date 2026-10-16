import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroPlus, heroExclamationTriangle, heroRectangleStack } from '@ng-icons/heroicons/outline';
import { ScheduleService } from './services/schedule.service';
import { GrantService } from './services/grant.service';
import { ScheduledPrompt } from './models/schedule.model';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../components/confirmation-dialog/confirmation-dialog.component';
import { ToastService } from '../services/toast/toast.service';
import { FEATURES } from '../services/features';
import { ProjectsService } from '../projects/services/projects.service';
import { cadenceSummary, relativeFuture, relativePast } from './schedule-wording';

/**
 * Schedules management page — list, create/edit (routed), pause/resume,
 * delete, and the grant-enablement UX (docs/specs/scheduled-runs-phase-b-brief.md
 * §2 B3).
 *
 * Open to every signed-in user, and reached from the sidebar's Schedules
 * entry. The only control is the environment's `SCHEDULED_RUNS_ENABLED` kill
 * switch: with it off the `/schedules` list call 404s, which flips
 * `ScheduleService.accessible$` to false and this page renders a graceful
 * "not available" state instead of an error.
 */
@Component({
  selector: 'app-schedules-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, NgIcon],
  providers: [provideIcons({ heroPlus, heroExclamationTriangle, heroRectangleStack })],
  templateUrl: './schedules.page.html',
})
export class SchedulesPage implements OnInit {
  private readonly router = inject(Router);
  private readonly scheduleService = inject(ScheduleService);
  private readonly grantService = inject(GrantService);
  private readonly dialog = inject(Dialog);
  private readonly toast = inject(ToastService);
  private readonly projects = inject(ProjectsService);
  protected readonly features = inject(FEATURES);

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

  /** Project names by id, from the list the sidebar already loads. */
  private readonly projectNames = computed(
    () => new Map(this.projects.projects$().map((p) => [p.projectId, p.name] as const)),
  );

  /** A project schedule is changed and resumed from its project (shared-projects 3.2). */
  projectNameOf(schedule: ScheduledPrompt): string {
    return (schedule.projectId && this.projectNames().get(schedule.projectId)) || 'a project';
  }

  /** Where "Manage in project" goes: the project with its Schedules dialog open. */
  projectLink(schedule: ScheduledPrompt): string[] | null {
    return this.features.projects && schedule.projectId ? ['/projects', schedule.projectId, 'schedules'] : null;
  }

  async ngOnInit(): Promise<void> {
    if (this.features.projects && this.projects.available$() === null && !this.projects.loading$()) {
      void this.projects.load();
    }
    await Promise.allSettled([
      this.scheduleService.loadSchedules(),
      this.grantService.loadStatus(),
    ]);
  }

  onCreateNew(): void {
    this.router.navigate(['/schedules/new']);
  }

  onEdit(schedule: ScheduledPrompt): void {
    const project = this.projectLink(schedule);
    if (project) {
      this.router.navigate(project);
      return;
    }
    if (schedule.projectId) return;
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
      if (schedule.projectId) {
        // A project schedule resumes from its project, where the rule it runs under is checked.
        this.toast.success('Access renewed', `Resume "${schedule.label}" from ${this.projectNameOf(schedule)}.`);
        return;
      }
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

  /** "Every day at 7:00 AM" style summary. */
  cadenceSummary(schedule: ScheduledPrompt): string {
    return cadenceSummary(schedule);
  }

  nextRunLabel(schedule: ScheduledPrompt): string {
    if (schedule.state !== 'active' || !schedule.nextRunAt) {
      return '—';
    }
    return relativeFuture(schedule.nextRunAt);
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
    return `${status} · ${relativePast(schedule.lastRunAt)}`;
  }
}
