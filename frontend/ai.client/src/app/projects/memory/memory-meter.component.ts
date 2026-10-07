import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { DecimalPipe } from '@angular/common';

/**
 * How full a memory file or index is against its limit: a slim bar and, unless
 * `compact`, "1,234 / 8,000 tokens" beside it. Amber from `soft`, red at `max`.
 *
 * A `meter` with its value as text, so a screen reader hears the numbers the bar draws.
 * `tokens` null (an entry saved before sizes were counted) renders "Size unknown".
 */
@Component({
  selector: 'app-memory-meter',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DecimalPipe],
  host: { class: 'inline-flex items-center gap-2' },
  template: `
    @if (tokens() === null) {
      <span class="text-xs/5 text-gray-600 dark:text-gray-400">Size unknown</span>
    } @else {
      <span
        role="meter"
        [attr.aria-label]="label()"
        aria-valuemin="0"
        [attr.aria-valuemax]="max()"
        [attr.aria-valuenow]="clamped()"
        [attr.aria-valuetext]="valueText()"
        class="block h-1.5 shrink-0 overflow-hidden rounded-full bg-gray-200 dark:bg-gray-700"
        [class]="compact() ? 'w-14' : 'w-20'"
      >
        <span class="block h-full rounded-full" [class]="fill()" [style.width.%]="percent()"></span>
      </span>
      @if (!compact()) {
        <span class="text-xs/5 text-gray-600 tabular-nums dark:text-gray-400" aria-hidden="true">
          {{ tokens() | number }} / {{ max() | number }} tokens
        </span>
      }
    }
  `,
})
export class MemoryMeterComponent {
  readonly tokens = input.required<number | null>();
  readonly max = input.required<number>();
  /** Where "close to the limit" starts; defaults to 75% of `max`. */
  readonly soft = input<number | null>(null);
  readonly label = input('Size');
  readonly compact = input(false);

  protected readonly clamped = computed(() => Math.min(this.tokens() ?? 0, this.max()));
  protected readonly percent = computed(() => (this.max() > 0 ? Math.round((this.clamped() / this.max()) * 100) : 0));
  protected readonly valueText = computed(() => {
    const tokens = (this.tokens() ?? 0).toLocaleString('en-US');
    return `${tokens} of ${this.max().toLocaleString('en-US')} tokens${this.state() === 'over' ? ', at the limit' : this.state() === 'near' ? ', close to the limit' : ''}`;
  });
  private readonly state = computed(() => {
    const tokens = this.tokens() ?? 0;
    if (tokens >= this.max()) return 'over';
    return tokens >= (this.soft() ?? this.max() * 0.75) ? 'near' : 'ok';
  });
  protected readonly fill = computed(() => {
    switch (this.state()) {
      case 'over':
        return 'bg-state-danger-600 dark:bg-state-danger-500';
      case 'near':
        return 'bg-state-warning-500';
      default:
        return 'bg-primary-accessible dark:bg-primary-400';
    }
  });
}
