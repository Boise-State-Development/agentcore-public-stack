import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { DefaultToolResultComponent } from './default-tool-result.component';
import type { ToolResultData, ToolResultRenderer } from '../tool-renderer-registry.service';

/**
 * Registry proof point for the `calculator` tool. Renders identically to the
 * default today; it exists to validate that a distinct, tool-named component
 * resolves and slots in with zero visual change — the exact mechanism the
 * MCP App renderer will use.
 */
@Component({
  selector: 'app-calculator-tool-result',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DefaultToolResultComponent],
  template: `<app-default-tool-result [result]="result()" [minimized]="minimized()" />`,
})
export class CalculatorToolResultComponent implements ToolResultRenderer {
  result = input.required<ToolResultData>();
  minimized = input<boolean>(false);
}
