import { Dialog } from '@angular/cdk/dialog';
import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  untracked,
} from '@angular/core';
import { firstValueFrom } from 'rxjs';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog/confirmation-dialog.component';
import { confirmSkillReach } from './confirm-skill-reach';
import { ImportDiffComponent } from './import-diff.component';
import { ImportFileRow } from './skill-bundle-import.model';
import { saveBlob, skillExportErrorMessage, skillExportSlug } from './skill-export';
import { reachConfirmationFrom } from './skill-provenance';
import {
  SkillRestoreResult,
  SkillVersionDetail,
  SkillVersionHistoryApi,
  SkillVersionSummary,
  VersionGroup,
  groupVersions,
  versionAuthorLabel,
  versionTriggerLabel,
} from './skill-version-history.model';

const PAGE = 20;

const CHANGE_LABELS: Record<string, string> = {
  added: 'Added back',
  changed: 'Changed',
  removed: 'Removed',
};

const CHANGE_CLASSES: Record<string, string> = {
  added: 'bg-state-success-50 text-state-success-800 dark:bg-state-success-900/20 dark:text-state-success-300',
  changed: 'bg-state-info-50 text-state-info-800 dark:bg-state-info-900/20 dark:text-state-info-300',
  removed: 'bg-state-danger-50 text-state-danger-800 dark:bg-state-danger-900/20 dark:text-state-danger-300',
};

/**
 * A skill's History section (docs/specs/skill-version-history.md §6.2): every
 * version newest first, what restoring one would change, a download of it, and
 * Restore.
 *
 * A restore never moves the history backwards: it saves the chosen version's
 * content as a **new** version, so the content it replaces stays in the list
 * and can be restored in turn (D3). That is why Restore is offered on every row
 * but the one matching the live skill, and why the dialog names the number the
 * restore will get.
 *
 * Tier-neutral like `SkillBundleImportComponent`: the tier's service comes in as
 * `api` (`MySkillService` on Customize, `AdminSkillService` on the admin form),
 * and `restored` emits that tier's skill. The host passes the live skill's
 * `updatedAt`; a change to it (a save, an import, a restore) reloads the list.
 *
 * Rendered by both skill pages only when `features.skillVersionHistory` is on.
 */
@Component({
  selector: 'app-skill-version-history',
  imports: [ImportDiffComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section
      aria-labelledby="skill-history-heading"
      class="rounded-2xl border border-gray-200 p-4 dark:border-gray-700"
    >
      <h2 id="skill-history-heading" class="text-sm/6 font-medium text-gray-900 dark:text-white">
        History
      </h2>
      <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
        Every change to this skill is kept. Restoring a version saves it as a new version, so
        nothing is lost. Projects and agents pinned to a version keep it.
      </p>

      @if (notice()) {
        <p role="status" class="mt-3 text-sm/6 text-state-success-700 dark:text-state-success-400">
          {{ notice() }}
        </p>
      }
      @if (error()) {
        <p role="alert" class="mt-3 text-sm/6 text-state-danger-700 dark:text-state-danger-400">
          {{ error() }}
        </p>
      }

      @if (rows().length === 0 && loading()) {
        <!-- First load: rows shaped like the real ones, so nothing moves when they land. -->
        <span role="status" class="sr-only">Loading history</span>
        <ul aria-hidden="true" class="mt-3 divide-y divide-gray-200 dark:divide-gray-700">
          @for (n of skeletonRows; track n) {
            <li class="py-2">
              <div class="flex items-baseline justify-between gap-x-3 px-2 py-1">
                <span
                  class="h-4 w-24 animate-pulse rounded-full bg-gray-200 motion-reduce:animate-none dark:bg-gray-700"
                ></span>
                <span
                  class="h-3 w-48 animate-pulse rounded-full bg-gray-200 motion-reduce:animate-none dark:bg-gray-700"
                ></span>
              </div>
            </li>
          }
        </ul>
      } @else if (rows().length === 0) {
        <p class="mt-3 text-sm/6 text-gray-600 dark:text-gray-400">
          No versions yet. The next change to this skill starts its history.
        </p>
      } @else {
        <ul role="list" class="mt-3 divide-y divide-gray-200 dark:divide-gray-700">
          @for (group of groups(); track group.key) {
            @if (group.rows.length > 1 && !isExpanded(group.key)) {
              <li class="py-2">
                <button
                  type="button"
                  (click)="expand(group.key)"
                  aria-expanded="false"
                  class="flex w-full flex-wrap items-baseline justify-between gap-x-3 rounded-2xl px-2 py-1 text-left text-sm/6 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-800"
                >
                  <span class="font-medium text-gray-900 dark:text-white">
                    {{ group.rows.length }} {{ groupNoun(group) }} · versions
                    {{ oldest(group) }}–{{ group.key }}
                  </span>
                  <span class="text-xs/5 text-gray-600 dark:text-gray-400">{{ meta(group.rows[0]) }}</span>
                </button>
              </li>
            } @else {
              @for (row of group.rows; track row.version) {
                <li class="py-2">
                  <button
                    type="button"
                    (click)="toggle(row.version)"
                    [attr.aria-expanded]="open() === row.version"
                    [attr.aria-controls]="'skill-version-' + row.version"
                    class="flex w-full flex-wrap items-baseline justify-between gap-x-3 rounded-2xl px-2 py-1 text-left text-sm/6 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-gray-800"
                  >
                    <span class="flex items-center gap-2 font-medium text-gray-900 dark:text-white">
                      Version {{ row.version }}
                      @if (row.isCurrent) {
                        <span
                          class="rounded-full bg-primary-50 px-2 py-0.5 text-xs font-medium text-primary-800 dark:bg-primary-900/30 dark:text-primary-200"
                          >Current</span
                        >
                      }
                    </span>
                    <span class="text-xs/5 text-gray-600 dark:text-gray-400">{{ meta(row) }}</span>
                  </button>

                  @if (open() === row.version) {
                    <div
                      [id]="'skill-version-' + row.version"
                      class="mt-2 rounded-2xl bg-gray-50 p-4 dark:bg-gray-800/60"
                    >
                      @if (detailLoading()) {
                        <p class="text-sm/6 text-gray-600 dark:text-gray-400">Loading version…</p>
                      } @else if (detail(); as d) {
                        @if (d.isCurrent) {
                          <p class="text-sm/6 text-gray-700 dark:text-gray-300">
                            This is the skill as it is now.
                          </p>
                        } @else {
                          <p class="text-sm/6 text-gray-700 dark:text-gray-300">
                            Restoring version {{ d.version }} would change:
                          </p>
                          @if (d.displayName !== currentName()) {
                            <p class="mt-1 text-sm/6 text-gray-700 dark:text-gray-300">
                              Name: {{ d.displayName }}
                            </p>
                          }
                          <app-import-diff label="Description" [diff]="d.descriptionDiff" />
                          <app-import-diff label="Instructions (SKILL.md)" [diff]="d.instructionsDiff" />
                          @if (changedFiles(d).length > 0) {
                            <h3 class="mt-3 text-sm/6 font-medium text-gray-900 dark:text-white">Files</h3>
                            <ul role="list" class="mt-1 divide-y divide-gray-200 dark:divide-gray-700">
                              @for (file of changedFiles(d); track file.path) {
                                <li class="flex items-center justify-between gap-3 py-1.5">
                                  <span class="min-w-0 truncate font-mono text-xs/5 text-gray-900 dark:text-white">
                                    {{ file.path }}
                                  </span>
                                  <span
                                    class="shrink-0 rounded-full px-2 py-0.5 text-xs font-medium"
                                    [class]="changeClass(file)"
                                    >{{ changeLabel(file) }}</span
                                  >
                                </li>
                              }
                            </ul>
                          } @else {
                            <p class="mt-2 text-sm/6 text-gray-600 dark:text-gray-400">Files: no change.</p>
                          }
                        }
                        <div class="mt-4 flex flex-wrap items-center gap-3">
                          @if (!d.isCurrent) {
                            <button
                              type="button"
                              (click)="restore(d.version)"
                              [disabled]="busy()"
                              class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50"
                            >
                              {{ busy() ? 'Restoring…' : 'Restore this version' }}
                            </button>
                          }
                          <button
                            type="button"
                            (click)="download(d.version)"
                            [disabled]="busy()"
                            class="rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
                          >
                            Download this version
                          </button>
                        </div>
                      }
                    </div>
                  }
                </li>
              }
            }
          }
        </ul>
        @if (nextCursor()) {
          <button
            type="button"
            (click)="loadMore()"
            [disabled]="loading()"
            class="mt-3 rounded-2xl px-3 py-1.5 text-sm/6 font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50 dark:text-primary-accessible-dark"
          >
            {{ loading() ? 'Loading…' : 'Show older versions' }}
          </button>
        }
      }
    </section>
  `,
})
export class SkillVersionHistoryComponent<TSkill = unknown> {
  private readonly dialog = inject(Dialog);

  /** The tier's history routes. */
  readonly api = input.required<SkillVersionHistoryApi<TSkill>>();
  readonly skillId = input.required<string>();
  /** The live skill's `updatedAt`, sent with a restore; a change reloads the list. */
  readonly updatedAt = input<string | null>(null);
  /** The live skill's name, so a detail can say when restoring would rename it. */
  readonly currentName = input<string>('');
  /** The restored skill, for the host to show. */
  readonly restored = output<TSkill>();

  protected readonly rows = signal<SkillVersionSummary[]>([]);
  protected readonly nextCursor = signal<number | null>(null);
  protected readonly loading = signal(false);
  protected readonly open = signal<number | null>(null);
  protected readonly detail = signal<SkillVersionDetail | null>(null);
  protected readonly detailLoading = signal(false);
  protected readonly busy = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly notice = signal<string | null>(null);
  private readonly expanded = signal<ReadonlySet<number>>(new Set());

  protected readonly groups = computed(() => groupVersions(this.rows()));
  protected readonly skeletonRows = [1, 2, 3];

  constructor() {
    effect(() => {
      this.skillId();
      this.updatedAt();
      untracked(() => void this.load(true));
    });
  }

  protected meta(row: SkillVersionSummary): string {
    return [versionTriggerLabel(row), versionAuthorLabel(row), this.when(row.createdAt)]
      .filter(Boolean)
      .join(' · ');
  }

  protected groupNoun(group: VersionGroup): string {
    return group.rows[0].trigger === 'authoring' ? 'changes by an agent' : 'file changes';
  }

  protected oldest(group: VersionGroup): number {
    return group.rows[group.rows.length - 1].version;
  }

  protected isExpanded(key: number): boolean {
    return this.expanded().has(key);
  }

  protected expand(key: number): void {
    this.expanded.update((keys) => new Set([...keys, key]));
  }

  protected changedFiles(detail: SkillVersionDetail): ImportFileRow[] {
    return detail.fileChanges.filter((f) => f.status !== 'unchanged');
  }

  protected changeLabel(row: ImportFileRow): string {
    return CHANGE_LABELS[row.status] ?? row.status;
  }

  protected changeClass(row: ImportFileRow): string {
    return CHANGE_CLASSES[row.status] ?? '';
  }

  protected async loadMore(): Promise<void> {
    await this.load(false);
  }

  protected async toggle(version: number): Promise<void> {
    if (this.open() === version) {
      this.open.set(null);
      this.detail.set(null);
      return;
    }
    this.open.set(version);
    this.detail.set(null);
    this.detailLoading.set(true);
    this.error.set(null);
    try {
      const detail = await this.api().getVersion(this.skillId(), version);
      if (this.open() === version) {
        this.detail.set(detail);
      }
    } catch (err) {
      this.error.set(this.api().describeError(err, "We couldn't load that version."));
      this.open.set(null);
    } finally {
      this.detailLoading.set(false);
    }
  }

  protected async restore(version: number): Promise<void> {
    const next = (this.rows()[0]?.version ?? version) + 1;
    const ref = this.dialog.open<boolean, ConfirmationDialogData>(ConfirmationDialogComponent, {
      data: {
        title: `Restore version ${version}?`,
        message:
          `Version ${version}'s instructions, files and name become the current skill, ` +
          `saved as version ${next}. The current content stays in the history, and ` +
          'projects and agents pinned to a version keep it.',
        confirmText: 'Restore',
        cancelText: 'Cancel',
      },
    });
    if ((await firstValueFrom(ref.closed)) !== true) {
      return;
    }
    this.busy.set(true);
    this.error.set(null);
    this.notice.set(null);
    try {
      const result = await this.restoreConfirmingReach(version);
      if (result === null) {
        return;
      }
      const saved = result.version?.version;
      let message = saved
        ? `Restored version ${version} as version ${saved}.`
        : `Restored version ${version}.`;
      if (!result.importSourceRestored) {
        message += ' Its import source was left as it is now.';
      }
      this.notice.set(message);
      this.open.set(null);
      this.detail.set(null);
      this.restored.emit(result.skill);
    } catch (err) {
      this.error.set(this.api().describeError(err, "We couldn't restore that version."));
    } finally {
      this.busy.set(false);
    }
  }

  protected async download(version: number): Promise<void> {
    this.busy.set(true);
    this.error.set(null);
    try {
      const blob = await this.api().exportSkill(this.skillId(), version);
      saveBlob(blob, `${skillExportSlug(this.skillId())}-v${version}.zip`);
    } catch (err) {
      this.error.set(await skillExportErrorMessage(err));
    } finally {
      this.busy.set(false);
    }
  }

  /**
   * Restore, asking first when the server reports the change would reach other
   * people's agents or projects (My Skills' 409 with the reach summary). Null
   * when the user keeps the current version: nothing was written.
   */
  private async restoreConfirmingReach(version: number): Promise<SkillRestoreResult<TSkill> | null> {
    const options = { expectedUpdatedAt: this.updatedAt() };
    try {
      return await this.api().restoreVersion(this.skillId(), version, options);
    } catch (err) {
      const reach = reachConfirmationFrom(err);
      if (reach === null) throw err;
      if (!(await confirmSkillReach(this.dialog, reach))) return null;
      return this.api().restoreVersion(this.skillId(), version, { ...options, confirmReach: true });
    }
  }

  private async load(reset: boolean): Promise<void> {
    const skillId = this.skillId();
    this.loading.set(true);
    if (reset) {
      this.error.set(null);
    }
    try {
      const page = await this.api().listVersions(skillId, {
        limit: PAGE,
        before: reset ? null : this.nextCursor(),
      });
      if (skillId !== this.skillId()) {
        return;
      }
      this.rows.update((rows) => (reset ? page.versions : [...rows, ...page.versions]));
      this.nextCursor.set(page.nextCursor ?? null);
      if (reset) {
        this.open.set(null);
        this.detail.set(null);
      }
    } catch (err) {
      this.error.set(this.api().describeError(err, "We couldn't load this skill's history."));
    } finally {
      this.loading.set(false);
    }
  }

  private when(iso: string | null | undefined): string | null {
    if (!iso) return null;
    const date = new Date(iso);
    return Number.isNaN(date.getTime())
      ? null
      : date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  }
}
