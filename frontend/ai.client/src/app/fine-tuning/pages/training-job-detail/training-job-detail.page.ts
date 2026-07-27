import { Component, ChangeDetectionStrategy, computed, DestroyRef, inject, OnInit, signal } from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { DatePipe, KeyValuePipe } from '@angular/common';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroArrowPath,
  heroArrowDownTray,
  heroExclamationTriangle,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { heroStopSolid } from '@ng-icons/heroicons/solid';
import { FineTuningStateService } from '../../services/fine-tuning-state.service';
import { StatusBadgeComponent } from '../../components/status-badge.component';
import { TooltipDirective } from '../../../components/tooltip/tooltip.directive';
import { parseIso } from '../../../utils/date';

@Component({
  selector: 'app-training-job-detail',
  imports: [RouterLink, DatePipe, KeyValuePipe, NgIcon, StatusBadgeComponent, TooltipDirective],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroArrowPath,
      heroArrowDownTray,
      heroStopSolid,
      heroExclamationTriangle,
      heroXMark,
    }),
  ],
  templateUrl: './training-job-detail.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block' },
})
export class TrainingJobDetailPage implements OnInit {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);
  readonly state = inject(FineTuningStateService);

  /** Whether stop confirmation is showing. */
  readonly confirmingStop = signal(false);

  /** Whether logs are being loaded. */
  readonly loadingLogs = signal(false);

  /** Whether a download request is in progress. */
  readonly loadingDownload = signal(false);

  /** Download error message. */
  readonly downloadError = signal<string | null>(null);

  /** Current timestamp, ticks every second for elapsed time display. */
  readonly now = signal(Date.now());

  /** Elapsed time string for active jobs. */
  readonly elapsed = computed(() => {
    const job = this.state.currentTrainingJob();
    if (!job || !this.canStop(job.status)) return null;
    const start = job.training_start_time ?? job.created_at;
    const ms = this.now() - parseIso(start).getTime();
    const totalSeconds = Math.max(0, Math.floor(ms / 1000));
    return this.formatDuration(totalSeconds);
  });

  /** The job ID from the route. */
  private jobId = '';

  /** Polling interval ID. */
  private pollId: ReturnType<typeof setInterval> | null = null;

  ngOnInit(): void {
    this.jobId = this.route.snapshot.paramMap.get('jobId') ?? '';
    if (this.jobId) {
      this.state.loadTrainingJobDetail(this.jobId);
      this.loadLogs();
      this.startTimer();
      this.startPolling();
    }
  }

  /** Start the 1-second timer for elapsed time. */
  private startTimer(): void {
    const timerId = setInterval(() => this.now.set(Date.now()), 1000);
    this.destroyRef.onDestroy(() => clearInterval(timerId));
  }

  /** Start polling job detail + logs every 10s while job is active. */
  private startPolling(): void {
    this.pollId = setInterval(async () => {
      const job = this.state.currentTrainingJob();
      if (job && this.canStop(job.status)) {
        await this.refreshJob();
      } else {
        this.stopPolling();
      }
    }, 10_000);
    this.destroyRef.onDestroy(() => this.stopPolling());
  }

  /** Stop the polling interval. */
  private stopPolling(): void {
    if (this.pollId) {
      clearInterval(this.pollId);
      this.pollId = null;
    }
  }

  /** Refresh the job detail and logs. */
  async refreshJob(): Promise<void> {
    if (!this.jobId) return;
    await Promise.all([
      this.state.loadTrainingJobDetail(this.jobId),
      this.loadLogs(),
    ]);
  }

  /** Load logs for the current job. */
  private async loadLogs(): Promise<void> {
    this.loadingLogs.set(true);
    try {
      await this.state.loadTrainingJobLogs(this.jobId);
    } finally {
      this.loadingLogs.set(false);
    }
  }

  /** Refresh only the logs section. */
  async refreshLogs(): Promise<void> {
    await this.loadLogs();
  }

  /** Show the stop confirmation. */
  confirmStop(): void {
    this.confirmingStop.set(true);
  }

  /** Execute the stop action. */
  async executeStop(): Promise<void> {
    this.confirmingStop.set(false);
    await this.state.stopTrainingJob(this.jobId);
    await this.state.loadTrainingJobDetail(this.jobId);
  }

  /** Cancel the stop confirmation. */
  cancelStop(): void {
    this.confirmingStop.set(false);
  }

  /** Download the model artifact. */
  async downloadArtifact(): Promise<void> {
    this.loadingDownload.set(true);
    this.downloadError.set(null);
    try {
      const response = await this.state.getTrainingDownloadUrl(this.jobId);
      window.open(response.download_url, '_blank');
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to get download URL';
      this.downloadError.set(message);
    } finally {
      this.loadingDownload.set(false);
    }
  }

  /** Whether the job can be stopped. */
  canStop(status: string): boolean {
    return status === 'PENDING' || status === 'TRAINING';
  }

  /** Format seconds into a human-readable duration. */
  formatDuration(seconds: number | null): string {
    if (seconds == null) return '-';
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    if (hours > 0) return `${hours}h ${minutes}m ${secs}s`;
    if (minutes > 0) return `${minutes}m ${secs}s`;
    return `${secs}s`;
  }

  /** Format cost in USD. */
  formatCost(cost: number | null): string {
    if (cost == null) return '-';
    return `$${cost.toFixed(2)}`;
  }

  /** Keys to hide from hyperparameters display (internal/infrastructure details). */
  private readonly hiddenHyperparamKeys = new Set([
    'dynamodb_region', 'dynamodb_table_name', 'table_name', 'job_pk', 'job_sk',
  ]);

  /** Filter hyperparameters to exclude internal keys. */
  filteredHyperparameters(params: Record<string, string>): Record<string, string> {
    return Object.fromEntries(
      Object.entries(params).filter(([key]) => !this.hiddenHyperparamKeys.has(key))
    );
  }
}
