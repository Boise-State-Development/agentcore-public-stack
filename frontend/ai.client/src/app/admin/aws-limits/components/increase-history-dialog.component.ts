import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { DialogRef } from '@angular/cdk/dialog';
import { DialogShellComponent } from '../../../components/dialog/dialog-shell.component';
import { AwsLimitsStateService } from '../services/aws-limits-state.service';
import { IncreaseHistoryTableComponent } from './increase-history-table.component';

/**
 * Every increase request AWS still lists, opened from "View all" on the open
 * requests card. Approved requests are hidden by default: once AWS has said
 * yes there is nothing left to follow up.
 */
@Component({
  selector: 'app-increase-history-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, IncreaseHistoryTableComponent],
  host: { class: 'block' },
  template: `
    <app-dialog-shell
      title="Increase requests"
      description="Requests AWS lists for this account and Region, made from this app, the console, the CLI or Automatic Management."
      size="xl"
      (closed)="close()"
    >
      <div class="mb-4 flex flex-wrap items-center justify-between gap-3">
        <div class="flex items-center gap-2">
          <input
            id="aws-limits-hide-approved"
            type="checkbox"
            cdkFocusInitial
            [checked]="hideApproved()"
            (change)="hideApproved.set($any($event.target).checked)"
            class="size-4 cursor-pointer rounded accent-primary-accessible focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:accent-primary-500"
          />
          <label for="aws-limits-hide-approved" class="text-sm/6 text-gray-700 dark:text-gray-300">Hide approved requests</label>
        </div>
        @if (hideApproved() && hiddenCount()) {
          <p class="text-sm/6 text-gray-500 dark:text-gray-400" data-testid="hidden-count">
            {{ hiddenCount() }} approved {{ hiddenCount() === 1 ? 'request' : 'requests' }} hidden
          </p>
        }
      </div>

      @if (state.historyError(); as message) {
        <p role="alert" class="mb-3 rounded-2xl bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-200">
          {{ message }}
        </p>
      }
      @if (state.historyLoading() && !state.history().length) {
        <p class="py-6 text-center text-sm/6 text-gray-600 dark:text-gray-400">Loading requests…</p>
      } @else {
        <app-increase-history-table
          [requests]="visible()"
          [guidanceUrl]="state.dashboard()?.guidanceUrl ?? ''"
          [emptyMessage]="hiddenCount() ? 'No requests other than approved ones.' : 'No increase requests in this Region.'"
        />
      }
    </app-dialog-shell>
  `,
})
export class IncreaseHistoryDialogComponent implements OnInit {
  private dialogRef = inject<DialogRef<void>>(DialogRef);
  protected readonly state = inject(AwsLimitsStateService);

  readonly hideApproved = signal(true);

  readonly visible = computed(() =>
    this.hideApproved() ? this.state.history().filter(r => r.status !== 'APPROVED') : this.state.history(),
  );
  readonly hiddenCount = computed(() => this.state.history().length - this.visible().length);

  ngOnInit(): void {
    void this.state.loadHistory();
  }

  close(): void {
    this.dialogRef.close();
  }
}
