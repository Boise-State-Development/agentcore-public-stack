import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  input,
  output,
  signal,
  untracked,
} from '@angular/core';
import {
  SkillUse,
  SkillUseUpdateResult,
  SkillUses,
  SkillUsesApi,
  useKey,
} from './skill-uses.model';

/**
 * Where a skill is used, split so nobody has to guess whether a change reached a
 * place (docs/specs/skill-version-history.md D9):
 *
 * 1. **Using the current version** — unpinned places, and those pinned to the
 *    live content. Listed so the author knows who just got the change.
 * 2. **On an older version** — pinned places the author can update, each a
 *    checkbox (all ticked) with **Update selected**.
 * 3. **Others on an older version** — a count; their editors see Update available.
 *
 * Two modes: `section`, the skill page's **Where it's used**, which loads its own
 * data and reloads when `refreshKey` changes; and `offer`, the body of the dialog
 * that opens after a change (`offerPinUpdates`), given the uses it was opened for
 * and adding **Not now**. A pin never moves without a click.
 */
@Component({
  selector: 'app-skill-uses',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <section
      [attr.aria-labelledby]="mode() === 'section' ? 'skill-uses-heading' : null"
      [class]="mode() === 'section' ? 'rounded-2xl border border-gray-200 p-4 dark:border-gray-700' : ''"
    >
      @if (mode() === 'section') {
        <h2 id="skill-uses-heading" class="text-sm/6 font-medium text-gray-900 dark:text-white">
          Where it's used
        </h2>
        <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
          Agents and projects that don't pin a version run your latest change. Pinned ones
          keep their version until someone updates them.
        </p>
      }

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

      @if (!uses() && loading()) {
        <span role="status" class="sr-only">Loading where this skill is used</span>
        <div aria-hidden="true" class="mt-3 flex flex-col gap-2">
          @for (n of skeletonRows; track n) {
            <span
              class="h-4 w-56 animate-pulse rounded-full bg-gray-200 motion-reduce:animate-none dark:bg-gray-700"
            ></span>
          }
        </div>
      } @else if (uses(); as u) {
        @if (!u.complete) {
          <p class="mt-3 text-sm/6 text-state-warning-800 dark:text-state-warning-200">
            We couldn't check every agent and project, so this list may be incomplete.
          </p>
        }
        @if (u.current.length === 0 && u.behind.length === 0 && u.othersBehind === 0) {
          <p class="mt-3 text-sm/6 text-gray-600 dark:text-gray-400">
            None of your agents or projects use this skill.
          </p>
        }

        @if (u.behind.length > 0) {
          <fieldset class="mt-4">
            <legend class="text-sm/6 font-medium text-gray-900 dark:text-white">
              On an older version
            </legend>
            <ul role="list" class="mt-2 flex flex-col gap-2">
              @for (use of u.behind; track key(use)) {
                <li>
                  <label class="flex items-start gap-3 text-sm/6 text-gray-700 dark:text-gray-300">
                    <input
                      type="checkbox"
                      class="mt-1 size-4 accent-primary-600"
                      [checked]="isSelected(use)"
                      [disabled]="busy()"
                      (change)="toggle(use)"
                    />
                    <span class="min-w-0">
                      <span class="font-medium text-gray-900 dark:text-white">{{ use.name }}</span>
                      · {{ kindLabel(use) }} · on version {{ use.pinnedVersion }}
                      @if (use.published) {
                        <span class="block text-xs/5 text-gray-600 dark:text-gray-400">
                          Updates its draft. Resubmit the agent so the change reaches the people using it.
                        </span>
                      }
                      @if (resultFor(use); as r) {
                        <span
                          class="block text-xs/5"
                          [class]="r.ok ? 'text-state-success-700 dark:text-state-success-400' : 'text-state-danger-700 dark:text-state-danger-400'"
                        >
                          {{ r.ok ? 'Updated' : r.error || "Couldn't update" }}
                        </span>
                      }
                    </span>
                  </label>
                </li>
              }
            </ul>
          </fieldset>
          <div class="mt-4 flex flex-wrap items-center gap-3">
            <button
              type="button"
              (click)="update()"
              [disabled]="busy() || selectedCount() === 0"
              class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50"
            >
              {{ busy() ? 'Updating…' : 'Update selected' }}
            </button>
            @if (mode() === 'offer') {
              <button
                type="button"
                (click)="done.emit()"
                class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-300 dark:hover:bg-gray-700"
              >
                Not now
              </button>
            }
          </div>
        }

        @if (u.othersBehind > 0) {
          <p class="mt-4 text-sm/6 text-gray-600 dark:text-gray-400">
            {{ u.othersBehind }} more {{ u.othersBehind === 1 ? 'place pins' : 'places pin' }} an
            older version that isn't yours to update. Their editors see Update available.
          </p>
        }

        @if (u.current.length > 0) {
          <h3 class="mt-4 text-sm/6 font-medium text-gray-900 dark:text-white">
            Using the current version
          </h3>
          <ul role="list" class="mt-1 flex flex-col gap-1">
            @for (use of u.current; track key(use)) {
              <li class="text-sm/6 text-gray-700 dark:text-gray-300">
                <span class="font-medium text-gray-900 dark:text-white">{{ use.name }}</span>
                · {{ kindLabel(use) }}
              </li>
            }
          </ul>
        }
      }
    </section>
  `,
})
export class SkillUsesComponent {
  readonly api = input.required<SkillUsesApi>();
  readonly skillId = input.required<string>();
  readonly mode = input<'section' | 'offer'>('section');
  /** `offer`: the uses the dialog was opened for, so it opens without a second read. */
  readonly initial = input<SkillUses | null>(null);
  /** `section`: a change (the skill's `updatedAt`) reloads the list. */
  readonly refreshKey = input<string | null>(null);
  /** `offer`: the author is finished, by updating or by Not now. */
  readonly done = output<void>();

  protected readonly uses = signal<SkillUses | null>(null);
  protected readonly loading = signal(false);
  protected readonly busy = signal(false);
  protected readonly error = signal<string | null>(null);
  protected readonly notice = signal<string | null>(null);
  private readonly selected = signal<ReadonlySet<string>>(new Set());
  private readonly results = signal<ReadonlyMap<string, SkillUseUpdateResult>>(new Map());
  protected readonly skeletonRows = [1, 2];

  protected readonly selectedCount = computed(() => this.selected().size);

  constructor() {
    effect(() => {
      const initial = this.initial();
      this.skillId();
      this.refreshKey();
      untracked(() => {
        if (initial) {
          this.show(initial);
        } else {
          void this.load();
        }
      });
    });
  }

  protected key(use: SkillUse): string {
    return useKey(use);
  }

  protected kindLabel(use: SkillUse): string {
    return use.kind === 'project' ? 'project' : use.shared ? 'shared agent' : 'agent';
  }

  protected isSelected(use: SkillUse): boolean {
    return this.selected().has(useKey(use));
  }

  protected toggle(use: SkillUse): void {
    const key = useKey(use);
    this.selected.update((keys) => {
      const next = new Set(keys);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  protected resultFor(use: SkillUse): SkillUseUpdateResult | undefined {
    return this.results().get(useKey(use));
  }

  protected async update(): Promise<void> {
    const targets = (this.uses()?.behind ?? [])
      .filter((u) => this.selected().has(useKey(u)))
      .map((u) => ({ kind: u.kind, id: u.id }));
    if (targets.length === 0) return;
    this.busy.set(true);
    this.error.set(null);
    this.notice.set(null);
    try {
      const results = await this.api().updateUses(this.skillId(), targets);
      this.results.set(new Map(results.map((r) => [useKey(r), r])));
      const ok = results.filter((r) => r.ok).length;
      const failed = results.length - ok;
      this.notice.set(
        failed === 0
          ? `Updated ${ok} ${ok === 1 ? 'place' : 'places'} to the current version.`
          : `Updated ${ok} of ${results.length}. The others say why below.`,
      );
      if (failed === 0 && this.mode() === 'offer') {
        this.done.emit();
        return;
      }
      await this.load(true);
    } catch (err) {
      this.error.set(this.api().describeError(err, "We couldn't update those places."));
    } finally {
      this.busy.set(false);
    }
  }

  private async load(keepResults = false): Promise<void> {
    const skillId = this.skillId();
    this.loading.set(true);
    try {
      const uses = await this.api().getUses(skillId);
      if (skillId === this.skillId()) {
        this.show(uses, keepResults);
      }
    } catch (err) {
      this.error.set(this.api().describeError(err, "We couldn't check where this skill is used."));
    } finally {
      this.loading.set(false);
    }
  }

  /** Every place behind starts ticked; a failed update's row keeps its message. */
  private show(uses: SkillUses, keepResults = false): void {
    this.uses.set(uses);
    this.selected.set(new Set(uses.behind.map(useKey)));
    if (!keepResults) {
      this.results.set(new Map());
    }
  }
}
