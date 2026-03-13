import { Component, ChangeDetectionStrategy, inject, OnInit, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { DatePipe } from '@angular/common';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroPlus,
  heroArrowPath,
  heroExclamationTriangle,
  heroXMark,
  heroStop,
  heroLockClosed,
} from '@ng-icons/heroicons/outline';
import { TooltipDirective } from '../../../components/tooltip/tooltip.directive';
import { FineTuningStateService } from '../../services/fine-tuning-state.service';
import { StatusBadgeComponent } from '../../components/status-badge.component';
import { QuotaCardComponent } from '../../components/quota-card.component';

@Component({
  selector: 'app-fine-tuning-dashboard',
  imports: [RouterLink, DatePipe, NgIcon, TooltipDirective, StatusBadgeComponent, QuotaCardComponent],
  providers: [
    provideIcons({
      heroPlus,
      heroArrowPath,
      heroExclamationTriangle,
      heroXMark,
      heroStop,
      heroLockClosed,
    }),
  ],
  templateUrl: './fine-tuning-dashboard.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block' },
})
export class FineTuningDashboardPage implements OnInit {
  readonly state = inject(FineTuningStateService);
  private readonly router = inject(Router);

  /** Job ID currently showing stop confirmation for training jobs. */
  readonly confirmingStopTraining = signal<string | null>(null);

  /** Job ID currently showing stop confirmation for inference jobs. */
  readonly confirmingStopInference = signal<string | null>(null);

  ngOnInit(): void {
    this.state.loadDashboard();
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
}
