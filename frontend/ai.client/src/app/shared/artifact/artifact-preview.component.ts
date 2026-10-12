import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCodeBracket, heroDocument, heroDocumentText, heroPhoto, heroTableCells } from '@ng-icons/heroicons/outline';
import { artifactTypeStyle } from './shared-artifact-card.component';
import { csvTable, isNumeric, markdownBlocks, previewKind, svgDataUri } from './artifact-preview';

/**
 * The top of an artifact, drawn small: a miniature of the document a card
 * sits on. Each kind gets its own drawing (Markdown as headings and prose, a
 * CSV as a table, a web page in a browser frame, an SVG as itself), and an
 * artifact with no stored excerpt shows its type's icon instead.
 *
 * Decorative: the card around it carries the accessible name, so the whole
 * miniature is hidden from assistive technology.
 */
@Component({
  selector: 'app-artifact-preview',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroCodeBracket, heroDocument, heroDocumentText, heroPhoto, heroTableCells })],
  host: { 'aria-hidden': 'true', class: 'block' },
  template: `
    @if (!excerpt()) {
      <div class="grid h-40 place-items-center" [class]="style().bg">
        <span [class]="style().text"><ng-icon [name]="style().icon" size="2rem" /></span>
      </div>
    } @else {
      @switch (kind()) {
        @case ('markdown') {
          <div data-testid="preview-markdown" class="space-y-1 px-3.5 pt-3 text-[10px]/[1.45] text-gray-600 dark:text-gray-300">
            @for (b of blocks(); track $index) {
              @switch (b.kind) {
                @case ('heading') {
                  <p class="font-semibold text-gray-900 dark:text-white" [class.text-xs]="b.level === 1">{{ b.text }}</p>
                }
                @case ('item') {
                  <p class="flex gap-1.5 pl-1"><span class="mt-[0.55em] size-1 shrink-0 rounded-full bg-current opacity-60"></span><span>{{ b.text }}</span></p>
                }
                @default {
                  <p>{{ b.text }}</p>
                }
              }
            }
          </div>
        }
        @case ('csv') {
          <div class="px-2.5 pt-2.5">
            <table data-testid="preview-csv" class="w-full table-fixed border-collapse text-[9.5px]/[1.5] tabular-nums text-gray-600 dark:text-gray-300">
              <thead>
                <tr>
                  @for (h of table().header; track $index) {
                    <th class="truncate border-b border-gray-200 px-1 py-0.5 text-left font-semibold text-gray-900 dark:border-gray-700 dark:text-white" [class.text-right]="numericColumn($index)">{{ h }}</th>
                  }
                </tr>
              </thead>
              <tbody>
                @for (row of table().rows; track $index) {
                  <tr>
                    @for (cell of row; track $index) {
                      <td class="truncate border-b border-gray-100 px-1 py-0.5 dark:border-gray-700/60" [class.text-right]="numericColumn($index)">{{ cell }}</td>
                    }
                  </tr>
                }
              </tbody>
            </table>
          </div>
        }
        @case ('page') {
          <div data-testid="preview-page">
            <div class="flex items-center gap-1 border-b border-gray-200 bg-gray-50 px-2 py-1.5 dark:border-gray-700 dark:bg-gray-900/60">
              <span class="size-1.5 rounded-full bg-gray-300 dark:bg-gray-600"></span>
              <span class="size-1.5 rounded-full bg-gray-300 dark:bg-gray-600"></span>
              <span class="size-1.5 rounded-full bg-gray-300 dark:bg-gray-600"></span>
              <span class="ml-1.5 h-1.5 flex-1 rounded-full bg-gray-200 dark:bg-gray-700"></span>
            </div>
            <p class="px-3.5 pt-2.5 text-[10px]/[1.5] text-gray-600 dark:text-gray-300">{{ excerpt() }}</p>
          </div>
        }
        @case ('svg') {
          <div class="grid h-40 place-items-center p-3" [class]="style().bg">
            <img data-testid="preview-svg" [src]="svgSrc()" alt="" class="max-h-full max-w-full object-contain" />
          </div>
        }
        @default {
          <p class="px-3.5 pt-3 text-[10px]/[1.5] whitespace-pre-line text-gray-600 dark:text-gray-300">{{ excerpt() }}</p>
        }
      }
    }
  `,
})
export class ArtifactPreviewComponent {
  readonly contentType = input.required<string>();
  /** The stored excerpt; null, undefined or empty draws the type's icon. */
  readonly excerpt = input<string | null | undefined>(null);

  protected readonly kind = computed(() => previewKind(this.contentType()));
  protected readonly style = computed(() => artifactTypeStyle(this.contentType()));
  protected readonly blocks = computed(() => (this.kind() === 'markdown' ? markdownBlocks(this.excerpt() ?? '') : []));
  protected readonly table = computed(() => (this.kind() === 'csv' ? csvTable(this.excerpt() ?? '') : { header: [], rows: [] }));
  protected readonly svgSrc = computed(() => (this.kind() === 'svg' ? svgDataUri(this.excerpt() ?? '') : ''));
  private readonly numericColumns = computed(() => {
    const { rows } = this.table();
    const width = Math.max(0, ...rows.map(r => r.length));
    return Array.from({ length: width }, (_, i) => rows.length > 0 && rows.every(r => !r[i] || isNumeric(r[i])));
  });

  protected numericColumn(index: number): boolean {
    return this.numericColumns()[index] ?? false;
  }
}
