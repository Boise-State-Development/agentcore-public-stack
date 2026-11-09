import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  computed,
  effect,
  inject,
  input,
  viewChild,
} from '@angular/core';
import { Chart, ChartConfiguration } from 'chart.js/auto';
import { TrendChartBand, versionBandsPlugin } from '../../costs/components/cost-trends-chart.component';
import { getChromeColorsForMode } from '../../../shared/constants/chart-colors.constants';

/** One line: a value per run (`null` where the run has none), and a note per run for the tooltip. */
export interface EvalLineSeries {
  label: string;
  color: string;
  values: readonly (number | null)[];
  notes?: readonly (string | null)[];
  /** A reference line (the baseline): dashed, no points. */
  reference?: boolean;
}

export type EvalLineFormat = 'count' | 'score';

/**
 * A small line chart with one category per run (eval-harness spec §16.3,
 * items 3 and 5): the family trend small multiples, with the baseline as a
 * dashed reference, and the judge means per evaluator.
 *
 * The version bands are the Releases tab's, drawn by the same plugin as
 * `app-cost-trends-chart` (`versionBandsPlugin`), so a build reads the same on
 * both pages. One model per chart (D22): the page never passes two.
 */
@Component({
  selector: 'app-eval-line-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <figure class="rounded-2xl border border-gray-200 bg-white p-4 shadow-xs dark:border-gray-700 dark:bg-gray-800">
      <figcaption class="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span class="font-mono text-sm/6 font-semibold text-gray-900 dark:text-white">{{ heading() }}</span>
        @if (subheading()) {
          <span class="text-xs/5 tabular-nums" [class]="subheadingTone()">{{ subheading() }}</span>
        }
      </figcaption>
      <ul role="list" class="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs/5 text-gray-600 dark:text-gray-400">
        @for (s of series(); track s.label) {
          <li class="flex items-center gap-1.5">
            <!-- intentional: legend swatches mirror the Chart.js dataset colours -->
            <span
              class="inline-block h-0.5 w-4"
              [class.border-t-2]="s.reference"
              [class.border-dashed]="s.reference"
              [style.border-color]="s.reference ? s.color : null"
              [style.background-color]="s.reference ? null : s.color"
              aria-hidden="true"
            ></span>
            {{ s.label }}
          </li>
        }
      </ul>
      @if (labels().length === 0) {
        <p class="mt-3 flex h-40 items-center justify-center rounded-xl border-2 border-dashed border-gray-200 text-sm/6 text-gray-500 dark:border-gray-700 dark:text-gray-400">
          {{ emptyText() }}
        </p>
      } @else {
        <div class="mt-2" [class]="compact() ? 'h-40' : 'h-56'">
          <canvas #canvas role="img" [attr.aria-label]="ariaLabel()"></canvas>
        </div>
      }
    </figure>
  `,
})
export class EvalLineChartComponent {
  heading = input.required<string>();
  subheading = input('');
  /** Amber for a family under the D18 floor. */
  subheadingWarn = input(false);
  /** One label per run, oldest first. */
  labels = input.required<readonly string[]>();
  /** One `YYYY-MM-DD` per run, for the bands. */
  dates = input.required<readonly string[]>();
  series = input.required<readonly EvalLineSeries[]>();
  bands = input<readonly TrendChartBand[]>([]);
  format = input<EvalLineFormat>('count');
  /** Upper bound of the y axis (a family's `n`; 1 for a judge mean). */
  yMax = input<number | null>(null);
  compact = input(true);
  emptyText = input('No runs in this range');

  protected readonly subheadingTone = computed(() =>
    this.subheadingWarn()
      ? 'font-medium text-state-warning-800 dark:text-state-warning-200'
      : 'text-gray-500 dark:text-gray-400',
  );

  protected readonly ariaLabel = computed(() => {
    const labels = this.labels();
    if (!labels.length) return '';
    const lines = this.series()
      .map(s => {
        const last = [...s.values].reverse().find(v => v !== null);
        return `${s.label} latest ${last === undefined || last === null ? 'none' : this.fmt(last)}`;
      })
      .join('; ');
    return `${this.heading()} over ${labels.length} runs, ${labels[0]} to ${labels[labels.length - 1]}: ${lines}`;
  });

  private readonly canvas = viewChild<ElementRef<HTMLCanvasElement>>('canvas');
  private chart: Chart | null = null;

  constructor() {
    inject(DestroyRef).onDestroy(() => {
      this.chart?.destroy();
      this.chart = null;
    });
    effect(() => {
      const canvas = this.canvas();
      const labels = this.labels();
      const dates = this.dates();
      const series = this.series();
      const bands = this.bands();
      const yMax = this.yMax();
      const format = this.format();
      if (canvas && labels.length) this.render(canvas.nativeElement, labels, dates, series, bands, yMax, format);
    });
  }

  private fmt(value: number): string {
    return this.format() === 'score' ? value.toFixed(2) : String(value);
  }

  private render(
    canvas: HTMLCanvasElement,
    labels: readonly string[],
    dates: readonly string[],
    series: readonly EvalLineSeries[],
    bands: readonly TrendChartBand[],
    yMax: number | null,
    format: EvalLineFormat,
  ): void {
    this.chart?.destroy();
    const dark = document.documentElement.classList.contains('dark');
    const chrome = getChromeColorsForMode(dark);
    const points = dates.map(date => ({ date }));
    const config: ChartConfiguration<'line'> = {
      type: 'line',
      data: {
        labels: [...labels],
        datasets: series.map(s => ({
          label: s.label,
          data: [...s.values],
          borderColor: s.color,
          backgroundColor: s.color,
          borderDash: s.reference ? [5, 4] : undefined,
          borderWidth: s.reference ? 1.5 : 2,
          pointRadius: s.reference ? 0 : 3,
          pointHoverRadius: s.reference ? 0 : 5,
          spanGaps: false,
          tension: 0,
          stepped: s.reference ? ('middle' as const) : false,
        })),
      },
      plugins: bands.length ? [versionBandsPlugin(points, bands, dark)] : [],
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: chrome.background,
            titleColor: chrome.titleText,
            bodyColor: chrome.bodyText,
            borderColor: chrome.border,
            borderWidth: 1,
            padding: 10,
            callbacks: {
              label: ctx => {
                const s = series[ctx.datasetIndex];
                const v = ctx.parsed.y;
                const note = s?.notes?.[ctx.dataIndex];
                const value = v === null || v === undefined ? 'not run' : this.fmt(v);
                return `${s?.label ?? ''}: ${value}${note ? ` (${note})` : ''}`;
              },
            },
          },
        },
        scales: {
          x: {
            grid: { color: chrome.gridLine },
            ticks: { color: chrome.axisText, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
          },
          y: {
            min: 0,
            max: yMax ?? undefined,
            grid: { color: chrome.gridLine },
            ticks: {
              color: chrome.axisText,
              precision: format === 'count' ? 0 : undefined,
              maxTicksLimit: 5,
            },
          },
        },
      },
    };
    this.chart = new Chart(canvas, config);
  }
}
