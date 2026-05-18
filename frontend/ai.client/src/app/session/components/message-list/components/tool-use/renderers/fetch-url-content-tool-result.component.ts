import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { DefaultToolResultComponent } from './default-tool-result.component';
import type { ToolResultData, ToolResultRenderer } from '../tool-renderer-registry.service';

/**
 * Registry proof point for the `fetch_url_content` tool. Renders identically
 * to the default today; it exists to validate the registry shape with a
 * distinct, tool-named component.
 */
@Component({
  selector: 'app-fetch-url-content-tool-result',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DefaultToolResultComponent],
  template: `<app-default-tool-result [result]="result()" [minimized]="minimized()" />`,
})
export class FetchUrlContentToolResultComponent implements ToolResultRenderer {
  result = input.required<ToolResultData>();
  minimized = input<boolean>(false);
}
