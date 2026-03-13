import { Component, ChangeDetectionStrategy, inject, OnInit, signal } from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { DatePipe } from '@angular/common';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroArrowPath,
  heroArrowDownTray,
  heroStop,
  heroExclamationTriangle,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { FineTuningStateService } from '../../services/fine-tuning-state.service';
import { StatusBadgeComponent } from '../../components/status-badge.component';
import { TooltipDirective } from '../../../components/tooltip/tooltip.directive';

@Component({
  selector: 'app-inference-job-detail',
  imports: [RouterLink, DatePipe, NgIcon, StatusBadgeComponent, TooltipDirective],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroArrowPath,
      heroArrowDownTray,
      heroStop,
      heroExclamationTriangle,
      heroXMark,
    }),
  ],
  templateUrl: './inference-job-detail.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block' },
})
export class InferenceJobDetailPage implements OnInit {
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);
  readonly state = inject(FineTuningStateService);

  /** Whether stop confirmation is showing. */
  readonly confirmingStop = signal(false);

  /** Whether logs are being loaded. */
  readonly loadingLogs = signal(false);

  /** Whether a download request is in progress. */
  readonly loadingDownload = signal(false);

  /** Download error message. */
  readonly downloadError = signal<string | null>(null);

  /** The job ID from the route. */
  private jobId = '';

  ngOnInit(): void {
    this.jobId = this.route.snapshot.paramMap.get('jobId') ?? '';
    if (this.jobId) {
      this.state.loadInferenceJobDetail(this.jobId);
      this.loadLogs();
    }
  }

  /** Refresh the job detail and logs. */
  async refreshJob(): Promise<void> {
    if (!this.jobId) return;
    await Promise.all([
      this.state.loadInferenceJobDetail(this.jobId),
      this.loadLogs(),
    ]);
  }

  /** Load logs for the current job. */
  private async loadLogs(): Promise<void> {
    this.loadingLogs.set(true);
    try {
      await this.state.loadInferenceJobLogs(this.jobId);
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
    await this.state.stopInferenceJob(this.jobId);
    await this.state.loadInferenceJobDetail(this.jobId);
  }

  /** Cancel the stop confirmation. */
  cancelStop(): void {
    this.confirmingStop.set(false);
  }

  /** Download the inference results. */
  async downloadResults(): Promise<void> {
    this.loadingDownload.set(true);
    this.downloadError.set(null);
    try {
      const response = await this.state.getInferenceDownloadUrl(this.jobId);
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
    return status === 'PENDING' || status === 'TRANSFORMING';
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
}
