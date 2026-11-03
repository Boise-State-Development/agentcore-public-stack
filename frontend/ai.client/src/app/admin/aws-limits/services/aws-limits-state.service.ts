import { Injectable, computed, inject, signal } from '@angular/core';
import {
  AwsLimit,
  AwsLimitGroup,
  AwsLimitsDashboard,
  DANGER_THRESHOLD,
  IncreaseRequestRecord,
  WARN_THRESHOLD,
} from '../models';
import { AwsLimitsHttpService, awsLimitsErrorMessage } from './aws-limits-http.service';

export interface AwsLimitFilters {
  /** A group id, or '' for every group. */
  group: string;
  adjustableOnly: boolean;
  nearLimitOnly: boolean;
}

/** Highest utilization first; rows without usage last, then by name. */
export function sortByUtilization(limits: readonly AwsLimit[]): AwsLimit[] {
  return [...limits].sort((a, b) => {
    const ua = a.utilization ?? -1;
    const ub = b.utilization ?? -1;
    if (ua !== ub) return ub - ua;
    return a.quotaName.localeCompare(b.quotaName);
  });
}

export type UtilizationLevel = 'ok' | 'warning' | 'danger' | 'none';

export function utilizationLevel(utilization: number | null | undefined): UtilizationLevel {
  if (utilization === null || utilization === undefined) return 'none';
  if (utilization >= DANGER_THRESHOLD) return 'danger';
  if (utilization >= WARN_THRESHOLD) return 'warning';
  return 'ok';
}

/** Signal state for the AWS Limits page. */
@Injectable({ providedIn: 'root' })
export class AwsLimitsStateService {
  private http = inject(AwsLimitsHttpService);

  readonly dashboard = signal<AwsLimitsDashboard | null>(null);
  readonly history = signal<IncreaseRequestRecord[]>([]);
  readonly loading = signal(false);
  readonly historyLoading = signal(false);
  readonly error = signal<string | null>(null);
  readonly historyError = signal<string | null>(null);
  readonly showAllBedrock = signal(false);
  readonly filters = signal<AwsLimitFilters>({ group: '', adjustableOnly: false, nearLimitOnly: false });

  readonly canRequestIncrease = computed(() => this.dashboard()?.canRequestIncrease ?? false);
  readonly summary = computed(() => this.dashboard()?.summary ?? null);

  /** The groups after filtering, each sorted by utilization. Doc-link-only groups stay unless filtered out. */
  readonly visibleGroups = computed<AwsLimitGroup[]>(() => {
    const dashboard = this.dashboard();
    if (!dashboard) return [];
    const { group, adjustableOnly, nearLimitOnly } = this.filters();
    const rowFilter = adjustableOnly || nearLimitOnly;
    return dashboard.groups
      .filter(g => !group || g.id === group)
      .map(g => ({
        ...g,
        limits: sortByUtilization(
          g.limits.filter(
            l => (!adjustableOnly || l.adjustable) && (!nearLimitOnly || (l.utilization ?? 0) >= WARN_THRESHOLD),
          ),
        ),
      }))
      .filter(g => !rowFilter || g.limits.length > 0 || !!g.error);
  });

  async load(options: { refresh?: boolean } = {}): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      this.dashboard.set(
        await this.http.getDashboard({ refresh: options.refresh, allBedrock: this.showAllBedrock() }),
      );
    } catch (error) {
      this.error.set(awsLimitsErrorMessage(error, 'AWS limits could not be loaded.'));
    } finally {
      this.loading.set(false);
    }
  }

  async loadHistory(refresh = false): Promise<void> {
    this.historyLoading.set(true);
    this.historyError.set(null);
    try {
      this.history.set((await this.http.getHistory(refresh)).requests);
    } catch (error) {
      this.historyError.set(awsLimitsErrorMessage(error, 'The increase history could not be loaded.'));
    } finally {
      this.historyLoading.set(false);
    }
  }

  /** Re-read both from AWS, bypassing the server's cache. */
  async refresh(): Promise<void> {
    await Promise.all([this.load({ refresh: true }), this.loadHistory(true)]);
  }

  async setShowAllBedrock(value: boolean): Promise<void> {
    if (this.showAllBedrock() === value) return;
    this.showAllBedrock.set(value);
    await this.load();
  }

  setFilters(patch: Partial<AwsLimitFilters>): void {
    this.filters.update(f => ({ ...f, ...patch }));
  }

  /** After a successful request: show it at once, then let the server's view catch up. */
  async recordSubmitted(record: IncreaseRequestRecord): Promise<void> {
    this.history.update(list => [record, ...list.filter(r => r.requestId !== record.requestId)]);
    await Promise.all([this.loadHistory(), this.load()]);
  }
}
