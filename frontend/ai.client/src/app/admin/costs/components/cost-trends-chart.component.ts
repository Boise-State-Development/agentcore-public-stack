import {
  Component,
  ChangeDetectionStrategy,
  DestroyRef,
  input,
  computed,
  effect,
  inject,
  viewChild,
  ElementRef,
} from '@angular/core';
import { Chart, ChartConfiguration, ChartData, Plugin } from 'chart.js/auto';
import { CostTrend } from '../models';
import {
  CHART_SERIES_COLORS,
  CHART_FILL_COLORS,
  getChromeColorsForMode,
} from '../../../shared/constants/chart-colors.constants';

/**
 * A day of the chart: the primary value (a gap when `null`) and the request count.
 * `CostTrend` is one; the Releases tab passes a ledger KPI as `totalCost`.
 */
export type TrendChartPoint = Pick<CostTrend, 'date' | 'totalRequests'> & {
  totalCost: number | null;
};

/** A shaded span behind the lines: one version or build, its first to last date. */
export interface TrendChartBand {
  from: string;
  to: string;
  label: string;
}

/** How the primary (left-axis) series is printed. */
export type TrendChartFormat = 'currency' | 'usd-precise' | 'percent' | 'ms';

/**
 * Cost trends line chart component.
 *
 * Displays daily cost and request trends over the selected period. The card
 * takes the console's full content width and the chart is sized off that: a
 * month of daily points in a half-width box put the date axis on a 45-degree
 * rotation and still dropped labels, which is the one thing a trend chart
 * cannot afford to lose.
 *
 * The Releases tab reuses it (release-ledger spec §6.4 item 4) with `bands`,
 * which shade each version's days behind the lines, and a primary series of
 * its choosing. Every new input defaults to the Cost Trends tab's behaviour.
 */
@Component({
  selector: 'app-cost-trends-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="rounded-2xl border border-gray-200 bg-white shadow-xs dark:border-gray-700 dark:bg-gray-800"
    >
      <div
        class="flex flex-col gap-3 border-b border-gray-200 px-6 py-4 sm:flex-row sm:items-center sm:justify-between dark:border-gray-700"
      >
        <div>
          <h3 class="text-lg/7 font-semibold text-gray-900 dark:text-white">
            {{ heading() }}
          </h3>
          <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
            {{ description() }}
          </p>
        </div>
        <!-- intentional: legend dots mirror the Chart.js dataset borderColor
             values from CHART_SERIES_COLORS constants (#3b82f6/#10b981), which are
             exempt from token migration per identity.css's chart-series note -->
        <div class="flex shrink-0 items-center gap-4 text-sm/6">
          <div class="flex items-center gap-2">
            <span class="size-3 rounded-full" [style.background-color]="CHART_SERIES_COLORS.cost"></span>
            <span class="text-gray-600 dark:text-gray-400">{{ primaryLabel() }}</span>
          </div>
          <div class="flex items-center gap-2">
            <span class="size-3 rounded-full" [style.background-color]="CHART_SERIES_COLORS.requests"></span>
            <span class="text-gray-600 dark:text-gray-400">{{ secondaryLabel() }}</span>
          </div>
          @if (bands().length > 0) {
            <div class="flex items-center gap-2">
              <span class="size-3 rounded-xs bg-gray-300 dark:bg-gray-600"></span>
              <span class="text-gray-600 dark:text-gray-400">Version</span>
            </div>
          }
        </div>
      </div>

      <div class="p-6">
        @if (data().length === 0) {
          <div
            class="flex h-64 items-center justify-center rounded-2xl border-2 border-dashed border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-900/50"
          >
            <p class="text-sm text-gray-500 dark:text-gray-400">
              {{ emptyText() }}
            </p>
          </div>
        } @else {
          <div class="h-72 sm:h-80 lg:h-[26rem]">
            <canvas #chartCanvas role="img" [attr.aria-label]="ariaLabel()"></canvas>
          </div>
        }
      </div>

      <!-- Summary stats below chart -->
      @if (data().length > 0 && showSummary()) {
        <dl
          class="grid grid-cols-2 divide-y divide-gray-200 border-t border-gray-200 sm:grid-cols-4 sm:divide-y-0 sm:divide-x dark:divide-gray-700 dark:border-gray-700"
        >
          <div class="px-6 py-4">
            <dt class="text-sm/6 text-gray-500 dark:text-gray-400">Peak Cost</dt>
            <dd class="mt-0.5 text-xl/7 font-semibold text-gray-900 tabular-nums dark:text-white">
              {{ formatCurrency(peakCost()) }}
            </dd>
          </div>
          <div class="px-6 py-4">
            <dt class="text-sm/6 text-gray-500 dark:text-gray-400">Avg Daily</dt>
            <dd class="mt-0.5 text-xl/7 font-semibold text-gray-900 tabular-nums dark:text-white">
              {{ formatCurrency(avgDailyCost()) }}
            </dd>
          </div>
          <div class="px-6 py-4">
            <dt class="text-sm/6 text-gray-500 dark:text-gray-400">Total Requests</dt>
            <dd class="mt-0.5 text-xl/7 font-semibold text-gray-900 tabular-nums dark:text-white">
              {{ formatNumber(totalRequests()) }}
            </dd>
          </div>
          <div class="px-6 py-4">
            <dt class="text-sm/6 text-gray-500 dark:text-gray-400">Total Days</dt>
            <dd class="mt-0.5 text-xl/7 font-semibold text-gray-900 tabular-nums dark:text-white">
              {{ data().length }}
            </dd>
          </div>
        </dl>
      }
    </div>
  `,
})
export class CostTrendsChartComponent {
  data = input.required<readonly TrendChartPoint[]>();
  heading = input('Cost Trends');
  description = input('Daily spend and request volume across the period.');
  primaryLabel = input('Cost');
  primaryFormat = input<TrendChartFormat>('currency');
  secondaryLabel = input('Requests');
  emptyText = input('No trend data available for this period');
  /** The four-number strip under the chart. Off where the primary is a rate (a "peak" share is noise). */
  showSummary = input(true);
  bands = input<readonly TrendChartBand[]>([]);

  protected readonly ariaLabel = computed(() => {
    const points = this.data();
    if (points.length === 0) return '';
    const span = `${points[0].date} to ${points[points.length - 1].date}`;
    const bands = this.bands();
    const shaded = bands.length
      ? `; shaded by version: ${bands.map(b => `${b.label} ${b.from} to ${b.to}`).join(', ')}`
      : '';
    return `Daily ${this.primaryLabel()} and ${this.secondaryLabel()}, ${span}${shaded}`;
  });

  // Export constant for template access
  protected readonly CHART_SERIES_COLORS = CHART_SERIES_COLORS;

  private chartCanvas = viewChild<ElementRef<HTMLCanvasElement>>('chartCanvas');
  private chart: Chart | null = null;

  // Computed stats
  peakCost = computed(() => {
    const trends = this.data();
    if (trends.length === 0) return 0;
    return Math.max(...trends.map(t => t.totalCost ?? 0));
  });

  avgDailyCost = computed(() => {
    const trends = this.data();
    if (trends.length === 0) return 0;
    const total = trends.reduce((sum, t) => sum + (t.totalCost ?? 0), 0);
    return total / trends.length;
  });

  totalRequests = computed(() =>
    this.data().reduce((sum, t) => sum + t.totalRequests, 0)
  );

  constructor() {
    // The card is a tab panel now, so it is destroyed and rebuilt on every
    // switch away and back. Chart.js keeps its instances in a module-level
    // registry keyed by canvas, so without this each visit leaks one.
    inject(DestroyRef).onDestroy(() => {
      this.chart?.destroy();
      this.chart = null;
    });

    effect(() => {
      const canvas = this.chartCanvas();
      const trends = this.data();
      const bands = this.bands();
      const primary = { label: this.primaryLabel(), format: this.primaryFormat() };
      const secondaryLabel = this.secondaryLabel();

      if (canvas && trends.length > 0) {
        this.renderChart(canvas.nativeElement, trends, bands, primary, secondaryLabel);
      }
    });
  }

  private renderChart(
    canvas: HTMLCanvasElement,
    trends: readonly TrendChartPoint[],
    bands: readonly TrendChartBand[],
    primary: { label: string; format: TrendChartFormat },
    secondaryLabel: string,
  ): void {
    // Destroy existing chart if present
    if (this.chart) {
      this.chart.destroy();
    }

    const labels = trends.map(t => this.formatDate(t.date));
    const costData = trends.map(t => t.totalCost);
    const requestsData = trends.map(t => t.totalRequests);

    // Calculate max values for scaling
    const maxCost = Math.max(...costData.map(v => v ?? 0));
    const maxRequests = Math.max(...requestsData);

    const isDarkMode = document.documentElement.classList.contains('dark');
    const chromeColors = getChromeColorsForMode(isDarkMode);

    const chartData: ChartData<'line'> = {
      labels,
      datasets: [
        {
          label: primary.format === 'currency' ? 'Cost ($)' : primary.label,
          data: costData,
          spanGaps: false,
          borderColor: CHART_SERIES_COLORS.cost,
          backgroundColor: CHART_FILL_COLORS.cost,
          fill: true,
          tension: 0.3,
          yAxisID: 'y',
          pointRadius: 3,
          pointHoverRadius: 5,
        },
        {
          label: secondaryLabel,
          data: requestsData,
          borderColor: CHART_SERIES_COLORS.requests,
          backgroundColor: CHART_FILL_COLORS.requests,
          fill: false,
          tension: 0.3,
          yAxisID: 'y1',
          pointRadius: 3,
          pointHoverRadius: 5,
        },
      ],
    };

    const config: ChartConfiguration<'line'> = {
      type: 'line',
      data: chartData,
      plugins: bands.length ? [versionBandsPlugin(trends, bands, isDarkMode)] : [],
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
          mode: 'index',
          intersect: false,
        },
        plugins: {
          legend: {
            display: false,
          },
          tooltip: {
            backgroundColor: chromeColors.background,
            titleColor: chromeColors.titleText,
            bodyColor: chromeColors.bodyText,
            borderColor: chromeColors.border,
            borderWidth: 1,
            padding: 12,
            callbacks: {
              label: context => {
                const label = context.dataset.label || '';
                const value = context.parsed.y;
                if (context.datasetIndex === 0) {
                  return `${label}: ${value === null ? 'not shown' : this.formatPrimary(value, primary.format)}`;
                }
                return `${label}: ${this.formatNumber(value ?? 0)}`;
              },
            },
          },
        },
        scales: {
          x: {
            grid: {
              color: chromeColors.gridLine,
            },
            ticks: {
              color: chromeColors.axisText,
              // Full width fits a month of "Sep 1" labels flat; rotation was
              // a symptom of the old half-width card, not of the data.
              maxRotation: 0,
              minRotation: 0,
              autoSkip: true,
              maxTicksLimit: 16,
            },
          },
          y: {
            type: 'linear',
            display: true,
            position: 'left',
            title: {
              display: true,
              text: primary.format === 'currency' ? 'Cost ($)' : primary.label,
              color: chromeColors.axisText,
            },
            grid: {
              color: chromeColors.gridLine,
            },
            ticks: {
              color: chromeColors.axisText,
              callback: (value, _index, ticks) =>
                primary.format === 'currency'
                  ? this.formatAxisCurrency(Number(value), ticks)
                  : this.formatPrimary(Number(value), primary.format),
            },
            suggestedMin: 0,
            suggestedMax: maxCost * 1.1,
          },
          y1: {
            type: 'linear',
            display: true,
            position: 'right',
            title: {
              display: true,
              text: secondaryLabel,
              color: chromeColors.axisText,
            },
            grid: {
              drawOnChartArea: false,
            },
            ticks: {
              color: chromeColors.axisText,
              callback: value => this.formatNumberShort(Number(value)),
            },
            suggestedMin: 0,
            suggestedMax: maxRequests * 1.1,
          },
        },
      },
    };

    this.chart = new Chart(canvas, config);
  }

  private formatDate(dateStr: string): string {
    // Parse as local date to avoid timezone offset issues
    // Input format: "YYYY-MM-DD" - split and create date with local timezone
    const [year, month, day] = dateStr.split('-').map(Number);
    const date = new Date(year, month - 1, day); // month is 0-indexed
    return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
  }

  formatCurrency(value: number): string {
    return new Intl.NumberFormat('en-US', {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(value);
  }

  /**
   * Axis tick label, with the precision decided by the whole tick set.
   *
   * Whole dollars are wrong whenever the axis steps by less than one: a
   * $3.50 peak gives Chart.js half-dollar gridlines, and rounding each label
   * independently printed "$4 $4 $3 $2 $2 $1 $1 $0" — seven labels for four
   * distinct values, against eight distinct lines. Reading the precision off
   * `ticks` (Chart.js hands the callback the full array) keeps the column
   * uniform instead of mixing "$1" with "$1.50".
   */
  private formatAxisCurrency(
    value: number,
    ticks: readonly { value: number }[]
  ): string {
    if (Math.abs(value) >= 1000) {
      return `$${(value / 1000).toFixed(1)}k`;
    }
    const fractional = ticks.some(t => !Number.isInteger(t.value));
    return `$${value.toFixed(fractional ? 2 : 0)}`;
  }

  private formatPrimary(value: number, format: TrendChartFormat): string {
    switch (format) {
      case 'currency':
        return this.formatCurrency(value);
      case 'usd-precise':
        return value === 0
          ? '$0'
          : new Intl.NumberFormat('en-US', {
              style: 'currency',
              currency: 'USD',
              maximumSignificantDigits: 3,
            }).format(value);
      case 'percent':
        return `${(value * 100).toFixed(1)}%`;
      case 'ms':
        return `${Math.round(value).toLocaleString('en-US')} ms`;
    }
  }

  formatNumber(value: number): string {
    return new Intl.NumberFormat('en-US').format(value);
  }

  private formatNumberShort(value: number): string {
    if (value >= 1000000) {
      return `${(value / 1000000).toFixed(1)}M`;
    }
    if (value >= 1000) {
      return `${(value / 1000).toFixed(1)}k`;
    }
    return value.toFixed(0);
  }
}

/**
 * Shades each band's dates behind the datasets, alternating two tints so
 * adjacent builds stay distinguishable, with the band's label at the top when
 * it fits. The x axis is a category axis of the points' dates, so a band's
 * edges are its first and last date's ticks, widened by half a step.
 *
 * Exported for the Evaluations page's per-run charts (eval-harness spec §16.3),
 * whose x axis is one category per run: several runs on one date sit inside
 * that date's band, so the same plugin shades them.
 */
export function versionBandsPlugin(
  points: readonly { date: string }[],
  bands: readonly TrendChartBand[],
  isDarkMode: boolean,
): Plugin<'line'> {
  const fills = isDarkMode
    ? ['rgba(148, 163, 184, 0.14)', 'rgba(148, 163, 184, 0.06)']
    : ['rgba(100, 116, 139, 0.12)', 'rgba(100, 116, 139, 0.04)'];
  // slate-200 / slate-700: AA against both tints, which the lighter pair was not.
  const text = isDarkMode ? '#e2e8f0' : '#334155';
  const dates = points.map(p => p.date);
  return {
    id: 'versionBands',
    beforeDatasetsDraw(chart) {
      const x = chart.scales['x'];
      const area = chart.chartArea;
      if (!x || !area || dates.length === 0) return;
      const step = dates.length > 1 ? x.getPixelForValue(1) - x.getPixelForValue(0) : area.right - area.left;
      const ctx = chart.ctx;
      ctx.save();
      // Two builds deployed on one day share that day's slot, so their bands
      // overlap; a label that would overprint the one before it is left off.
      let labelEnd = -Infinity;
      bands.forEach((band, i) => {
        const first = dates.findIndex(d => d >= band.from);
        let last = -1;
        for (let j = dates.length - 1; j >= 0; j--) {
          if (dates[j] <= band.to) {
            last = j;
            break;
          }
        }
        if (first < 0 || last < first) return;
        const left = Math.max(area.left, x.getPixelForValue(first) - step / 2);
        const right = Math.min(area.right, x.getPixelForValue(last) + step / 2);
        ctx.fillStyle = fills[i % 2];
        ctx.fillRect(left, area.top, right - left, area.bottom - area.top);
        ctx.font = '500 11px ui-sans-serif, system-ui, sans-serif';
        const width = ctx.measureText(band.label).width;
        if (width + 8 <= right - left && left + 4 >= labelEnd) {
          ctx.fillStyle = text;
          ctx.fillText(band.label, left + 4, area.top + 12);
          labelEnd = left + 4 + width + 8;
        }
      });
      ctx.restore();
    },
  };
}
