import { Injectable, inject, signal, computed } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { FineTuningAdminHttpService } from './fine-tuning-admin-http.service';
import { FineTuningGrant } from '../models/fine-tuning-access.models';

/**
 * State service for admin fine-tuning access management.
 * Manages grants list, loading/error state, and CRUD operations.
 */
@Injectable({
  providedIn: 'root',
})
export class FineTuningAdminStateService {
  private http = inject(FineTuningAdminHttpService);

  /** All access grants. */
  readonly grants = signal<FineTuningGrant[]>([]);

  /** Whether a network request is in progress. */
  readonly loading = signal(false);

  /** Last error message (null when clear). */
  readonly error = signal<string | null>(null);

  /** Whether the inline grant form is visible. */
  readonly showGrantForm = signal(false);

  /** Total number of grants. */
  readonly grantCount = computed(() => this.grants().length);

  /** Whether an error is present. */
  readonly hasError = computed(() => this.error() !== null);

  /** Fetch all access grants from the backend. */
  async loadGrants(): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const response = await firstValueFrom(this.http.listGrants());
      this.grants.set(response.grants);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to load access grants';
      this.error.set(message);
    } finally {
      this.loading.set(false);
    }
  }

  /** Grant fine-tuning access and refresh the list. */
  async grantAccess(email: string, monthlyQuotaHours: number): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      await firstValueFrom(this.http.grantAccess(email, monthlyQuotaHours));
      await this.loadGrants();
      this.showGrantForm.set(false);
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to grant access';
      this.error.set(message);
      this.loading.set(false);
    }
  }

  /** Update the monthly quota for a user and refresh the list. */
  async updateQuota(email: string, monthlyQuotaHours: number): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      await firstValueFrom(this.http.updateQuota(email, monthlyQuotaHours));
      await this.loadGrants();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to update quota';
      this.error.set(message);
      this.loading.set(false);
    }
  }

  /** Revoke access for a user and refresh the list. */
  async revokeAccess(email: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      await firstValueFrom(this.http.revokeAccess(email));
      await this.loadGrants();
    } catch (err: unknown) {
      const message = err instanceof Error ? err.message : 'Failed to revoke access';
      this.error.set(message);
      this.loading.set(false);
    }
  }

  /** Toggle visibility of the inline grant form. */
  toggleGrantForm(): void {
    this.showGrantForm.update(v => !v);
  }

  /** Clear the current error. */
  clearError(): void {
    this.error.set(null);
  }
}
