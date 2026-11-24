import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { DialogShellComponent } from '../../../components/dialog/dialog-shell.component';
import {
  ScriptFileRow,
  UnreviewedScriptSkill,
  fileSize,
  reviewDate,
  scriptsProvenanceLine,
  scriptsVersion,
} from '../script-review.model';

/** What the sheet is opened with. */
export interface ScriptReviewSheetData {
  skill: UnreviewedScriptSkill;
  /** The Agent's or the project's name. */
  scopeName: string;
  scope: 'agent' | 'project';
  /**
   * An Update (the pin moves onto a new script set) rather than a first binding. The
   * secondary button then keeps the reviewed version instead of binding without scripts.
   */
  isUpdate?: boolean;
  /** Reads one of the skill's files as text for View, or null when it can't be shown. */
  readFile?: (skillId: string, path: string) => Promise<string>;
}

/** `allow`: Allow scripts. `decline`: Add without scripts, or on an Update, keep the reviewed version. */
export type ScriptReviewSheetResult = 'allow' | 'decline';

/** Text files above this are offered as "too large to show here". */
const MAX_VIEW_BYTES = 200_000;

interface FileView {
  state: 'loading' | 'shown' | 'failed';
  text?: string;
}

const CHANGE_LABELS: Record<string, string> = { new: 'New', changed: 'Changed', removed: 'Removed' };

/**
 * The review sheet (docs/specs/skill-code-sandbox.md §8.5): shown when an Agent or
 * project save binds a skill whose scripts nobody has reviewed for it. People who use
 * the Agent or project never see a prompt, so this is the one place the decision is
 * made, by the person who binds the skill.
 */
@Component({
  selector: 'app-script-review-sheet',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent],
  host: { class: 'block' },
  template: `
    <app-dialog-shell [title]="title()" [description]="lede()" size="lg" (closed)="cancel()">
      <p class="text-sm/6 text-gray-700 dark:text-gray-300">{{ provenance() }}</p>
      <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">Version {{ version() }}</p>

      <ul class="mt-4 divide-y divide-gray-200 rounded-2xl border border-gray-200 dark:divide-gray-700 dark:border-gray-700" aria-label="Scripts">
        @for (row of shownScripts(); track row.path) {
          <li class="px-4 py-3">
            <div class="flex items-center gap-3">
              <code class="min-w-0 flex-1 truncate text-sm/6 text-gray-900 dark:text-gray-100">{{ row.path }}</code>
              @if (changeLabel(row); as label) {
                <span
                  class="shrink-0 rounded-full px-2 py-0.5 text-xs/5 font-medium"
                  [class]="row.change === 'removed'
                    ? 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-200'
                    : 'bg-state-warning-50 text-state-warning-800 dark:bg-state-warning-500/10 dark:text-state-warning-300'"
                >{{ label }}</span>
              }
              <span class="shrink-0 text-xs/5 text-gray-500 dark:text-gray-400">{{ size(row.size) }}</span>
              @if (canView(row)) {
                <button
                  type="button"
                  class="shrink-0 rounded-2xl px-2 py-1 text-sm/6 font-medium text-primary-accessible hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-700"
                  [attr.aria-expanded]="isOpen(row.path)"
                  [attr.aria-label]="(isOpen(row.path) ? 'Hide ' : 'View ') + row.path"
                  (click)="toggle(row.path, row.size)"
                >{{ isOpen(row.path) ? 'Hide' : 'View' }}</button>
              }
            </div>
            @if (views()[row.path]; as view) {
              @switch (view.state) {
                @case ('loading') {
                  <p class="mt-2 text-sm/6 text-gray-500 dark:text-gray-400" role="status">Loading…</p>
                }
                @case ('failed') {
                  <p class="mt-2 text-sm/6 text-state-danger-700 dark:text-state-danger-400">This file couldn't be shown here.</p>
                }
                @case ('shown') {
                  <pre class="mt-2 max-h-80 overflow-auto rounded-xl bg-gray-50 p-3 text-xs/5 text-gray-800 dark:bg-gray-900 dark:text-gray-200" tabindex="0" [attr.aria-label]="'Source of ' + row.path">{{ view.text }}</pre>
                }
              }
            }
          </li>
        }
        @if (unchangedCount()) {
          <li class="px-4 py-3 text-sm/6 text-gray-500 dark:text-gray-400">{{ unchangedCount() }} unchanged</li>
        }
      </ul>

      @if (data.skill.files.length) {
        <p class="mt-4 text-sm/6 text-gray-700 dark:text-gray-300">The scripts can also read the skill's other files:</p>
        <ul class="mt-1 space-y-1">
          @for (file of data.skill.files; track file.path) {
            <li class="flex items-center gap-3 text-sm/6">
              <code class="min-w-0 flex-1 truncate text-gray-700 dark:text-gray-300">{{ file.path }}</code>
              <span class="shrink-0 text-xs/5 text-gray-500 dark:text-gray-400">{{ size(file.size) }}</span>
              @if (data.readFile && file.size <= maxViewBytes) {
                <button
                  type="button"
                  class="shrink-0 rounded-2xl px-2 py-1 text-sm/6 font-medium text-primary-accessible hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-700"
                  [attr.aria-expanded]="isOpen(file.path)"
                  [attr.aria-label]="(isOpen(file.path) ? 'Hide ' : 'View ') + file.path"
                  (click)="toggle(file.path, file.size)"
                >{{ isOpen(file.path) ? 'Hide' : 'View' }}</button>
              }
            </li>
            @if (views()[file.path]; as view) {
              @if (view.state === 'shown') {
                <pre class="max-h-80 overflow-auto rounded-xl bg-gray-50 p-3 text-xs/5 text-gray-800 dark:bg-gray-900 dark:text-gray-200" tabindex="0" [attr.aria-label]="'Source of ' + file.path">{{ view.text }}</pre>
              } @else if (view.state === 'failed') {
                <p class="text-sm/6 text-state-danger-700 dark:text-state-danger-400">This file couldn't be shown here.</p>
              }
            }
          }
        </ul>
      }

      <div dialogFooter class="flex flex-col-reverse gap-3 border-t border-gray-200 px-6 py-4 sm:flex-row sm:justify-end dark:border-gray-700">
        <button
          type="button"
          cdkFocusInitial
          (click)="cancel()"
          class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
        >Cancel</button>
        <button
          type="button"
          (click)="decline()"
          class="rounded-2xl border border-gray-300 px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:border-gray-600 dark:text-gray-200 dark:hover:bg-gray-700"
        >{{ data.isUpdate ? 'Keep the reviewed version' : 'Add without scripts' }}</button>
        <button
          type="button"
          (click)="allow()"
          class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
        >Allow scripts</button>
      </div>
    </app-dialog-shell>
  `,
})
export class ScriptReviewSheetComponent {
  protected readonly dialogRef = inject<DialogRef<ScriptReviewSheetResult>>(DialogRef);
  protected readonly data = inject<ScriptReviewSheetData>(DIALOG_DATA);
  protected readonly maxViewBytes = MAX_VIEW_BYTES;

  protected readonly views = signal<Record<string, FileView>>({});

  /** Compared with a reviewed version: an Update whose scripts changed. */
  private readonly comparing = computed(() => this.data.skill.fromVersion !== null);

  private readonly changedCount = computed(
    () => this.data.skill.scripts.filter((s) => s.change !== 'same').length,
  );

  protected readonly title = computed(() => {
    if (this.comparing()) {
      const n = this.changedCount();
      const date = reviewDate(this.data.skill.reviewedAt);
      return `${n} ${n === 1 ? 'script' : 'scripts'} changed since you reviewed them${date ? ` on ${date}` : ''}`;
    }
    return `Review the scripts in ${this.data.skill.displayName}`;
  });

  protected readonly lede = computed(() => {
    const who =
      this.data.scope === 'project' ? `Anyone in ${this.data.scopeName}` : `Anyone who uses ${this.data.scopeName}`;
    const body =
      `${who} can run these scripts without being asked first. They run in a sandbox with no internet ` +
      "access, so they can't send anyone's files anywhere. They can still produce wrong or misleading " +
      "results, so allow only scripts you've checked or whose author you trust.";
    return this.comparing()
      ? `Until you allow this version of ${this.data.skill.displayName}, everyone keeps running the version you reviewed. ${body}`
      : body;
  });

  protected readonly provenance = computed(() => scriptsProvenanceLine(this.data.skill));
  protected readonly version = computed(() => scriptsVersion(this.data.skill.scriptsHash));

  /** On an Update, the unchanged scripts collapse to a count. */
  protected readonly shownScripts = computed<ScriptFileRow[]>(() =>
    this.comparing() ? this.data.skill.scripts.filter((s) => s.change !== 'same') : this.data.skill.scripts,
  );
  protected readonly unchangedCount = computed(() =>
    this.comparing() ? this.data.skill.scripts.filter((s) => s.change === 'same').length : 0,
  );

  protected changeLabel(row: ScriptFileRow): string | null {
    return this.comparing() ? CHANGE_LABELS[row.change] ?? null : null;
  }

  protected size(bytes: number): string {
    return fileSize(bytes);
  }

  protected canView(row: ScriptFileRow): boolean {
    return !!this.data.readFile && row.change !== 'removed' && row.size <= MAX_VIEW_BYTES;
  }

  protected isOpen(path: string): boolean {
    return !!this.views()[path];
  }

  protected async toggle(path: string, size: number): Promise<void> {
    if (this.views()[path]) {
      this.views.update((v) => {
        const { [path]: _, ...rest } = v;
        return rest;
      });
      return;
    }
    const read = this.data.readFile;
    if (!read || size > MAX_VIEW_BYTES) return;
    this.views.update((v) => ({ ...v, [path]: { state: 'loading' } }));
    try {
      const text = await read(this.data.skill.skillId, path);
      this.views.update((v) => (v[path] ? { ...v, [path]: { state: 'shown', text } } : v));
    } catch {
      this.views.update((v) => (v[path] ? { ...v, [path]: { state: 'failed' } } : v));
    }
  }

  protected allow(): void {
    this.dialogRef.close('allow');
  }

  protected decline(): void {
    this.dialogRef.close('decline');
  }

  protected cancel(): void {
    this.dialogRef.close(undefined);
  }
}
