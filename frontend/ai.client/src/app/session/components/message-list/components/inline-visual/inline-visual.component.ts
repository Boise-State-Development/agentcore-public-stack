import { Component, input, computed, inject, ChangeDetectionStrategy } from '@angular/core';
import { ChartRendererComponent } from './renderers/chart-renderer.component';
import { DefaultRendererComponent } from './renderers/default-renderer.component';
import { FileDownloadRendererComponent } from './renderers/file-download-renderer.component';
import { VisualStateService } from '../../../../services/visual-state/visual-state.service';

/**
 * Router component for inline visual tool results.
 * Delegates to specific renderers based on uiType.
 */
@Component({
  selector: 'app-inline-visual',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ChartRendererComponent, DefaultRendererComponent, FileDownloadRendererComponent],
  template: `
    @if (!isDismissed()) {
      <div class="inline-visual-container">
        @switch (uiType()) {
          @case ('chart') {
            <app-chart-renderer
              [payload]="payload()"
              [isExpanded]="isExpanded()"
              (dismiss)="onDismiss()"
              (toggleExpanded)="onToggleExpanded()"
            />
          }
          @case ('file_download') {
            <app-file-download-renderer [payload]="payload()" />
          }
          @case ('word_document') {
            <!-- Backward-compat alias: cards persisted before the shared
                 file_download ui_type still route to the same renderer. -->
            <app-file-download-renderer [payload]="payload()" />
          }
          @default {
            <app-default-renderer
              [payload]="payload()"
              [uiType]="uiType()"
            />
          }
        }
      </div>
    }
  `,
  styles: `
    .inline-visual-container {
      width: 100%;
    }
  `
})
export class InlineVisualComponent {
  /** The UI type for this visual (e.g., 'chart') */
  uiType = input.required<string>();

  /** The payload data for the renderer */
  payload = input.required<unknown>();

  /** The tool use ID for state tracking */
  toolUseId = input.required<string>();

  private visualStateService = inject(VisualStateService);

  /** Whether this visual has been dismissed */
  isDismissed = computed(() =>
    this.visualStateService.isDismissed(this.toolUseId())
  );

  /** Whether this visual is expanded */
  isExpanded = computed(() =>
    this.visualStateService.isExpanded(this.toolUseId())
  );

  /** Dismiss this visual */
  onDismiss(): void {
    this.visualStateService.dismiss(this.toolUseId());
  }

  /** Toggle the expanded state */
  onToggleExpanded(): void {
    this.visualStateService.toggleExpanded(this.toolUseId());
  }
}
