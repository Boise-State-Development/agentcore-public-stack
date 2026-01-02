import {
  Component,
  ChangeDetectionStrategy,
  input,
  computed,
  effect,
  viewChild,
  ElementRef,
} from '@angular/core';
import { Chart, ChartConfiguration, ChartData } from 'chart.js/auto';
import { CostTrend } from '../models';

/**
 * Cost trends line chart component.
 * Displays daily cost and request trends over the selected period.
 */
@Component({
  selector: 'app-cost-trends-chart',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div
      class="bg-white dark:bg-gray-800 rounded-lg shadow-xs border border-gray-200 dark:border-gray-700 p-6"
    >
      <div class="flex items-center justify-between mb-4">
        <h3 class="text-lg font-semibold text-gray-900 dark:text-white">
          Cost Trends
        </h3>
        <div class="flex items-center gap-4 text-sm">
          <div class="flex items-center gap-2">
            <span class="size-3 rounded-full bg-blue-500"></span>
            <span class="text-gray-600 dark:text-gray-400">Cost</span>
          </div>
          <div class="flex items-center gap-2">
            <span class="size-3 rounded-full bg-emerald-500"></span>
            <span class="text-gray-600 dark:text-gray-400">Requests</span>
          </div>
        </div>
      </div>

      @if (data().length === 0) {
        <div
          class="h-64 flex items-center justify-center bg-gray-50 dark:bg-gray-900/50 rounded-lg border-2 border-dashed border-gray-200 dark:border-gray-700"
        >
          <p class="text-sm text-gray-500 dark:text-gray-400">
            No trend data available for this period
          </p>
        </div>
      } @else {
        <div class="h-64">
          <canvas #chartCanvas></canvas>
        </div>
      }

      <!-- Summary stats below chart -->
      @if (data().length > 0) {
        <div
          class="mt-4 pt-4 border-t border-gray-200 dark:border-gray-700 grid grid-cols-3 gap-4"
        >
          <div class="text-center">
            <p class="text-sm text-gray-500 dark:text-gray-400">Peak Cost</p>
            <p class="text-lg font-semibold text-gray-900 dark:text-white">
              {{ formatCurrency(peakCost()) }}
            </p>
          </div>
          <div class="text-center">
            <p class="text-sm text-gray-500 dark:text-gray-400">Avg Daily</p>
            <p class="text-lg font-semibold text-gray-900 dark:text-white">
              {{ formatCurrency(avgDailyCost()) }}
            </p>
          </div>
          <div class="text-center">
            <p class="text-sm text-gray-500 dark:text-gray-400">Total Days</p>
            <p class="text-lg font-semibold text-gray-900 dark:text-white">
              {{ data().length }}
            </p>
          </div>
        </div>
      }
    </div>
  `,
})
export class CostTrendsChartComponent {
  data = input.required<CostTrend[]>();

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

  constructor() {
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
    const gridColor = isDarkMode ? 'rgba(255, 255, 255, 0.1)' : 'rgba(0, 0, 0, 0.1)';
    const textColor = isDarkMode ? '#9ca3af' : '#6b7280';

    const chartData: ChartData<'line'> = {
      labels,
      datasets: [
        {
          label: 'Cost ($)',
          data: costData,
          borderColor: '#3b82f6',
          backgroundColor: 'rgba(59, 130, 246, 0.1)',
          fill: true,
          tension: 0.3,
          yAxisID: 'y',
          pointRadius: 3,
          pointHoverRadius: 5,
        },
        {
          label: 'Requests',
          data: requestsData,
          borderColor: '#10b981',
          backgroundColor: 'rgba(16, 185, 129, 0.1)',
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
            backgroundColor: isDarkMode ? '#1f2937' : '#ffffff',
            titleColor: isDarkMode ? '#ffffff' : '#111827',
            bodyColor: isDarkMode ? '#d1d5db' : '#4b5563',
            borderColor: isDarkMode ? '#374151' : '#e5e7eb',
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
              color: gridColor,
            },
            ticks: {
              color: textColor,
              maxRotation: 45,
              minRotation: 0,
            },
          },
          y: {
            type: 'linear',
            display: true,
            position: 'left',
            title: {
              display: true,
              text: 'Cost ($)',
              color: textColor,
            },
            grid: {
              color: gridColor,
            },
            ticks: {
              color: textColor,
              callback: value => this.formatCurrencyShort(Number(value)),
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
              color: textColor,
            },
            grid: {
              drawOnChartArea: false,
            },
            ticks: {
              color: textColor,
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

  private formatCurrencyShort(value: number): string {
    if (value >= 1000) {
      return `$${(value / 1000).toFixed(1)}k`;
    }
    return `$${value.toFixed(0)}`;
  }

  private formatNumber(value: number): string {
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
