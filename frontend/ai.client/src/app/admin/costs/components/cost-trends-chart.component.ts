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
import { Chart, ChartConfiguration, ChartData } from 'chart.js/auto';
import { CostTrend } from '../models';
import {
  CHART_SERIES_COLORS,
  CHART_FILL_COLORS,
  getChromeColorsForMode,
} from '../../../shared/constants/chart-colors.constants';

/**
 * Cost trends line chart component.
 *
 * Displays daily cost and request trends over the selected period. The card
 * takes the console's full content width and the chart is sized off that: a
 * month of daily points in a half-width box put the date axis on a 45-degree
 * rotation and still dropped labels, which is the one thing a trend chart
 * cannot afford to lose.
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
            Cost Trends
          </h3>
          <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
            Daily spend and request volume across the period.
          </p>
        </div>
        <!-- intentional: legend dots mirror the Chart.js dataset borderColor
             values from CHART_SERIES_COLORS constants (#3b82f6/#10b981), which are
             exempt from token migration per identity.css's chart-series note -->
        <div class="flex shrink-0 items-center gap-4 text-sm/6">
          <div class="flex items-center gap-2">
            <span class="size-3 rounded-full" [style.background-color]="CHART_SERIES_COLORS.cost"></span>
            <span class="text-gray-600 dark:text-gray-400">Cost</span>
          </div>
          <div class="flex items-center gap-2">
            <span class="size-3 rounded-full" [style.background-color]="CHART_SERIES_COLORS.requests"></span>
            <span class="text-gray-600 dark:text-gray-400">Requests</span>
          </div>
        </div>
      </div>

      <div class="p-6">
        @if (data().length === 0) {
          <div
            class="flex h-64 items-center justify-center rounded-2xl border-2 border-dashed border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-900/50"
          >
            <p class="text-sm text-gray-500 dark:text-gray-400">
              No trend data available for this period
            </p>
          </div>
        } @else {
          <div class="h-72 sm:h-80 lg:h-[26rem]">
            <canvas #chartCanvas></canvas>
          </div>
        }
      </div>

      <!-- Summary stats below chart -->
      @if (data().length > 0) {
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
  data = input.required<CostTrend[]>();

  // Export constant for template access
  protected readonly CHART_SERIES_COLORS = CHART_SERIES_COLORS;

  private chartCanvas = viewChild<ElementRef<HTMLCanvasElement>>('chartCanvas');
  private chart: Chart | null = null;

  // Computed stats
  peakCost = computed(() => {
    const trends = this.data();
    if (trends.length === 0) return 0;
    return Math.max(...trends.map(t => t.totalCost));
  });

  avgDailyCost = computed(() => {
    const trends = this.data();
    if (trends.length === 0) return 0;
    const total = trends.reduce((sum, t) => sum + t.totalCost, 0);
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

      if (canvas && trends.length > 0) {
        this.renderChart(canvas.nativeElement, trends);
      }
    });
  }

  private renderChart(canvas: HTMLCanvasElement, trends: CostTrend[]): void {
    // Destroy existing chart if present
    if (this.chart) {
      this.chart.destroy();
    }

    const labels = trends.map(t => this.formatDate(t.date));
    const costData = trends.map(t => t.totalCost);
    const requestsData = trends.map(t => t.totalRequests);

    // Calculate max values for scaling
    const maxCost = Math.max(...costData);
    const maxRequests = Math.max(...requestsData);

    const isDarkMode = document.documentElement.classList.contains('dark');
    const chromeColors = getChromeColorsForMode(isDarkMode);

    const chartData: ChartData<'line'> = {
      labels,
      datasets: [
        {
          label: 'Cost ($)',
          data: costData,
          borderColor: CHART_SERIES_COLORS.cost,
          backgroundColor: CHART_FILL_COLORS.cost,
          fill: true,
          tension: 0.3,
          yAxisID: 'y',
          pointRadius: 3,
          pointHoverRadius: 5,
        },
        {
          label: 'Requests',
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
                const value = context.parsed.y ?? 0;
                if (label.includes('Cost')) {
                  return `${label}: ${this.formatCurrency(value)}`;
                }
                return `${label}: ${this.formatNumber(value)}`;
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
              text: 'Cost ($)',
              color: chromeColors.axisText,
            },
            grid: {
              color: chromeColors.gridLine,
            },
            ticks: {
              color: chromeColors.axisText,
              callback: (value, _index, ticks) =>
                this.formatAxisCurrency(Number(value), ticks),
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
              text: 'Requests',
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
