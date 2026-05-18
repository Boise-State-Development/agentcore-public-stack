import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { JsonSyntaxHighlightPipe } from '../json-syntax-highlight.pipe';
import { ToolResultContent } from '../../../../../services/models/message.model';
import type { ToolResultData, ToolResultRenderer } from '../tool-renderer-registry.service';

/**
 * Fallback tool-result renderer. Reproduces the historical text / JSON /
 * image rendering verbatim — this is what every unregistered tool resolves
 * to, so its output must stay byte-for-byte identical to the markup that
 * previously lived inline in `tool-use.component.html`.
 *
 * `minimized` reproduces the two pre-existing variants: the minimized view
 * used `rounded-xs` cards and never rendered image content; the full view
 * used `rounded-sm` cards and appended images after the text/JSON items.
 */
@Component({
  selector: 'app-default-tool-result',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [JsonSyntaxHighlightPipe],
  styles: ':host { display: block; }',
  template: `
    <div class="space-y-2">
      @for (item of textItems(); track $index) {
        <div
          class="p-2 bg-gray-100 dark:bg-gray-800 border border-gray-300 dark:border-gray-600"
          [class.rounded-xs]="minimized()"
          [class.rounded-sm]="!minimized()"
        >
          @if (item.json) {
            <pre class="font-mono text-xs overflow-x-auto"><code [innerHTML]="formatContent(item) | jsonSyntaxHighlight"></code></pre>
          } @else {
            <div class="text-sm/6 text-gray-900 dark:text-gray-100 whitespace-pre-wrap">{{ formatContent(item) }}</div>
          }
        </div>
      }

      @if (!minimized()) {
        @for (item of imageItems(); track $index) {
          <div class="rounded-sm overflow-hidden bg-gray-100 dark:bg-gray-800 p-2 border border-gray-300 dark:border-gray-600">
            <img
              [src]="imageDataUrl(item)"
              alt="Tool result image"
              class="max-w-full h-auto rounded-xs"
            />
          </div>
        }
      }
    </div>
  `,
})
export class DefaultToolResultComponent implements ToolResultRenderer {
  result = input.required<ToolResultData>();
  minimized = input<boolean>(false);

  /** Text and JSON content items (images are rendered separately). */
  textItems = computed(() =>
    this.result().content.filter((item) => Boolean(item.text || item.json)),
  );

  /** Image content items. */
  imageItems = computed(() =>
    this.result().content.filter((item) => Boolean(item.image)),
  );

  formatContent(item: ToolResultContent): string {
    if (item.text) return item.text;
    if (item.json) return JSON.stringify(item.json, null, 2);
    return '';
  }

  imageDataUrl(item: ToolResultContent): string {
    if (!item.image) return '';
    return `data:image/${item.image.format};base64,${item.image.data}`;
  }
}
