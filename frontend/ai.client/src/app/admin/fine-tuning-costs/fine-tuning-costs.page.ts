import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  OnInit,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroCurrencyDollar,
  heroClock,
  heroUsers,
  heroCpuChip,
  heroChevronDown,
  heroChevronUp,
  heroCalendar,
} from '@ng-icons/heroicons/outline';
import { FineTuningAdminStateService } from '../fine-tuning-access/services/fine-tuning-admin-state.service';
import { UserCostBreakdown } from '../fine-tuning-access/models/fine-tuning-access.models';

type SortField = 'email' | 'total_cost_usd' | 'total_gpu_hours' | 'training_job_count' | 'inference_job_count';

@Component({
  selector: 'app-fine-tuning-costs-page',
  imports: [RouterLink, FormsModule, NgIcon],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroCurrencyDollar,
      heroClock,
      heroUsers,
      heroCpuChip,
      heroChevronDown,
      heroChevronUp,
      heroCalendar,
    }),
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './fine-tuning-costs.page.html',
  host: { class: 'block' },
})
export class FineTuningCostsPage implements OnInit {
  readonly state = inject(FineTuningAdminStateService);

  readonly sortField = signal<SortField>('total_cost_usd');
  readonly sortAsc = signal(false);

  readonly dashboard = this.state.costDashboard;
  readonly loading = this.state.costLoading;
  readonly error = this.state.error;
  readonly selectedPeriod = this.state.costPeriod;

  readonly periodOptions = computed(() => {
    const options: { value: string; label: string }[] = [];
    const now = new Date();
    for (let i = 0; i < 12; i++) {
      const date = new Date(now.getFullYear(), now.getMonth() - i, 1);
      const value = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`;
      const label = date.toLocaleDateString('en-US', { month: 'long', year: 'numeric' });
      options.push({ value, label });
    }
    return options;
  });

  readonly sortedUsers = computed(() => {
    const data = this.dashboard();
    if (!data) return [];
    const users = [...data.users];
    const field = this.sortField();
    const asc = this.sortAsc();
    users.sort((a, b) => {
      const aVal = a[field];
      const bVal = b[field];
      if (typeof aVal === 'string' && typeof bVal === 'string') {
        return asc ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
      }
      return asc ? (aVal as number) - (bVal as number) : (bVal as number) - (aVal as number);
    });
    return users;
  });

  readonly avgCostPerUser = computed(() => {
    const data = this.dashboard();
    if (!data || data.active_user_count === 0) return 0;
    return data.total_cost_usd / data.active_user_count;
  });

  ngOnInit(): void {
    this.state.loadCostDashboard();
  }

  onPeriodChange(period: string): void {
    this.state.setCostPeriod(period);
  }

  toggleSort(field: SortField): void {
    if (this.sortField() === field) {
      this.sortAsc.update(v => !v);
    } else {
      this.sortField.set(field);
      this.sortAsc.set(field === 'email');
    }
  }

  isSortedBy(field: SortField): boolean {
    return this.sortField() === field;
  }

  formatCurrency(value: number): string {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(value);
  }

  formatHours(value: number): string {
    return new Intl.NumberFormat('en-US', {
      minimumFractionDigits: 1,
      maximumFractionDigits: 1,
    }).format(value);
  }

  costBarWidth(user: UserCostBreakdown): number {
    const data = this.dashboard();
    if (!data || data.total_cost_usd === 0) return 0;
    return Math.max(2, Math.round((user.total_cost_usd / data.total_cost_usd) * 100));
  }
}
