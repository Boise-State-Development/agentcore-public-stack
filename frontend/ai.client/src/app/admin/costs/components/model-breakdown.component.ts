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
  signal,
} from '@angular/core';
import { Chart, ChartConfiguration, ChartData } from 'chart.js/auto';
import { ModelUsageSummary } from '../models';
import {
  getChromeColorsForMode,
  getCategoricalColor,
} from '../../../shared/constants/chart-colors.constants';

type ChartView = 'pie' | 'bar';

/**
 * Model breakdown chart component.
 *
 * Displays cost distribution across models. Pie is the default view: the
 * question the panel exists to answer is what share of the period's spend
 * each model took, and a part-of-whole reads off a donut at a glance in a way
 * it does not off ranked bars.
 *
 * The card owns the console's full content width, and the two views want that
 * width differently — a donut is constrained by the shorter side, so making
 * it wider past a point only adds empty canvas, whereas the horizontal bars
 * carry long `us.anthropic.*` labels and earn every pixel. Hence the column
 * split flips with the view rather than being fixed: donut 2/5 with a roomy
 * legend beside it, bars 3/5.
 */
@Component({
  selector: 'app-model-breakdown',
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
            Model Usage Breakdown
          </h3>
          <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
            Share of the period's spend by model.
          </p>
        </div>

        <!-- View toggle. One setting with two values, so a radiogroup
             rather than two independent buttons, and the shell/child radius
             pair is the sanctioned segment idiom. -->
        <div
          role="radiogroup"
          aria-label="Chart view"
          class="inline-flex shrink-0 self-start items-center gap-1 rounded-xl border border-gray-200 bg-gray-50 p-1 sm:self-center dark:border-gray-700 dark:bg-gray-900"
        >
          @for (option of viewOptions; track option.value) {
            <button
              type="button"
              role="radio"
              [attr.aria-checked]="chartView() === option.value"
              (click)="setChartView(option.value)"
              class="rounded-lg px-3.5 py-1 text-sm/6 font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
              [class]="
                chartView() === option.value
                  ? 'bg-white text-gray-900 shadow-xs dark:bg-gray-800 dark:text-white'
                  : 'text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white'
              "
            >
              {{ option.label }}
            </button>
          }
        </div>
      </div>

      @if (data().length === 0) {
        <div class="p-6">
          <div
            class="flex h-64 items-center justify-center rounded-2xl border-2 border-dashed border-gray-200 bg-gray-50 dark:border-gray-700 dark:bg-gray-900/50"
          >
            <p class="text-sm text-gray-500 dark:text-gray-400">
              No model usage data available for this period
            </p>
          </div>
        </div>
      } @else {
        <div class="grid grid-cols-1 gap-8 p-6 lg:grid-cols-5">
          <div [class]="chartView() === 'pie' ? 'lg:col-span-2' : 'lg:col-span-3'">
            <div class="h-72 sm:h-80 lg:h-[26rem]">
              <canvas #chartCanvas></canvas>
            </div>
          </div>

          <!-- Legend / details. Full-height beside the chart on wide screens
               instead of stacked underneath, so the whole fleet is visible
               without scrolling past the chart to reach the tail. -->
          <div
            class="lg:min-w-0"
            [class]="chartView() === 'pie' ? 'lg:col-span-3' : 'lg:col-span-2'"
          >
            <ul
              role="list"
              class="divide-y divide-gray-100 dark:divide-gray-700/60"
            >
              @for (model of sortedData(); track model.modelId; let i = $index) {
                <li
                  class="flex items-center justify-between gap-4 rounded-lg px-3 py-2.5 transition-colors hover:bg-gray-50 dark:hover:bg-gray-700/50"
                >
                  <div class="flex min-w-0 items-center gap-3">
                    <span
                      class="size-3 shrink-0 rounded-full"
                      [style.background-color]="getColor(i)"
                    ></span>
                    <div class="min-w-0">
                      <p
                        class="truncate text-sm/6 font-medium text-gray-900 dark:text-white"
                      >
                        {{ model.modelName }}
                      </p>
                      <p class="text-xs/5 text-gray-500 dark:text-gray-400">
                        {{ formatNumber(model.totalRequests) }} requests •
                        {{ formatNumber(model.uniqueUsers) }}
                        {{ model.uniqueUsers === 1 ? 'user' : 'users' }}
                      </p>
                    </div>
                  </div>

                  <div class="flex shrink-0 items-center gap-4">
                    <!-- Share bar, pie view only: the donut encodes share by
                         arc, which is hard to compare between two small
                         slices, so a shared baseline makes the ranking
                         readable. In bar view the chart already IS that
                         baseline, and the column it costs is what truncates
                         the model ids beside it. -->
                    @if (chartView() === 'pie') {
                      <div
                        class="hidden h-1.5 w-24 overflow-hidden rounded-full bg-gray-100 xl:block dark:bg-gray-700"
                        aria-hidden="true"
                      >
                        <div
                          class="h-full rounded-full"
                          [style.width.%]="getPercentage(model.totalCost)"
                          [style.background-color]="getColor(i)"
                        ></div>
                      </div>
                    }
                    <div class="w-24 text-right">
                      <p
                        class="text-sm/6 font-semibold text-gray-900 tabular-nums dark:text-white"
                      >
                        {{ formatCurrency(model.totalCost) }}
                      </p>
                      <p class="text-xs/5 text-gray-500 tabular-nums dark:text-gray-400">
                        {{ getPercentage(model.totalCost) }}%
                      </p>
                    </div>
                  </div>
                </li>
              }
            </ul>
          </div>
        </div>
      }
    </div>
  `,
})
export class ModelBreakdownComponent {
  data = input.required<ModelUsageSummary[]>();

  chartView = signal<ChartView>('pie');

  protected readonly viewOptions: ReadonlyArray<{
    value: ChartView;
    label: string;
  }> = [
    { value: 'pie', label: 'Pie' },
    { value: 'bar', label: 'Bar' },
  ];

  private chartCanvas = viewChild<ElementRef<HTMLCanvasElement>>('chartCanvas');
  private chart: Chart | null = null;

  // Sort data by cost descending
  sortedData = computed(() => {
    return [...this.data()].sort((a, b) => b.totalCost - a.totalCost);
  });

  // Total cost for percentage calculation
  totalCost = computed(() => {
    return this.data().reduce((sum, m) => sum + m.totalCost, 0);
  });

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
      const models = this.sortedData();
      const view = this.chartView();

      if (canvas && models.length > 0) {
        this.renderChart(canvas.nativeElement, models, view);
      }
    });
  }

  setChartView(view: ChartView): void {
    this.chartView.set(view);
  }

  getColor(index: number): string {
    return getCategoricalColor(index);
  }

  getPercentage(cost: number): string {
    const total = this.totalCost();
    if (total === 0) return '0';
    return ((cost / total) * 100).toFixed(1);
  }

  private renderChart(
    canvas: HTMLCanvasElement,
    models: ModelUsageSummary[],
    view: ChartView
  ): void {
    // Destroy existing chart if present
    if (this.chart) {
      this.chart.destroy();
    }

    const labels = models.map(m => m.modelName);
    const costData = models.map(m => m.totalCost);
    const backgroundColors = models.map((_, i) => this.getColor(i));

    const isDarkMode = document.documentElement.classList.contains('dark');
    const chromeColors = getChromeColorsForMode(isDarkMode);

    if (view === 'pie') {
      this.renderPieChart(canvas, labels, costData, backgroundColors, isDarkMode, chromeColors);
    } else {
      this.renderBarChart(canvas, labels, costData, backgroundColors, chromeColors, isDarkMode);
    }
  }

  private renderPieChart(
    canvas: HTMLCanvasElement,
    labels: string[],
    data: number[],
    colors: string[],
    isDarkMode: boolean,
    chromeColors: ReturnType<typeof getChromeColorsForMode>
  ): void {
    const chartData: ChartData<'doughnut'> = {
      labels,
      datasets: [
        {
          data,
          backgroundColor: colors,
          borderColor: isDarkMode ? chromeColors.background : '#ffffff',
          borderWidth: 2,
          hoverOffset: 4,
        },
      ],
    };

    const config: ChartConfiguration<'doughnut'> = {
      type: 'doughnut',
      data: chartData,
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: '60%',
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
                const value = context.parsed;
                const total = context.dataset.data.reduce((a, b) => a + b, 0);
                const percentage = ((value / total) * 100).toFixed(1);
                return `${this.formatCurrency(value)} (${percentage}%)`;
              },
            },
          },
        },
      },
    };

    this.chart = new Chart(canvas, config);
  }

  private renderBarChart(
    canvas: HTMLCanvasElement,
    labels: string[],
    data: number[],
    colors: string[],
    chromeColors: ReturnType<typeof getChromeColorsForMode>,
    isDarkMode: boolean
  ): void {
    const chartData: ChartData<'bar'> = {
      labels,
      datasets: [
        {
          label: 'Cost',
          data,
          backgroundColor: colors,
          borderRadius: 4,
        },
      ],
    };

    const config: ChartConfiguration<'bar'> = {
      type: 'bar',
      data: chartData,
      options: {
        responsive: true,
        maintainAspectRatio: false,
        indexAxis: 'y',
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
                return this.formatCurrency(context.parsed.x ?? 0);
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
              callback: (value, _index, ticks) =>
                this.formatAxisCurrency(Number(value), ticks),
            },
          },
          y: {
            grid: {
              display: false,
            },
            ticks: {
              color: chromeColors.axisText,
            },
          },
        },
      },
    };

    this.chart = new Chart(canvas, config);
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
   * Axis tick label, with the precision decided by the whole tick set — see
   * the same helper on `CostTrendsChartComponent`. Rounding each label on its
   * own prints "$4 $4 $3 $2 $2" whenever the axis steps by under a dollar,
   * which is every period where no single model cleared $10.
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
}
