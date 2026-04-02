import { Component, ChangeDetectionStrategy, DestroyRef, inject, OnInit, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { DatePipe, DecimalPipe } from '@angular/common';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroPlus,
  heroArrowPath,
  heroExclamationTriangle,
  heroXMark,
  heroLockClosed,
  heroArrowDownTray,
  heroInformationCircle,
  heroChevronRight,
} from '@ng-icons/heroicons/outline';
import { heroStopSolid } from '@ng-icons/heroicons/solid';
import { TooltipDirective } from '../../../components/tooltip/tooltip.directive';
import { FineTuningStateService } from '../../services/fine-tuning-state.service';
import { StatusBadgeComponent } from '../../components/status-badge.component';
import { QuotaCardComponent } from '../../components/quota-card.component';
import type { JobResponse, InferenceJobResponse } from '../../models/fine-tuning.models';

@Component({
  selector: 'app-fine-tuning-dashboard',
  imports: [RouterLink, DatePipe, DecimalPipe, NgIcon, TooltipDirective, StatusBadgeComponent, QuotaCardComponent],
  providers: [
    provideIcons({
      heroPlus,
      heroArrowPath,
      heroExclamationTriangle,
      heroXMark,
      heroStopSolid,
      heroLockClosed,
      heroArrowDownTray,
      heroInformationCircle,
      heroChevronRight,
    }),
  ],
  templateUrl: './fine-tuning-dashboard.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block' },
})
export class FineTuningDashboardPage implements OnInit {
  readonly state = inject(FineTuningStateService);
  private readonly router = inject(Router);
  private readonly destroyRef = inject(DestroyRef);

  /** Job ID currently showing stop confirmation for training jobs. */
  readonly confirmingStopTraining = signal<string | null>(null);

  /** Job ID currently showing stop confirmation for inference jobs. */
  readonly confirmingStopInference = signal<string | null>(null);

  /** Current timestamp, ticks every second for elapsed time display. */
  readonly now = signal(Date.now());

  /** Polling interval ID. */
  private pollId: ReturnType<typeof setInterval> | null = null;

  ngOnInit(): void {
    this.state.loadDashboard();
    this.startTimer();
    this.startPolling();
  }

  /** Start the 1-second timer for elapsed time display. */
  private startTimer(): void {
    const timerId = setInterval(() => this.now.set(Date.now()), 1000);
    this.destroyRef.onDestroy(() => clearInterval(timerId));
  }

  /** Start polling dashboard data every 10s while any job is active. */
  private startPolling(): void {
    this.pollId = setInterval(async () => {
      if (this.hasActiveJobs()) {
        await this.state.loadDashboard();
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

  /** Check if any training or inference job is currently active. */
  private hasActiveJobs(): boolean {
    const hasActiveTraining = this.state.trainingJobs().some(j => this.canStopTraining(j.status));
    const hasActiveInference = this.state.inferenceJobs().some(j => this.canStopInference(j.status));
    return hasActiveTraining || hasActiveInference;
  }

  navigateToNewTrainingJob(): void {
    this.router.navigate(['/fine-tuning/new-training']);
  }

  navigateToNewInferenceJob(): void {
    this.router.navigate(['/fine-tuning/new-inference']);
  }

  /** Show inline stop confirmation for a training job. */
  confirmStopTraining(jobId: string): void {
    this.confirmingStopTraining.set(jobId);
  }

  /** Execute the stop for a training job. */
  async executeStopTraining(jobId: string): Promise<void> {
    this.confirmingStopTraining.set(null);
    await this.state.stopTrainingJob(jobId);
  }

  /** Cancel stop confirmation for a training job. */
  cancelStopTraining(): void {
    this.confirmingStopTraining.set(null);
  }

  /** Show inline stop confirmation for an inference job. */
  confirmStopInference(jobId: string): void {
    this.confirmingStopInference.set(jobId);
  }

  /** Execute the stop for an inference job. */
  async executeStopInference(jobId: string): Promise<void> {
    this.confirmingStopInference.set(null);
    await this.state.stopInferenceJob(jobId);
  }

  /** Cancel stop confirmation for an inference job. */
  cancelStopInference(): void {
    this.confirmingStopInference.set(null);
  }

  /** Refresh all dashboard data. */
  async refresh(): Promise<void> {
    await this.state.loadDashboard();
  }

  /** Format cost as USD. */
  formatCost(cost: number | null): string {
    if (cost === null || cost === undefined) return '—';
    return `$${cost.toFixed(2)}`;
  }

  /** Check if a training job can be stopped. */
  canStopTraining(status: string): boolean {
    return status === 'PENDING' || status === 'TRAINING';
  }

  /** Check if an inference job can be stopped. */
  canStopInference(status: string): boolean {
    return status === 'PENDING' || status === 'TRANSFORMING';
  }

  /** Get elapsed time string for an active training job. */
  getElapsedTraining(job: JobResponse): string {
    if (!this.canStopTraining(job.status)) return '';
    return this.getElapsed(job.training_start_time, job.created_at);
  }

  /** Get elapsed time string for an active inference job. */
  getElapsedInference(job: InferenceJobResponse): string {
    if (!this.canStopInference(job.status)) return '';
    return this.getElapsed(job.transform_start_time, job.created_at);
  }

  /** Format seconds into a human-readable duration. */
  formatDuration(seconds: number): string {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;
    if (hours > 0) return `${hours}h ${minutes}m ${secs}s`;
    if (minutes > 0) return `${minutes}m ${secs}s`;
    return `${secs}s`;
  }

  /** Calculate elapsed time from a start timestamp to now. */
  private getElapsed(startTime: string | null, fallback: string): string {
    const start = startTime ?? fallback;
    const ms = this.now() - new Date(start).getTime();
    const totalSeconds = Math.max(0, Math.floor(ms / 1000));
    return this.formatDuration(totalSeconds);
  }

  /** Download the model artifact for a completed training job. */
  async downloadTrainingArtifact(jobId: string): Promise<void> {
    try {
      const response = await this.state.getTrainingDownloadUrl(jobId);
      window.open(response.download_url, '_blank');
    } catch {
      this.state.error.set('Failed to get download URL');
    }
  }

  /** Download inference results for a completed inference job. */
  async downloadInferenceResults(jobId: string): Promise<void> {
    try {
      const response = await this.state.getInferenceDownloadUrl(jobId);
      window.open(response.download_url, '_blank');
    } catch {
      this.state.error.set('Failed to get download URL');
    }
  }

  /** S3 retention period in days (matches lifecycle rule on fine-tuning-data bucket). */
  private readonly RETENTION_DAYS = 30;

  /** Get days remaining before S3 artifacts expire for a job. */
  getRetentionDaysRemaining(createdAt: string): number {
    const created = new Date(createdAt).getTime();
    const expiresAt = created + this.RETENTION_DAYS * 24 * 60 * 60 * 1000;
    return Math.max(0, Math.ceil((expiresAt - Date.now()) / (24 * 60 * 60 * 1000)));
  }

  /** Format retention as a human-readable string. */
  getRetentionLabel(createdAt: string): string {
    const days = this.getRetentionDaysRemaining(createdAt);
    if (days === 0) return 'Expired';
    return `${days}d remaining`;
  }
}
