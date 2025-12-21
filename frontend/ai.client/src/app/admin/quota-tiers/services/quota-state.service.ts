import { Injectable, inject, signal, computed } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { QuotaHttpService } from './quota-http.service';
import {
  QuotaTier,
  QuotaAssignment,
  QuotaOverride,
  QuotaEvent,
} from '../models/quota.models';

/**
 * State management service for quota admin using signals.
 * Provides reactive state for tiers, assignments, overrides, and events.
 */
@Injectable({
  providedIn: 'root',
})
export class QuotaStateService {
  private http = inject(QuotaHttpService);

  // ========== State Signals ==========

  tiers = signal<QuotaTier[]>([]);
  selectedTier = signal<QuotaTier | null>(null);
  loadingTiers = signal(false);

  assignments = signal<QuotaAssignment[]>([]);
  selectedAssignment = signal<QuotaAssignment | null>(null);
  loadingAssignments = signal(false);

  overrides = signal<QuotaOverride[]>([]);
  selectedOverride = signal<QuotaOverride | null>(null);
  loadingOverrides = signal(false);

  events = signal<QuotaEvent[]>([]);
  loadingEvents = signal(false);

  error = signal<string | null>(null);

  // ========== Computed Signals ==========

  enabledTiers = computed(() =>
    this.tiers().filter((tier) => tier.enabled)
  );

  tiersCount = computed(() => this.tiers().length);

  assignmentsCount = computed(() => this.assignments().length);

  overridesCount = computed(() => this.overrides().length);

  activeOverrides = computed(() => {
    const now = new Date().toISOString();
    return this.overrides().filter(
      (override) =>
        override.enabled &&
        override.validFrom <= now &&
        override.validUntil >= now
    );
  });

  // ========== Tier Methods ==========

  async loadTiers(enabledOnly = false): Promise<void> {
    this.loadingTiers.set(true);
    this.error.set(null);

    try {
      const tiers = await this.http.getTiers(enabledOnly).toPromise();
      this.tiers.set(tiers || []);
    } catch (error: any) {
      this.error.set(error.message || 'Failed to load tiers');
      throw error;
    } finally {
      this.loadingTiers.set(false);
    }
  }

  async selectTier(tierId: string): Promise<void> {
    try {
      const tier = await this.http.getTier(tierId).toPromise();
      this.selectedTier.set(tier || null);
    } catch (error: any) {
      this.error.set(error.message || 'Failed to load tier');
      throw error;
    }
  }

  async createTier(tierData: any): Promise<QuotaTier> {
    try {
      const tier = await this.http.createTier(tierData).toPromise();
      if (tier) {
        this.tiers.update((tiers) => [...tiers, tier]);
        return tier;
      }
      throw new Error('Failed to create tier');
    } catch (error: any) {
      this.error.set(error.message || 'Failed to create tier');
      throw error;
    }
  }

  async updateTier(tierId: string, updates: any): Promise<QuotaTier> {
    try {
      const updated = await this.http.updateTier(tierId, updates).toPromise();
      if (updated) {
        this.tiers.update((tiers) =>
          tiers.map((t) => (t.tierId === tierId ? updated : t))
        );
        if (this.selectedTier()?.tierId === tierId) {
          this.selectedTier.set(updated);
        }
        return updated;
      }
      throw new Error('Failed to update tier');
    } catch (error: any) {
      this.error.set(error.message || 'Failed to update tier');
      throw error;
    }
  }

  async deleteTier(tierId: string): Promise<void> {
    try {
      await this.http.deleteTier(tierId).toPromise();
      this.tiers.update((tiers) => tiers.filter((t) => t.tierId !== tierId));
      if (this.selectedTier()?.tierId === tierId) {
        this.selectedTier.set(null);
      }
    } catch (error: any) {
      this.error.set(error.message || 'Failed to delete tier');
      throw error;
    }
  }

  // ========== Assignment Methods ==========

  async loadAssignments(tierId?: string, enabledOnly = false): Promise<void> {
    this.loadingAssignments.set(true);
    this.error.set(null);

    try {
      const assignments = await this.http
        .getAssignments(tierId, enabledOnly)
        .toPromise();
      this.assignments.set(assignments || []);
    } catch (error: any) {
      this.error.set(error.message || 'Failed to load assignments');
      throw error;
    } finally {
      this.loadingAssignments.set(false);
    }
  }

  async createAssignment(assignmentData: any): Promise<QuotaAssignment> {
    try {
      const assignment = await this.http
        .createAssignment(assignmentData)
        .toPromise();
      if (assignment) {
        this.assignments.update((assignments) => [...assignments, assignment]);
        return assignment;
      }
      throw new Error('Failed to create assignment');
    } catch (error: any) {
      this.error.set(error.message || 'Failed to create assignment');
      throw error;
    }
  }

  async updateAssignment(
    assignmentId: string,
    updates: any
  ): Promise<QuotaAssignment> {
    try {
      const updated = await this.http
        .updateAssignment(assignmentId, updates)
        .toPromise();
      if (updated) {
        this.assignments.update((assignments) =>
          assignments.map((a) =>
            a.assignmentId === assignmentId ? updated : a
          )
        );
        return updated;
      }
      throw new Error('Failed to update assignment');
    } catch (error: any) {
      this.error.set(error.message || 'Failed to update assignment');
      throw error;
    }
  }

  async deleteAssignment(assignmentId: string): Promise<void> {
    try {
      await this.http.deleteAssignment(assignmentId).toPromise();
      this.assignments.update((assignments) =>
        assignments.filter((a) => a.assignmentId !== assignmentId)
      );
    } catch (error: any) {
      this.error.set(error.message || 'Failed to delete assignment');
      throw error;
    }
  }

  // ========== Override Methods ==========

  async loadOverrides(userId?: string, activeOnly = false): Promise<void> {
    this.loadingOverrides.set(true);
    this.error.set(null);

    try {
      const overrides = await this.http
        .getOverrides(userId, activeOnly)
        .toPromise();
      this.overrides.set(overrides || []);
    } catch (error: any) {
      this.error.set(error.message || 'Failed to load overrides');
      throw error;
    } finally {
      this.loadingOverrides.set(false);
    }
  }

  async createOverride(overrideData: any): Promise<QuotaOverride> {
    try {
      const override = await this.http.createOverride(overrideData).toPromise();
      if (override) {
        this.overrides.update((overrides) => [...overrides, override]);
        return override;
      }
      throw new Error('Failed to create override');
    } catch (error: any) {
      this.error.set(error.message || 'Failed to create override');
      throw error;
    }
  }

  async updateOverride(
    overrideId: string,
    updates: any
  ): Promise<QuotaOverride> {
    try {
      const updated = await this.http
        .updateOverride(overrideId, updates)
        .toPromise();
      if (updated) {
        this.overrides.update((overrides) =>
          overrides.map((o) => (o.overrideId === overrideId ? updated : o))
        );
        return updated;
      }
      throw new Error('Failed to update override');
    } catch (error: any) {
      this.error.set(error.message || 'Failed to update override');
      throw error;
    }
  }

  async deleteOverride(overrideId: string): Promise<void> {
    try {
      await this.http.deleteOverride(overrideId).toPromise();
      this.overrides.update((overrides) =>
        overrides.filter((o) => o.overrideId !== overrideId)
      );
    } catch (error: any) {
      this.error.set(error.message || 'Failed to delete override');
      throw error;
    }
  }

  // ========== Event Methods ==========

  async loadEvents(options: {
    userId?: string;
    tierId?: string;
    eventType?: string;
    limit?: number;
  }): Promise<void> {
    this.loadingEvents.set(true);
    this.error.set(null);

    try {
      const events = await this.http.getEvents(options).toPromise();
      this.events.set(events || []);
    } catch (error: any) {
      this.error.set(error.message || 'Failed to load events');
      throw error;
    } finally {
      this.loadingEvents.set(false);
    }
  }

  // ========== Utility Methods ==========

  clearError(): void {
    this.error.set(null);
  }

  reset(): void {
    this.tiers.set([]);
    this.selectedTier.set(null);
    this.assignments.set([]);
    this.selectedAssignment.set(null);
    this.overrides.set([]);
    this.selectedOverride.set(null);
    this.events.set([]);
    this.error.set(null);
  }
}
