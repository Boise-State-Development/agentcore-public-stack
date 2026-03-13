import { Component, ChangeDetectionStrategy, inject, signal, OnInit } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroPlus,
  heroPencilSquare,
  heroTrash,
  heroXMark,
  heroCheck,
  heroExclamationTriangle,
} from '@ng-icons/heroicons/outline';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';
import { FineTuningAdminStateService } from './services/fine-tuning-admin-state.service';

@Component({
  selector: 'app-fine-tuning-access-page',
  imports: [FormsModule, RouterLink, NgIcon, TooltipDirective],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroPlus,
      heroPencilSquare,
      heroTrash,
      heroXMark,
      heroCheck,
      heroExclamationTriangle,
    }),
  ],
  templateUrl: './fine-tuning-access.page.html',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block' },
})
export class FineTuningAccessPage implements OnInit {
  readonly state = inject(FineTuningAdminStateService);

  /** New-grant form fields */
  newEmail = '';
  newQuota = 10;

  /** Inline-edit state */
  editingEmail = signal<string | null>(null);
  editQuota = signal(0);

  /** Confirmation state for revoke */
  confirmingRevoke = signal<string | null>(null);

  ngOnInit(): void {
    this.state.loadGrants();
  }

  submitGrant(): void {
    const email = this.newEmail.trim().toLowerCase();
    if (!email) return;
    this.state.grantAccess(email, this.newQuota);
    this.newEmail = '';
    this.newQuota = 10;
  }

  startEdit(email: string, currentQuota: number): void {
    this.editingEmail.set(email);
    this.editQuota.set(currentQuota);
    this.confirmingRevoke.set(null);
  }

  cancelEdit(): void {
    this.editingEmail.set(null);
  }

  submitQuotaUpdate(): void {
    const email = this.editingEmail();
    if (!email) return;
    this.state.updateQuota(email, this.editQuota());
    this.editingEmail.set(null);
  }

  confirmRevoke(email: string): void {
    this.confirmingRevoke.set(email);
    this.editingEmail.set(null);
  }

  executeRevoke(email: string): void {
    this.state.revokeAccess(email);
    this.confirmingRevoke.set(null);
  }

  cancelRevoke(): void {
    this.confirmingRevoke.set(null);
  }

  formatDate(iso: string): string {
    const d = new Date(iso);
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  }

  usagePercent(usage: number, quota: number): number {
    return quota > 0 ? Math.min(100, Math.round((usage / quota) * 100)) : 0;
  }

  usageBarColor(usage: number, quota: number): string {
    const pct = this.usagePercent(usage, quota);
    if (pct >= 90) return 'bg-red-500';
    if (pct >= 70) return 'bg-amber-500';
    return 'bg-blue-500';
  }
}
