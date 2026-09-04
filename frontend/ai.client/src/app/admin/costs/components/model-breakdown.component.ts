import {
  Component,
  ChangeDetectionStrategy,
  input,
  computed,
  effect,
  viewChild,
  ElementRef,
  signal,
} from '@angular/core';
import { Chart, ChartConfiguration, ChartData } from 'chart.js/auto';
import { ModelUsageSummary } from '../models';
import {
  CHART_CATEGORICAL_PALETTE,
  getChromeColorsForMode,
  getCategoricalColor,
} from '../../../shared/constants/chart-colors.constants';

type ChartView = 'pie' | 'bar';

/**
 * Model breakdown chart component.
 * Displays cost distribution across different AI models.
 */
@Component({
  selector: 'app-model-breakdown',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="bg-white dark:bg-gray-800 rounded-lg shadow-xs border border-gray-200 dark:border-gray-700 p-6"
    >
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-lg font-semibold text-gray-900 dark:text-white">
          Model Usage Breakdown
        </h3>
        <!-- View toggle -->
        <div
          class="inline-flex rounded-lg border border-gray-200 dark:border-gray-700 p-1"
        >
          <button
            type="button"
            (click)="setChartView('pie')"
            class="px-3 py-1 text-sm font-medium rounded-md transition-colors"
            [class.bg-primary-100]="chartView() === 'pie'"
            [class.text-primary-accessible]="chartView() === 'pie'"
            [class.dark:bg-primary-900/30]="chartView() === 'pie'"
            [class.dark:text-primary-accessible-dark]="chartView() === 'pie'"
            [class.text-gray-600]="chartView() !== 'pie'"
            [class.dark:text-gray-400]="chartView() !== 'pie'"
            [class.hover:text-gray-900]="chartView() !== 'pie'"
            [class.dark:hover:text-white]="chartView() !== 'pie'"
          >
            Pie
          </button>
          <button
            type="button"
            (click)="setChartView('bar')"
            class="px-3 py-1 text-sm font-medium rounded-md transition-colors"
            [class.bg-primary-100]="chartView() === 'bar'"
            [class.text-primary-accessible]="chartView() === 'bar'"
            [class.dark:bg-primary-900/30]="chartView() === 'bar'"
            [class.dark:text-primary-accessible-dark]="chartView() === 'bar'"
            [class.text-gray-600]="chartView() !== 'bar'"
            [class.dark:text-gray-400]="chartView() !== 'bar'"
            [class.hover:text-gray-900]="chartView() !== 'bar'"
            [class.dark:hover:text-white]="chartView() !== 'bar'"
          >
            Bar
          </button>
        </div>
      </div>

      @if (data().length === 0) {
        <div
          class="h-64 flex items-center justify-center bg-gray-50 dark:bg-gray-900/50 rounded-lg border-2 border-dashed border-gray-200 dark:border-gray-700"
        >
          <p class="text-sm text-gray-500 dark:text-gray-400">
            No model usage data available for this period
          </p>
        </div>
      } @else {
        <div class="h-64">
          <canvas #chartCanvas></canvas>
        </div>

        <!-- Legend/Details table -->
        <div class="mt-4 pt-4 border-t border-gray-200 dark:border-gray-700">
          <div class="space-y-2">
            @for (model of sortedData(); track model.modelId; let i = $index) {
              <div
                class="flex items-center justify-between py-2 px-3 rounded-lg hover:bg-gray-50 dark:hover:bg-gray-700/50 transition-colors"
              >
                <div class="flex items-center gap-3">
                  <span
                    class="size-3 rounded-full shrink-0"
                    [style.background-color]="getColor(i)"
                  ></span>
                  <div class="min-w-0">
                    <p
                      class="text-sm font-medium text-gray-900 dark:text-white truncate"
                    >
                      {{ model.modelName }}
                    </p>
                    <p class="text-xs text-gray-500 dark:text-gray-400">
                      {{ formatNumber(model.totalRequests) }} requests •
                      {{ formatNumber(model.uniqueUsers) }} users
                    </p>
                  </div>
                </div>
                <div class="text-right shrink-0">
                  <p class="text-sm font-medium text-gray-900 dark:text-white">
                    {{ formatCurrency(model.totalCost) }}
                  </p>
                  <p class="text-xs text-gray-500 dark:text-gray-400">
                    {{ getPercentage(model.totalCost) }}%
                  </p>
                </div>
              </div>
            }
          </div>
        </div>
      }
    </div>
  `,
})
export class ModelBreakdownComponent {
  data = input.required<ModelUsageSummary[]>();

  chartView = signal<ChartView>('pie');

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
              callback: value => this.formatCurrencyShort(Number(value)),
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

  private formatCurrencyShort(value: number): string {
    if (value >= 1000) {
      return `$${(value / 1000).toFixed(1)}k`;
    }
    return `$${value.toFixed(0)}`;
  }

  formatNumber(value: number): string {
    return new Intl.NumberFormat('en-US').format(value);
  }
}
