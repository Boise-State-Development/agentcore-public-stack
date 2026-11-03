import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { utilizationLevel } from '../services/aws-limits-state.service';

const BAR: Record<string, string> = {
  ok: 'bg-state-success-500',
  warning: 'bg-state-warning-500',
  danger: 'bg-state-danger-600',
};
const TEXT: Record<string, string> = {
  ok: 'text-state-success-700 dark:text-state-success-300',
  warning: 'text-state-warning-800 dark:text-state-warning-300',
  danger: 'text-state-danger-700 dark:text-state-danger-300',
};

/** Usage against the applied value: green below 80%, amber to 95%, red at 95% and over, grey N/A without a metric. */
@Component({
  selector: 'app-utilization-bar',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DecimalPipe],
  host: { class: 'block' },
  template: `
    @if (level() === 'none') {
      <span class="text-sm/6 text-gray-500 dark:text-gray-400" title="AWS publishes no usage metric for this quota">N/A</span>
    } @else {
      <div class="flex items-center gap-2">
        <div
          class="h-2 w-24 overflow-hidden rounded-full bg-gray-200 dark:bg-white/10"
          role="meter"
          aria-valuemin="0"
          aria-valuemax="100"
          [attr.aria-valuenow]="clamped()"
          [attr.aria-label]="'Utilization ' + (utilization() | number: '1.0-1') + '%'"
        >
          <div class="h-full rounded-full" [class]="barClass()" [style.width.%]="clamped()"></div>
        </div>
        <span class="text-sm/6 font-medium tabular-nums" [class]="textClass()">{{ utilization() | number: '1.0-1' }}%</span>
      </div>
    }
  `,
})
export class UtilizationBarComponent {
  readonly utilization = input<number | null>(null);

  protected readonly level = computed(() => utilizationLevel(this.utilization()));
  protected readonly clamped = computed(() => Math.min(100, Math.max(0, this.utilization() ?? 0)));
  protected readonly barClass = computed(() => BAR[this.level()] ?? '');
  protected readonly textClass = computed(() => TEXT[this.level()] ?? '');
}
