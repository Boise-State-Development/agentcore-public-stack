import { Dialog } from '@angular/cdk/dialog';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowPath, heroArrowUpTray } from '@ng-icons/heroicons/outline';
import {
  ImportFileRow,
  ImportFileStatus,
  ImportApplyResult,
  ImportPreview,
  MAX_IMPORT_ZIP_BYTES,
  MySkill,
  SkillImportInput,
  SkillImportSource,
} from '../models/my-skill.model';
import { confirmSkillReach } from '../../../shared/skills/confirm-skill-reach';
import { reachConfirmationFrom } from '../../../shared/skills/skill-provenance';
import { MySkillService } from '../services/my-skill.service';
import { SkillMdDiagnosticsComponent } from '../../../shared/skills/skill-md-diagnostics.component';
import { ImportDiffComponent } from './import-diff.component';

type SourceMode = 'github' | 'zip';

const STATUS_LABELS: Record<ImportFileStatus, string> = {
  added: 'New',
  changed: 'Updated',
  unchanged: 'Unchanged',
  removed: 'Deleted',
  skipped: 'Skipped',
};

const STATUS_CLASSES: Record<ImportFileStatus, string> = {
  added: 'bg-state-success-50 text-state-success-800 dark:bg-state-success-900/20 dark:text-state-success-300',
  changed: 'bg-state-info-50 text-state-info-800 dark:bg-state-info-900/20 dark:text-state-info-300',
  unchanged: 'bg-gray-100 text-gray-700 dark:bg-gray-700 dark:text-gray-300',
  removed: 'bg-state-danger-50 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-300',
  skipped: 'bg-state-warning-50 text-state-warning-800 dark:bg-state-warning-900/20 dark:text-state-warning-200',
};

/**
 * Import a whole skill bundle — a `.zip` or a public GitHub path — and, on an
 * imported skill, refresh it from the same source (skill-bundle-import.md).
 *
 * Always two steps: **Preview** shows what the import adds, updates, deletes and
 * skips (and why), then **Import** applies exactly that. Apply sends the
 * preview's hash and the skill's `updatedAt`, so a push to the repository or an
 * edit in another tab since the preview is refused rather than written unseen.
 *
 * With no `skillId` it creates a skill; with one it re-imports into it, which
 * writes the live skill only. A Shared Project pinned to a version of the skill
 * keeps that version until someone clicks Update on the project's Skills page.
 *
 * Rendered by the skill form only when `features.skillBundleImport` is on.
 */
@Component({
  selector: 'app-skill-bundle-import',
  imports: [NgIcon, SkillMdDiagnosticsComponent, ImportDiffComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  viewProviders: [provideIcons({ heroArrowPath, heroArrowUpTray })],
  template: `
    <section
      aria-labelledby="bundle-import-heading"
      class="rounded-2xl border border-gray-200 p-4 dark:border-gray-700"
    >
      <h2 id="bundle-import-heading" class="text-sm/6 font-medium text-gray-900 dark:text-white">
        {{ skillId() ? 'Re-import from source' : 'Import a skill bundle' }}
      </h2>

      @if (importSource(); as source) {
        <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
          Imported from
          @if (source.type === 'github' && source.url) {
            <a
              [href]="source.url"
              target="_blank"
              rel="noopener noreferrer"
              class="font-medium text-primary-700 underline hover:no-underline dark:text-primary-400"
              >{{ source.owner }}/{{ source.repo }}{{ source.path ? '/' + source.path : '' }}</a
            >
            @if (source.commitSha) {
              at <code class="text-xs">{{ source.commitSha.slice(0, 7) }}</code>
            }
          } @else {
            {{ source.filename || 'a .zip file' }}
          }
          on {{ importedOn(source) }}.
        </p>
        @if (source.type === 'github') {
          <button
            type="button"
            (click)="refreshFromSource()"
            [disabled]="busy()"
            class="mt-3 inline-flex items-center gap-2 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            <ng-icon name="heroArrowPath" class="size-4" aria-hidden="true" />
            Check GitHub for changes
          </button>
        }
      } @else {
        <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
          @if (skillId()) {
            Replace this skill's description, instructions and files with a bundle's. Its name stays.
          } @else {
            A folder with a <code class="text-xs">SKILL.md</code> and its
            <code class="text-xs">references/</code>, <code class="text-xs">scripts/</code> and
            <code class="text-xs">assets/</code>, from a public GitHub repository or a .zip.
          }
        </p>
      }

      <div class="mt-4 flex gap-4" role="radiogroup" aria-label="Import from">
        @for (option of modes; track option.value) {
          <label class="flex items-center gap-2 text-sm/6 text-gray-700 dark:text-gray-300">
            <input
              type="radio"
              name="bundle-source"
              [value]="option.value"
              [checked]="mode() === option.value"
              (change)="setMode(option.value)"
              class="size-4 accent-primary-600"
            />
            {{ option.label }}
          </label>
        }
      </div>

      @if (mode() === 'github') {
        <div class="mt-3 flex flex-col gap-2 sm:flex-row">
          <label for="bundle-url" class="sr-only">GitHub URL</label>
          <input
            id="bundle-url"
            type="url"
            inputmode="url"
            placeholder="https://github.com/owner/repo/tree/main/skills/my-skill"
            [value]="url()"
            (input)="url.set($any($event.target).value)"
            (keydown.enter)="$event.preventDefault(); previewSource()"
            class="block w-full min-w-0 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          />
          <button
            type="button"
            (click)="previewSource()"
            [disabled]="busy() || !url().trim()"
            class="shrink-0 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            {{ busy() && !preview() ? 'Reading…' : 'Preview' }}
          </button>
        </div>
        <p class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">
          Public repositories only. Paste the repository, or the folder's URL from GitHub.
        </p>
      } @else {
        <div class="mt-3">
          <label
            for="bundle-zip"
            class="inline-flex cursor-pointer items-center gap-2 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
          >
            <ng-icon name="heroArrowUpTray" class="size-4" aria-hidden="true" />
            {{ zipName() ?? 'Choose a .zip' }}
          </label>
          <input
            id="bundle-zip"
            type="file"
            class="sr-only"
            accept=".zip,application/zip"
            (change)="onZipSelected($event)"
          />
        </div>
      }

      @if (error()) {
        <p role="alert" class="mt-3 text-sm/6 text-state-danger-700 dark:text-state-danger-400">
          {{ error() }}
        </p>
      }
      @if (notice()) {
        <p role="status" class="mt-3 text-sm/6 text-state-success-700 dark:text-state-success-400">
          {{ notice() }}
        </p>
      }

      @if (preview(); as p) {
        @if (p.candidates.length > 0) {
          <fieldset class="mt-4">
            <legend class="text-sm/6 font-medium text-gray-900 dark:text-white">
              This holds {{ p.candidates.length }} skills. Which one?
            </legend>
            <ul role="list" class="mt-2 flex flex-wrap gap-2">
              @for (candidate of p.candidates; track candidate) {
                <li>
                  <button
                    type="button"
                    (click)="chooseCandidate(candidate)"
                    [disabled]="busy()"
                    class="rounded-2xl border border-gray-300 bg-white px-3 py-1 font-mono text-xs/5 text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
                  >
                    {{ candidate || '(top level)' }}
                  </button>
                </li>
              }
            </ul>
          </fieldset>
        } @else if (p.skill; as skill) {
          <div class="mt-4 rounded-2xl bg-gray-50 p-4 dark:bg-gray-800/60">
            @if (!skillId()) {
              <label for="bundle-name" class="block text-sm/6 font-medium text-gray-900 dark:text-white">
                Name
              </label>
              <input
                id="bundle-name"
                type="text"
                maxlength="100"
                [value]="displayName()"
                (input)="displayName.set($any($event.target).value)"
                class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
              />
            }
            <p class="mt-2 text-sm/6 text-gray-700 dark:text-gray-300">{{ skill.description }}</p>
            <p class="mt-2 text-xs/5 text-gray-600 dark:text-gray-400">
              Activation name: <code class="text-xs">{{ skill.storedName }}</code>
            </p>
            @if (commitRange(); as range) {
              <p class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">
                Commit <code class="text-xs">{{ range.from }}</code> →
                <code class="text-xs">{{ range.to }}</code>
              </p>
            }
            @if (fieldChanges().length > 0) {
              <p class="mt-2 text-sm/6 text-gray-700 dark:text-gray-300">
                Changes: {{ fieldChanges().join(', ') }}.
              </p>
            }
            <app-import-diff label="Description" [diff]="p.descriptionDiff" />
            <app-import-diff label="Instructions (SKILL.md)" [diff]="p.instructionsDiff" />
            <app-skill-md-diagnostics
              headingId="bundle-import-diagnostics"
              subject="this bundle"
              [diagnostics]="p.diagnostics ?? []"
            />

            <h3 class="mt-4 text-sm/6 font-medium text-gray-900 dark:text-white">Files</h3>
            @if (p.files.length === 0) {
              <p class="text-sm/6 text-gray-600 dark:text-gray-400">Only a SKILL.md.</p>
            } @else {
              <ul role="list" class="mt-1 divide-y divide-gray-200 dark:divide-gray-700">
                @for (row of p.files; track row.path) {
                  <li class="flex items-start justify-between gap-3 py-1.5">
                    <span class="min-w-0 text-sm/6">
                      <span class="block truncate font-mono text-xs/5 text-gray-900 dark:text-white">
                        {{ row.path }}
                      </span>
                      @if (row.reason) {
                        <span class="block text-xs/5 text-gray-600 dark:text-gray-400">{{ row.reason }}</span>
                      } @else if (row.filename && row.filename !== basename(row.path)) {
                        <span class="block text-xs/5 text-gray-600 dark:text-gray-400">
                          stored as {{ row.filename }}
                        </span>
                      }
                    </span>
                    <span
                      class="shrink-0 rounded-full px-2 py-0.5 text-xs font-medium"
                      [class]="statusClass(row)"
                    >
                      {{ statusLabel(row) }}
                    </span>
                  </li>
                }
              </ul>
            }
            @if (removedCount() > 0) {
              <p class="mt-3 text-sm/6 text-state-danger-700 dark:text-state-danger-400">
                {{ removedCount() }} {{ removedCount() === 1 ? 'file is' : 'files are' }} not in the
                bundle and will be deleted from this skill.
              </p>
            }
            @if (skillId()) {
              <p class="mt-3 text-xs/5 text-gray-600 dark:text-gray-400">
                Projects that use this skill keep their current version until someone clicks Update
                on the project's Skills page.
              </p>
            }

            <div class="mt-4 flex items-center gap-3">
              <button
                type="button"
                (click)="apply()"
                [disabled]="busy() || nothingToApply()"
                class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50"
              >
                {{ busy() ? 'Importing…' : skillId() ? 'Re-import' : 'Import skill' }}
              </button>
              <button
                type="button"
                (click)="reset()"
                class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-300 dark:hover:bg-gray-700"
              >
                Cancel
              </button>
            </div>
          </div>
        }
      }
    </section>
  `,
})
export class SkillBundleImportComponent {
  private readonly skills = inject(MySkillService);
  private readonly dialog = inject(Dialog);

  /** The skill to re-import into; null creates a new skill. */
  readonly skillId = input<string | null>(null);
  /** The skill's last import, when it has one. */
  readonly importSource = input<SkillImportSource | null>(null);
  /** The created or re-imported skill. */
  readonly imported = output<MySkill>();

  protected readonly modes: { value: SourceMode; label: string }[] = [
    { value: 'github', label: 'GitHub' },
    { value: 'zip', label: '.zip file' },
  ];

  protected readonly mode = signal<SourceMode>('github');
  protected readonly url = signal('');
  private readonly zip = signal<File | null>(null);
  /** The path the user picked, sent explicitly so the URL's never overrides it. */
  private readonly path = signal<string | null>(null);
  protected readonly displayName = signal('');
  protected readonly preview = signal<ImportPreview | null>(null);
  protected readonly busy = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly notice = signal<string | null>(null);

  protected readonly zipName = computed(() => this.zip()?.name ?? null);

  protected readonly removedCount = computed(
    () => this.preview()?.files.filter((f) => f.status === 'removed').length ?? 0,
  );

  protected readonly fieldChanges = computed(() => {
    const changes = this.preview()?.changes;
    if (!changes) {
      return [];
    }
    const out: string[] = [];
    if (changes.description) out.push('description');
    if (changes.instructions) out.push('instructions');
    if (changes.allowedTools) out.push('tools it mentions');
    if (changes.skillMetadata) out.push('frontmatter');
    return out;
  });

  /** Short SHAs of the last import and this one, when both are known and differ. */
  protected readonly commitRange = computed(() => {
    const p = this.preview();
    const from = p?.previousCommitSha;
    const to = p?.source.commitSha;
    if (!from || !to || from === to) {
      return null;
    }
    return { from: from.slice(0, 7), to: to.slice(0, 7) };
  });

  /** A re-import whose every file is unchanged and no field moved. */
  protected readonly nothingToApply = computed(() => {
    const p = this.preview();
    if (!p || !this.skillId()) {
      return false;
    }
    return (
      this.fieldChanges().length === 0 &&
      p.files.every((f) => f.status === 'unchanged' || f.status === 'skipped')
    );
  });

  protected setMode(mode: SourceMode): void {
    this.mode.set(mode);
    this.reset();
  }

  protected onZipSelected(event: Event): void {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0] ?? null;
    input.value = '';
    if (!file) {
      return;
    }
    if (file.size > MAX_IMPORT_ZIP_BYTES) {
      this.error.set('That .zip is larger than 10 MB.');
      return;
    }
    this.zip.set(file);
    this.path.set(null);
    void this.previewSource();
  }

  /** Preview the same GitHub source the skill was imported from. */
  protected refreshFromSource(): void {
    const source = this.importSource();
    if (!source?.url) {
      return;
    }
    this.mode.set('github');
    this.url.set(source.url);
    this.path.set(source.path ?? '');
    void this.previewSource();
  }

  protected chooseCandidate(candidate: string): void {
    this.path.set(candidate);
    void this.previewSource();
  }

  protected async previewSource(): Promise<void> {
    const input = this.input();
    if (!input) {
      return;
    }
    this.busy.set(true);
    this.error.set(null);
    this.notice.set(null);
    try {
      const preview = await this.skills.previewImport(input, this.skillId());
      this.preview.set(preview);
      if (preview.skill && !this.skillId()) {
        this.displayName.set(preview.skill.displayName);
      }
      if (this.nothingToApply()) {
        this.notice.set('Already up to date.');
      }
    } catch (err) {
      this.preview.set(null);
      this.error.set(this.skills.describeError(err, "We couldn't read that bundle."));
    } finally {
      this.busy.set(false);
    }
  }

  protected async apply(): Promise<void> {
    const p = this.preview();
    const input = this.input();
    if (!p?.skill || !input) {
      return;
    }
    // Apply the bundle the preview showed, even when the URL named another path.
    const chosen: SkillImportInput = { ...input, path: p.source.path };
    this.busy.set(true);
    this.error.set(null);
    try {
      const id = this.skillId();
      const result = id
        ? await this.reimportConfirmingReach(id, chosen, p)
        : await this.skills.importSkill(chosen, {
            displayName: this.displayName().trim() || undefined,
            expectedBundleHash: p.bundleHash,
          });
      if (result === null) {
        return;
      }
      this.preview.set(null);
      this.notice.set(id ? 'Re-imported.' : null);
      this.imported.emit(result.skill);
    } catch (err) {
      this.error.set(this.skills.describeError(err, "We couldn't import that bundle."));
    } finally {
      this.busy.set(false);
    }
  }

  /**
   * Re-import, asking first when the server reports the change would reach other
   * people's agents or projects (a 409 with the reach summary). Null when the user
   * keeps the current version: nothing is written and the preview stays open.
   */
  private async reimportConfirmingReach(
    id: string,
    chosen: SkillImportInput,
    p: ImportPreview,
  ): Promise<ImportApplyResult | null> {
    const options = { expectedBundleHash: p.bundleHash, expectedUpdatedAt: p.targetUpdatedAt };
    try {
      return await this.skills.reimportSkill(id, chosen, options);
    } catch (err) {
      const reach = reachConfirmationFrom(err);
      if (reach === null) throw err;
      if (!(await confirmSkillReach(this.dialog, reach))) return null;
      return this.skills.reimportSkill(id, chosen, { ...options, confirmReach: true });
    }
  }

  protected reset(): void {
    this.preview.set(null);
    this.error.set(null);
    this.notice.set(null);
    this.path.set(null);
    this.zip.set(null);
  }

  protected statusLabel(row: ImportFileRow): string {
    return STATUS_LABELS[row.status];
  }

  protected statusClass(row: ImportFileRow): string {
    return STATUS_CLASSES[row.status];
  }

  protected basename(path: string): string {
    return path.slice(path.lastIndexOf('/') + 1);
  }

  protected importedOn(source: SkillImportSource): string {
    const when = new Date(source.importedAt);
    return Number.isNaN(when.getTime()) ? source.importedAt : when.toLocaleDateString();
  }

  private input(): SkillImportInput | null {
    const path = this.path();
    if (this.mode() === 'zip') {
      const file = this.zip();
      return file ? { file, path } : null;
    }
    const url = this.url().trim();
    return url ? { url, path } : null;
  }
}
