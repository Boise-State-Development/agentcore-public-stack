import {
  Component,
  ChangeDetectionStrategy,
  input,
  output,
  computed,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCalendar, heroChevronDown } from '@ng-icons/heroicons/outline';

interface PeriodOption {
  value: string;
  label: string;
}

/**
 * Period selector component for selecting billing periods (month/year).
 * Displays a dropdown with the last 12 months.
 */
@Component({
  selector: 'app-period-selector',
  imports: [FormsModule, NgIcon],
  providers: [provideIcons({ heroCalendar, heroChevronDown })],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="relative inline-flex items-center">
      <ng-icon
        name="heroCalendar"
        class="size-5 text-gray-400 dark:text-gray-500 absolute left-3 pointer-events-none"
      />
      <select
        [ngModel]="selectedPeriod()"
        (ngModelChange)="onPeriodChange($event)"
        class="appearance-none bg-white dark:bg-gray-800 border border-gray-300 dark:border-gray-600 rounded-lg pl-10 pr-10 py-2 text-sm font-medium text-gray-700 dark:text-gray-200 focus:outline-none focus:ring-2 focus:ring-blue-500 focus:border-blue-500 cursor-pointer hover:bg-gray-50 dark:hover:bg-gray-700 transition-colors"
      >
        @for (option of periodOptions(); track option.value) {
          <option [value]="option.value">{{ option.label }}</option>
        }
      </select>
      <ng-icon
        name="heroChevronDown"
        class="size-4 text-gray-400 dark:text-gray-500 absolute right-3 pointer-events-none"
      />
    </div>
  `,
})
export class PeriodSelectorComponent {
  selectedPeriod = input.required<string>();
  periodChange = output<string>();

  isOpen = signal(false);

  periodOptions = computed<PeriodOption[]>(() => {
    const options: PeriodOption[] = [];

    // Add Demo Month option first
    options.push({ value: 'demo', label: 'Demo Month' });

    const now = new Date();

    // Generate last 12 months
    for (let i = 0; i < 12; i++) {
      const date = new Date(now.getFullYear(), now.getMonth() - i, 1);
      const value = `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}`;
      const label = date.toLocaleDateString('en-US', {
        month: 'long',
        year: 'numeric',
      });
      options.push({ value, label });
    }

    return options;
  });

  onPeriodChange(value: string): void {
    this.periodChange.emit(value);
  }
}
