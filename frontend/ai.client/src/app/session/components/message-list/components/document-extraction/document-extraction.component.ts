import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import type {
  DocumentExtractionField,
  DocumentExtractionRun,
  SourceLocator,
} from '../../../../../shared/utils/stream-parser';
import { ArtifactStateService } from '../../../../services/artifacts/artifact-state.service';

type FieldState = DocumentExtractionField['state'];

/** One column of the table: a field, in the field set's authored order. */
interface FieldColumn {
  name: string;
  title: string;
}

/** One document's row in the multi-document table. */
interface DocumentRow {
  run: DocumentExtractionRun;
  cells: (DocumentExtractionField | null)[];
}

/** The cell whose passage is showing under the multi-document table. */
interface CellRef {
  uploadId: string;
  field: string;
}

/**
 * The rows one `document_extract` call produced, rendered under the answer
 * (`docs/specs/structured-extraction.md` §8.1, §17).
 *
 * The model is told this table "is already displayed to the user" and is
 * asked to compare the documents rather than re-type it, so this is the
 * deliverable, not a detail: it opens by default.
 *
 * Every value carries the source-provenance state its backend run assigned,
 * in the same words `SourceProvenanceComponent` uses, because it is the same
 * verdict from the same module. Three rules carry over from that component
 * because they are the feature, not its look:
 *
 * 1. **A plain locator appears only for `supported`.** Every other state says
 *    in words what is and is not established.
 * 2. **`quote_unsupported` is counted in the header** and stays visible when
 *    the table is collapsed.
 * 3. **Colour is never the only signal.** Each non-supported value has its
 *    state in words beside it.
 *
 * And two of its own, from §7 and §12: no column is headed "verified" (the
 * claim is only that the value is *in* the passage, which the footer states
 * once), and a run whose schema was not enforced says so.
 *
 * One document reads as a field list with each passage inline. Several read
 * as a documents × fields grid — the comparison is the point of running a
 * set (§17.1) — with a cell's passage shown under the grid when selected.
 */
@Component({
  selector: 'app-document-extraction',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styles: `
    :host {
      display: block;
    }

    /* Chevron, dot and accordion match source-provenance and the tool rail. */
    .chevron {
      transition: transform 0.2s ease-out;
    }

    .chevron.open {
      transform: rotate(90deg);
    }

    .status-dot {
      display: inline-block;
      width: 6px;
      height: 6px;
      border-radius: 9999px;
      flex-shrink: 0;
    }

    .collapsible-content {
      display: grid;
      grid-template-rows: 0fr;
      transition: grid-template-rows 0.2s ease-out;
    }

    .collapsible-content.open {
      grid-template-rows: 1fr;
    }

    .collapsible-content > div {
      overflow: hidden;
      opacity: 0;
      transition: opacity 0s ease-out;
    }

    .collapsible-content.open > div {
      opacity: 1;
      transition: opacity 0.15s ease-out 0.05s;
    }

    /* The table inherits the chat's markdown-table look from the global
       '.message-block table' rules, so it reads like the tables the model
       writes. Two overrides: it scrolls sideways inside its own box rather
       than widening the thread, and the prose rule's bottom margin and
       paragraph spacing would punch gaps into a dense grid. */
    .table-scroll {
      overflow-x: auto;
      margin-top: 0.5rem;
      border-radius: 0.5rem;
    }

    .table-scroll table {
      margin-bottom: 0;
      font-size: 0.75rem;
    }

    .table-scroll.matrix table {
      width: max-content;
      min-width: 100%;
    }

    .table-scroll th,
    .table-scroll td {
      vertical-align: top;
    }

    .cell-quote,
    .detail p {
      margin: 0.125rem 0 0;
    }
  `,
  template: `
    @if (runs().length) {
      <div class="mt-2">
        <button
          type="button"
          (click)="toggle()"
          class="group/extr flex w-full cursor-pointer items-center gap-2 text-left"
          [attr.aria-expanded]="isOpen()"
          [attr.aria-controls]="panelId"
          [attr.aria-label]="(isOpen() ? 'Hide ' : 'Show ') + headline()"
        >
          <svg
            class="chevron size-3 shrink-0 text-gray-400 dark:text-gray-500"
            [class.open]="isOpen()"
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
            aria-hidden="true"
          >
            <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M9 5l7 7-7 7" />
          </svg>
          <span
            class="min-w-0 truncate text-xs text-gray-500 group-hover/extr:text-gray-700 dark:text-gray-400 dark:group-hover/extr:text-gray-200"
          >
            {{ headline() }}
          </span>
          @if (unconfirmedCount(); as bad) {
            <span
              class="shrink-0 text-[11px] font-medium text-state-danger-600 dark:text-state-danger-400"
            >
              {{ bad }} unconfirmed
            </span>
          }
        </button>

        <div
          class="collapsible-content"
          [class.open]="isOpen()"
          [attr.aria-hidden]="isOpen() ? null : 'true'"
          [attr.inert]="isOpen() ? null : ''"
        >
          <div [id]="panelId">
            @if (unenforcedNote(); as note) {
              <p class="mt-2 text-[11px] text-state-warning-700 dark:text-state-warning-400">
                {{ note }}
              </p>
            }

            @if (single(); as run) {
              <div class="table-scroll text-gray-800 dark:text-gray-100">
                <table>
                  <caption class="sr-only">{{ headline() }}</caption>
                  <thead>
                    <tr>
                      <th scope="col">Field</th>
                      <th scope="col">Value</th>
                      <th scope="col">Source</th>
                    </tr>
                  </thead>
                  <tbody>
                    @for (field of run.fields; track field.name) {
                      <tr>
                        <th scope="row" class="font-medium">{{ field.title }}</th>
                        <td>
                          @if (field.value) {
                            {{ field.value }}
                          } @else {
                            <span class="text-gray-500 italic dark:text-gray-400">Not found</span>
                          }
                        </td>
                        <td>
                          <span class="flex items-center gap-1.5">
                            <span [class]="dotClass(field.state)" aria-hidden="true"></span>
                            <span [class]="verdictClass(field.state)">{{ verdict(field) }}</span>
                          </span>
                          @if (field.quote) {
                            <p class="cell-quote text-[11px] text-gray-500 dark:text-gray-400">
                              <q>{{ field.quote }}</q>
                            </p>
                          }
                        </td>
                      </tr>
                    }
                  </tbody>
                </table>
              </div>
            } @else {
              <div class="table-scroll matrix text-gray-800 dark:text-gray-100">
                <table>
                  <caption class="sr-only">{{ headline() }}</caption>
                  <thead>
                    <tr>
                      <th scope="col">Document</th>
                      @for (col of columns(); track col.name) {
                        <th scope="col">{{ col.title }}</th>
                      }
                    </tr>
                  </thead>
                  <tbody>
                    @for (row of rows(); track row.run.uploadId) {
                      <tr>
                        <th scope="row" class="max-w-48 font-medium break-words">
                          {{ row.run.filename || 'Document' }}
                        </th>
                        @for (cell of row.cells; track $index) {
                          <td>
                            @if (cell) {
                              <button
                                type="button"
                                class="flex max-w-56 cursor-pointer flex-col items-start rounded text-left focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                                [attr.aria-expanded]="isSelected(row.run, cell)"
                                [attr.aria-controls]="detailId"
                                [attr.aria-label]="cellLabel(row.run, cell)"
                                (click)="select(row.run, cell)"
                              >
                                <span class="flex items-start gap-1.5">
                                  <span
                                    [class]="dotClass(cell.state) + ' mt-1.5'"
                                    aria-hidden="true"
                                  ></span>
                                  @if (cell.value) {
                                    <span class="line-clamp-3">{{ cell.value }}</span>
                                  } @else {
                                    <span class="text-gray-500 italic dark:text-gray-400">Not found</span>
                                  }
                                </span>
                                @if (cell.value && stateWord(cell.state); as word) {
                                  <span class="pl-3 text-[11px]" [class]="verdictClass(cell.state)">
                                    {{ word }}
                                  </span>
                                }
                              </button>
                            } @else {
                              <span class="text-gray-500 dark:text-gray-400">—</span>
                            }
                          </td>
                        }
                      </tr>
                    }
                  </tbody>
                </table>
              </div>

              <div [id]="detailId" class="detail mt-2" aria-live="polite">
                @if (selectedCell(); as sel) {
                  <div
                    class="rounded-lg border border-gray-200/80 px-2.5 py-1.5 text-xs dark:border-white/10"
                  >
                    <p class="text-gray-600 dark:text-gray-300">
                      <span class="font-medium">{{ sel.field.title }}</span>
                      · {{ sel.run.filename || 'Document' }}:
                      {{ sel.field.value || 'Not found' }}
                    </p>
                    <p class="flex items-center gap-1.5">
                      <span [class]="dotClass(sel.field.state)" aria-hidden="true"></span>
                      <span class="text-[11px]" [class]="verdictClass(sel.field.state)">
                        {{ verdict(sel.field) }}
                      </span>
                    </p>
                    @if (sel.field.quote) {
                      <p class="text-[11px] text-gray-500 dark:text-gray-400">
                        <q>{{ sel.field.quote }}</q>
                      </p>
                    }
                  </div>
                } @else {
                  <p class="text-[11px] text-gray-500 dark:text-gray-400">
                    Select a value to see the passage it came from.
                  </p>
                }
              </div>
            }

            @if (quotesOmitted()) {
              <p class="mt-1.5 text-[11px] text-gray-500 dark:text-gray-400">
                Some passages were too long to keep with this conversation; the full table has them.
              </p>
            }

            <div
              class="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-gray-500 dark:text-gray-400"
            >
              <span>
                A source means the value appears in that passage, not that the passage assigns
                it to the field.
              </span>
              @if (modelId()) {
                <span>Extracted with {{ modelId() }}</span>
              }
              @if (artifactId()) {
                <button
                  type="button"
                  class="cursor-pointer font-medium text-primary-accessible underline-offset-2 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark"
                  (click)="openTable()"
                >
                  Open full table
                </button>
              }
            </div>
          </div>
        </div>
      </div>
    }
  `,
})
export class DocumentExtractionComponent {
  runs = input.required<readonly DocumentExtractionRun[]>();

  private readonly artifactState = inject(ArtifactStateService);

  private readonly uid = Math.random().toString(36).slice(2, 9);
  protected readonly panelId = `extraction-${this.uid}`;
  protected readonly detailId = `extraction-detail-${this.uid}`;

  /** The one run when the call covered a single document, else null. */
  protected readonly single = computed(() =>
    this.runs().length === 1 ? this.runs()[0] : null,
  );

  /** Fields in authored order, across every run (they share one field set). */
  protected readonly columns = computed<FieldColumn[]>(() => {
    const seen = new Map<string, FieldColumn>();
    for (const run of this.runs()) {
      for (const f of run.fields) {
        if (!seen.has(f.name)) seen.set(f.name, { name: f.name, title: f.title });
      }
    }
    return [...seen.values()];
  });

  protected readonly rows = computed<DocumentRow[]>(() => {
    const cols = this.columns();
    return this.runs().map(run => {
      const byName = new Map(run.fields.map(f => [f.name, f]));
      return { run, cells: cols.map(c => byName.get(c.name) ?? null) };
    });
  });

  protected readonly unconfirmedCount = computed(
    () =>
      this.runs().reduce(
        (n, run) => n + run.fields.filter(f => f.state === 'quote_unsupported').length,
        0,
      ),
  );

  protected readonly headline = computed(() => {
    const fields = this.columns().length;
    const what = fields === 1 ? '1 field' : `${fields} fields`;
    const one = this.single();
    if (one) {
      return one.filename ? `Extracted ${what} from ${one.filename}` : `Extracted ${what}`;
    }
    return `Extracted ${what} from ${this.runs().length} documents`;
  });

  /**
   * Said whenever any run's types were requested rather than guaranteed
   * (`schemaEnforced: false`), naming the documents when only some were.
   */
  protected readonly unenforcedNote = computed(() => {
    const unenforced = this.runs().filter(r => !r.schemaEnforced);
    if (!unenforced.length) return '';
    if (unenforced.length === this.runs().length) {
      return 'The field types were requested but not enforced for this run, so a value may not match its declared type.';
    }
    const names = unenforced.map(r => r.filename || 'a document').join(', ');
    return `The field types were requested but not enforced for ${names}, so a value there may not match its declared type.`;
  });

  protected readonly quotesOmitted = computed(() => this.runs().some(r => r.quotesOmitted));

  /** One model id when every run agrees, which is the normal case. */
  protected readonly modelId = computed(() => {
    const ids = new Set(this.runs().map(r => r.modelId).filter(Boolean));
    return ids.size === 1 ? [...ids][0] : '';
  });

  /** The call writes one artifact for the whole set, so every run names it. */
  protected readonly artifactId = computed(
    () => this.runs().find(r => r.artifactId)?.artifactId ?? null,
  );

  // Open by default: the table is the answer the user asked for.
  private readonly opened = signal(true);
  protected readonly isOpen = this.opened.asReadonly();

  protected toggle(): void {
    this.opened.update(v => !v);
  }

  /**
   * The cell whose passage is showing under the grid. Until the user picks
   * one, the first value the document does not support is shown, so the
   * warning is visible without a click.
   */
  private readonly picked = signal<CellRef | null>(null);
  protected readonly selectedCell = computed(() => {
    const ref = this.picked() ?? this.firstUnconfirmed();
    if (!ref) return null;
    const run = this.runs().find(r => r.uploadId === ref.uploadId);
    const field = run?.fields.find(f => f.name === ref.field);
    return run && field ? { run, field } : null;
  });

  private readonly firstUnconfirmed = computed<CellRef | null>(() => {
    for (const run of this.runs()) {
      const f = run.fields.find(x => x.state === 'quote_unsupported');
      if (f) return { uploadId: run.uploadId, field: f.name };
    }
    return null;
  });

  protected select(run: DocumentExtractionRun, field: DocumentExtractionField): void {
    this.picked.set({ uploadId: run.uploadId, field: field.name });
  }

  protected isSelected(run: DocumentExtractionRun, field: DocumentExtractionField): boolean {
    const sel = this.selectedCell();
    return !!sel && sel.run.uploadId === run.uploadId && sel.field.name === field.name;
  }

  protected cellLabel(run: DocumentExtractionRun, field: DocumentExtractionField): string {
    const value = field.value || 'not found';
    return `${field.title}, ${run.filename || 'document'}: ${value}, ${this.verdict(field)}`;
  }

  protected openTable(): void {
    const artifactId = this.artifactId();
    if (!artifactId) return;
    const known = this.artifactState.get(artifactId);
    this.artifactState.openArtifactPanel({
      artifactId,
      version: known?.version ?? 1,
      title: known?.title ?? this.headline(),
    });
  }

  /**
   * The words for a value's source. They carry the meaning — the dot only
   * reinforces them — and only `supported` reads as a plain citation.
   */
  protected verdict(field: DocumentExtractionField): string {
    if (!field.value) return 'the document does not answer this field';
    switch (field.state) {
      case 'supported':
        return `found on ${this.locator(field.locator)}`;
      case 'quote_unsupported':
        return `not in the quoted passage on ${this.locator(field.locator)}`;
      case 'unassessed':
        return field.locator
          ? `passage found on ${this.locator(field.locator)}, value not checked`
          : 'value not checked';
      default:
        return field.method === 'no_quote'
          ? 'no source passage given'
          : 'no matching passage in the document';
    }
  }

  /** The short state word under a grid cell; none for `supported`. */
  protected stateWord(state: FieldState): string {
    switch (state) {
      case 'quote_unsupported':
        return 'not in passage';
      case 'unassessed':
        return 'not checked';
      case 'unverified':
        return 'no source';
      default:
        return '';
    }
  }

  /** A locator in words, hedged with "about" when the position is estimated. */
  protected locator(loc: SourceLocator | null): string {
    if (!loc) return 'an unknown position';
    const about = loc.precision === 'estimated' ? 'about ' : '';
    const unit =
      loc.kind === 'page' ? 'page' : loc.kind === 'paragraph' ? 'paragraph' : 'line';
    return `${about}${unit} ${loc.value}`;
  }

  protected dotClass(state: FieldState): string {
    if (state === 'supported') return 'status-dot bg-state-success-500';
    if (state === 'quote_unsupported') return 'status-dot bg-state-danger-500';
    return 'status-dot bg-gray-400';
  }

  protected verdictClass(state: FieldState): string {
    if (state === 'quote_unsupported') {
      return 'font-medium text-state-danger-600 dark:text-state-danger-400';
    }
    return 'text-gray-500 dark:text-gray-400';
  }
}
