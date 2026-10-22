import { ChangeDetectionStrategy, Component, computed, inject, input, signal } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { CdkMenu, CdkMenuItem, CdkMenuTrigger } from '@angular/cdk/menu';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowUpTray,
  heroChevronDown,
  heroMagnifyingGlass,
  heroPencilSquare,
  heroPlus,
  heroSparkles,
} from '@ng-icons/heroicons/outline';
import { SkillService, UserSkill } from '../../services/skill/skill.service';
import { monogramFor } from '../../shared/utils/monogram';
import { SpinnerComponent } from '../../components/spinner/spinner.component';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog/confirmation-dialog.component';
import { CustomizeTabsComponent } from '../components/customize-tabs.component';
import { CustomizeCardComponent } from '../components/customize-card.component';
import { SkillRowComponent } from './components/skill-row.component';
import { MySkillService } from './services/my-skill.service';
import { MySkill } from './models/my-skill.model';

/** Which half of the page is showing. Mirrored into the `scope` query param. */
export type SkillScope = 'yours' | 'discover';

/** A skill resolved for the **Yours** list, with everything the row needs. */
interface SkillRow {
  skillId: string;
  name: string;
  description: string;
  monogram: string;
  byline: string;
  badge: string | null;
  meta: string | null;
  fileCount: number;
  enabled: boolean;
  toggleable: boolean;
  owned: boolean;
  detailLink: string;
  editLink: string;
}

/** A skill resolved for the **Discover** grid. */
interface SkillCard {
  skillId: string;
  name: string;
  description: string;
  monogram: string;
  enabled: boolean;
  detailLink: string;
}

/**
 * Customize → Skills. The **only** skills surface a user has.
 *
 * This page absorbed `/my-skills`, which used to be a separate top-level route
 * reachable only by a link-out from here. That split put the same noun in two
 * places with two different answers to "what skills do I have?" — one listed
 * what you authored, the other what you could turn on, and neither showed the
 * whole set. Everything now lives here, under two scopes:
 *
 * - **Yours** — skills you authored (any status, on or off) plus catalog skills
 *   you have turned on. The management surface: rows, with edit/delete on the
 *   ones you own.
 * - **Discover** — catalog skills your roles grant that are still off. The
 *   browse surface: cards, with a switch that turns one on.
 *
 * Turning a skill on is this platform's analogue of "installing" one: there is
 * no install step, because access is RBAC (`resolve_accessible_skill_ids`) and
 * the only state a user owns is the on/off preference.
 *
 * ⚠️ Reads `skillService.skills()` and `skill.isEnabled`, NOT `visibleSkills()` /
 * `isSkillShownEnabled()`, and writes with `respectAgentLock: false`. See
 * `docs/specs/customize-surface.md` §"The agent-lock seam" and the matching note
 * on `CustomizeToolsPage`.
 *
 * **Two services, on purpose.** `SkillService` (`GET /skills/`) is the picker
 * feed: accessible + ACTIVE only, with the enablement preference. `MySkillService`
 * (`GET /skills/mine`) is the authored tier at *every* status. Merging them
 * client-side is what keeps a draft skill visible to its author — it is absent
 * from `GET /skills/` by design, and widening that endpoint to carry drafts
 * would put skills in the composer picker that the runtime refuses to activate.
 *
 * Costs nothing against the model: both reads are catalog data for display.
 * Nothing here reaches the system prompt or `toolConfig`.
 */
@Component({
  selector: 'app-customize-skills',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    NgIcon,
    RouterLink,
    CdkMenu,
    CdkMenuItem,
    CdkMenuTrigger,
    SpinnerComponent,
    CustomizeTabsComponent,
    CustomizeCardComponent,
    SkillRowComponent,
  ],
  providers: [
    provideIcons({
      heroArrowUpTray,
      heroChevronDown,
      heroMagnifyingGlass,
      heroPencilSquare,
      heroPlus,
      heroSparkles,
    }),
  ],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
        <!-- Hub tabs, then the scope, then the actions — one row. The rule
             the divider draws is real: left of it changes *what* you are
             looking at, right of it changes *which of yours* you are seeing. -->
        <div class="flex flex-wrap items-center gap-x-3 gap-y-3">
          <app-customize-tabs />

          <span
            aria-hidden="true"
            class="hidden h-6 w-px bg-gray-300 sm:block dark:bg-white/20"
          ></span>

          <div class="flex items-center gap-1" role="group" aria-label="Skill scope">
            @for (option of SCOPES; track option.value) {
              <button
                type="button"
                (click)="setScope(option.value)"
                [attr.aria-pressed]="scope() === option.value"
                class="rounded-xl px-3.5 py-1.5 text-sm/6 font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                [class]="
                  scope() === option.value
                    ? 'bg-gray-900 text-white dark:bg-white dark:text-gray-900'
                    : 'text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white'
                "
              >
                {{ option.label }}
              </button>
            }
          </div>

          <div class="ml-auto flex items-center gap-2">
            <div class="relative">
              <ng-icon
                name="heroMagnifyingGlass"
                class="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-gray-400 dark:text-gray-500"
                aria-hidden="true"
              />
              <label for="customize-skill-search" class="sr-only">Search skills</label>
              <input
                type="search"
                id="customize-skill-search"
                [value]="query()"
                (input)="onSearch($event)"
                placeholder="Search skills…"
                class="block w-44 rounded-full border border-gray-300 bg-white py-1.5 pr-3 pl-9 text-sm/6 text-gray-900 transition-[width] placeholder:text-gray-400 focus:w-56 focus:border-primary-500 focus:ring-2 focus:ring-primary-500 focus:outline-none dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
              />
            </div>

            <!-- Authoring is gated on the same signal that used to hide the
                 whole /my-skills page: a 404 from GET /skills/mine means
                 SKILLS_ENABLED is off, and offering "Add" would open a form
                 whose save can only fail. -->
            @if (mySkillService.accessible$() === true) {
              <button
                type="button"
                [cdkMenuTriggerFor]="addMenu"
                class="inline-flex shrink-0 items-center gap-1.5 rounded-2xl bg-primary-accessible px-3.5 py-1.5 text-sm/6 font-medium text-white transition-[filter] hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
              >
                Add
                <ng-icon name="heroChevronDown" class="size-4" aria-hidden="true" />
              </button>
            }
          </div>
        </div>

        <ng-template #addMenu>
          <div
            cdkMenu
            role="menu"
            aria-orientation="vertical"
            class="w-56 rounded-xl bg-white py-1 shadow-lg ring-1 ring-black/5 focus:outline-hidden dark:bg-gray-800 dark:ring-white/10"
          >
            <a
              cdkMenuItem
              role="menuitem"
              routerLink="/customize/skills/new"
              [queryParams]="{ import: 1 }"
              class="flex w-full items-center gap-3 px-3 py-2 text-sm/6 text-gray-700 outline-hidden hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700"
            >
              <ng-icon name="heroArrowUpTray" class="size-4" aria-hidden="true" />
              Upload skill
            </a>
            <a
              cdkMenuItem
              role="menuitem"
              routerLink="/customize/skills/new"
              class="flex w-full items-center gap-3 px-3 py-2 text-sm/6 text-gray-700 outline-hidden hover:bg-gray-50 focus:bg-gray-50 dark:text-gray-300 dark:hover:bg-gray-700 dark:focus:bg-gray-700"
            >
              <ng-icon name="heroPencilSquare" class="size-4" aria-hidden="true" />
              Create a skill
            </a>
          </div>
        </ng-template>

        <div class="mt-6 mb-8">
          <h1 class="text-2xl font-bold tracking-tight text-gray-900 sm:text-3xl dark:text-white">
            Skills
          </h1>
          <p class="mt-1.5 max-w-2xl text-sm/6 text-gray-600 dark:text-gray-400">
            Instructions your assistant can pull in on demand — ones you write, and ones
            your roles grant you. Skills are off until you turn them on, and apply to every
            conversation.
          </p>
        </div>

        @if (skillService.error(); as loadError) {
          <div
            role="alert"
            class="mb-6 rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-300"
          >
            {{ loadError }}
          </div>
        }

        @if (mySkillService.error$(); as authorError) {
          <div
            role="alert"
            class="mb-6 rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-300"
          >
            {{ authorError }}
          </div>
        }

        @if (saveError()) {
          <div
            role="alert"
            class="mb-6 rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-300"
          >
            {{ saveError() }}
          </div>
        }

        @if (loading()) {
          <div class="flex items-center gap-3 text-sm/6 text-gray-500 dark:text-gray-400">
            <app-spinner size="sm" label="Loading skills" />
            Loading skills…
          </div>
        } @else if (scope() === 'yours') {
          @if (authored().length === 0 && fromCatalog().length === 0) {
            <div
              class="rounded-2xl border border-dashed border-gray-300 p-10 text-center dark:border-gray-700"
            >
              <ng-icon
                name="heroSparkles"
                class="mx-auto size-8 text-gray-400 dark:text-gray-500"
                aria-hidden="true"
              />
              <p class="mt-3 text-sm/6 font-medium text-gray-900 dark:text-white">
                {{ query() ? 'No skills match your search' : 'Nothing here yet' }}
              </p>
              <p class="mx-auto mt-1 max-w-md text-sm/6 text-gray-500 dark:text-gray-400">
                {{
                  query()
                    ? 'Try a different word, or clear the search.'
                    : 'Write down something you explain to the assistant over and over — a house style, a process, a checklist — or turn on one of the skills you already have.'
                }}
              </p>
              @if (!query()) {
                <div class="mt-6 flex flex-wrap items-center justify-center gap-2">
                  @if (mySkillService.accessible$() === true) {
                    <a
                      routerLink="/customize/skills/new"
                      class="inline-flex items-center gap-2 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                    >
                      <ng-icon name="heroPlus" class="size-5" aria-hidden="true" />
                      Create a skill
                    </a>
                  }
                  @if (discoverable().length > 0) {
                    <button
                      type="button"
                      (click)="setScope('discover')"
                      class="inline-flex items-center gap-2 rounded-2xl border border-gray-200 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:border-gray-300 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:text-white"
                    >
                      Browse {{ discoverable().length }} available
                    </button>
                  }
                </div>
              }
            </div>
          }

          @if (authored().length > 0) {
            <section>
              <h2
                class="flex items-baseline gap-2 text-base/7 font-semibold text-gray-900 dark:text-white"
              >
                Created by you
                <span
                  class="font-mono text-xs/5 font-normal text-gray-500 tabular-nums dark:text-gray-400"
                  >{{ authored().length }}</span
                >
              </h2>
              <ul class="mt-1">
                @for (row of authored(); track row.skillId) {
                  <li>
                    <app-skill-row
                      [name]="row.name"
                      [description]="row.description"
                      [monogram]="row.monogram"
                      [byline]="row.byline"
                      [badge]="row.badge"
                      [meta]="row.meta"
                      [fileCount]="row.fileCount"
                      [enabled]="row.enabled"
                      [toggleable]="row.toggleable"
                      [owned]="row.owned"
                      [pending]="pending().has(row.skillId)"
                      [detailLink]="row.detailLink"
                      [editLink]="row.editLink"
                      (toggled)="onToggleById(row.skillId)"
                      (deleted)="confirmDelete(row.skillId, row.name)"
                    />
                  </li>
                }
              </ul>
            </section>
          }

          @if (fromCatalog().length > 0) {
            <section [class.mt-10]="authored().length > 0">
              <h2
                class="flex items-baseline gap-2 text-base/7 font-semibold text-gray-900 dark:text-white"
              >
                From the catalog
                <span
                  class="font-mono text-xs/5 font-normal text-gray-500 tabular-nums dark:text-gray-400"
                  >{{ fromCatalog().length }}</span
                >
              </h2>
              <p class="mt-0.5 text-sm/6 text-gray-500 dark:text-gray-400">
                Granted by your roles, and switched on.
              </p>
              <ul class="mt-1">
                @for (row of fromCatalog(); track row.skillId) {
                  <li>
                    <app-skill-row
                      [name]="row.name"
                      [description]="row.description"
                      [monogram]="row.monogram"
                      [byline]="row.byline"
                      [badge]="row.badge"
                      [enabled]="row.enabled"
                      [toggleable]="row.toggleable"
                      [pending]="pending().has(row.skillId)"
                      [detailLink]="row.detailLink"
                      (toggled)="onToggleById(row.skillId)"
                    />
                  </li>
                }
              </ul>
            </section>
          }
        } @else {
          @if (!query() && categoryChips().length > 1) {
            <div class="mb-5 flex flex-wrap gap-2" role="group" aria-label="Filter by category">
              @for (chip of categoryChips(); track chip) {
                <button
                  type="button"
                  (click)="onCategory(chip)"
                  [attr.aria-pressed]="activeCategory() === chip"
                  class="rounded-full border px-3.5 py-1 text-sm/6 font-medium capitalize transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
                  [class]="
                    activeCategory() === chip
                      ? 'border-gray-900 bg-gray-900 text-white dark:border-white dark:bg-white dark:text-gray-900'
                      : 'border-gray-200 bg-white text-gray-600 hover:border-gray-300 hover:text-gray-900 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-400 dark:hover:text-white'
                  "
                >
                  {{ chip === ALL ? 'All' : chip }}
                </button>
              }
            </div>
          }

          @if (discoverCards().length === 0) {
            <div
              class="rounded-2xl border border-dashed border-gray-300 p-10 text-center dark:border-gray-700"
            >
              <p class="text-sm/6 font-medium text-gray-900 dark:text-white">
                {{ query() ? 'No skills match your search' : "You've turned on everything" }}
              </p>
              <p class="mx-auto mt-1 max-w-md text-sm/6 text-gray-500 dark:text-gray-400">
                {{
                  query()
                    ? 'Try a different word, or clear the search.'
                    : 'There are no more skills your roles grant. Ask an admin if you need one that is not here.'
                }}
              </p>
            </div>
          } @else {
            <p class="mb-3 text-sm/6 text-gray-500 dark:text-gray-400" aria-live="polite">
              {{ discoverLabel() }}
            </p>
            <ul class="grid gap-4 sm:grid-cols-2 2xl:grid-cols-3">
              @for (card of discoverCards(); track card.skillId) {
                <li>
                  <app-customize-card
                    [name]="card.name"
                    [description]="card.description"
                    [monogram]="card.monogram"
                    [enabled]="card.enabled"
                    [detailLink]="card.detailLink"
                    [pending]="pending().has(card.skillId)"
                    (toggled)="onToggleById(card.skillId)"
                  />
                </li>
              }
            </ul>
          }
        }
      </div>
    </div>
  `,
})
export class CustomizeSkillsPage {
  protected readonly skillService = inject(SkillService);
  protected readonly mySkillService = inject(MySkillService);
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  private readonly dialog = inject(Dialog);

  /** The chip meaning "don't filter". Not a category id, so it can never collide. */
  protected readonly ALL = '__all__';

  protected readonly SCOPES: { value: SkillScope; label: string }[] = [
    { value: 'yours', label: 'Yours' },
    { value: 'discover', label: 'Discover' },
  ];

  /**
   * Bound from the `scope` **query param** by `withComponentInputBinding()`, so
   * the two halves of the page are linkable and survive a reload. Absent (and
   * anything unrecognised) reads as `yours`: the scope a returning user wants.
   */
  readonly scopeParam = input<string | undefined>(undefined, { alias: 'scope' });

  protected readonly scope = computed<SkillScope>(() =>
    this.scopeParam() === 'discover' ? 'discover' : 'yours',
  );

  protected readonly query = signal('');
  protected readonly activeCategory = signal<string>(this.ALL);
  protected readonly pending = signal<ReadonlySet<string>>(new Set());
  protected readonly saveError = signal<string | null>(null);

  constructor() {
    // Unlike ToolService, SkillService does not load in its constructor — the
    // load is deferred to whichever surface needs it first. This is one of them.
    if (!this.skillService.initialized() && !this.skillService.loading()) {
      void this.skillService.loadSkills();
    }
    // The authored tier is a separate read, and deliberately so — see the
    // "Two services, on purpose" note on the class. It resolves `accessible$`,
    // which gates the Add menu.
    void this.mySkillService.loadSkills().catch(() => {
      // Surfaced on `error$` and rendered by the banner above.
    });
  }

  /**
   * Only the FIRST load blanks the page. Once either list has resolved, a
   * background refetch must not throw the user back to a spinner.
   */
  protected readonly loading = computed(
    () =>
      (this.skillService.loading() && !this.skillService.initialized()) ||
      (this.mySkillService.loading$() && this.mySkillService.accessible$() === null),
  );

  /** Skill ids the current user authored — the ownership test for every row. */
  private readonly ownedIds = computed(
    () => new Set(this.mySkillService.skills$().map(s => s.skillId)),
  );

  /** Enablement by id, from the picker feed. Absent ⇒ off (Skills v2 D6). */
  private readonly enabledById = computed(() => {
    const map = new Map<string, boolean>();
    for (const skill of this.skillService.skills()) {
      map.set(skill.skillId, skill.isEnabled);
    }
    return map;
  });

  /**
   * Your own skills, at every status. Sorted most-recently-touched first: this
   * is a working list, and the thing you just edited is the thing you came back
   * for.
   */
  protected readonly authored = computed<SkillRow[]>(() => {
    const enabled = this.enabledById();
    const rows = this.matchingAuthored().map<SkillRow>(skill => ({
      skillId: skill.skillId,
      name: skill.displayName,
      description: skill.description || 'No description recorded.',
      monogram: monogramFor(skill.displayName),
      byline: 'by you',
      // A non-active skill is absent from `GET /skills/` entirely, so the row
      // has to say why it can never be on rather than showing a dead switch.
      badge: skill.status !== 'active' ? skill.status : null,
      meta: this.dateLabel(skill.updatedAt ?? skill.createdAt),
      fileCount: skill.resources.length,
      enabled: enabled.get(skill.skillId) ?? false,
      // A non-active skill is not in the picker feed, so there is nothing to
      // toggle — see `toggleable` on SkillRowComponent.
      toggleable: enabled.has(skill.skillId),
      owned: true,
      detailLink: this.detailLink(skill.skillId),
      editLink: this.editLink(skill.skillId),
    }));
    return rows.sort((a, b) => a.name.localeCompare(b.name));
  });

  /** Catalog skills you have switched on. Yours in use, not yours to edit. */
  protected readonly fromCatalog = computed<SkillRow[]>(() => {
    const owned = this.ownedIds();
    return this.matchingCatalog()
      .filter(skill => skill.isEnabled && !owned.has(skill.skillId))
      .map<SkillRow>(skill => ({
        skillId: skill.skillId,
        name: skill.displayName,
        description: skill.description || 'No description recorded.',
        monogram: monogramFor(skill.displayName),
        byline: 'from the catalog',
        badge: skill.category,
        meta: null,
        fileCount: 0,
        enabled: true,
        toggleable: true,
        owned: false,
        detailLink: this.detailLink(skill.skillId),
        editLink: '',
      }));
  });

  /** Every catalog skill still off — the Discover population, before filtering. */
  protected readonly discoverable = computed(() => {
    const owned = this.ownedIds();
    return this.skillService.skills().filter(s => !s.isEnabled && !owned.has(s.skillId));
  });

  protected readonly discoverCards = computed<SkillCard[]>(() => {
    const owned = this.ownedIds();
    const category = this.activeCategory();
    let skills = this.matchingCatalog().filter(s => !s.isEnabled && !owned.has(s.skillId));

    if (!this.query().trim() && category !== this.ALL) {
      skills = skills.filter(skill => skill.category === category);
    }

    return skills.map(skill => ({
      skillId: skill.skillId,
      name: skill.displayName,
      description: skill.description || 'No description recorded.',
      monogram: monogramFor(skill.displayName),
      // `isEnabled`, never `isSkillShownEnabled()` — see the class comment.
      enabled: skill.isEnabled,
      detailLink: this.detailLink(skill.skillId),
    }));
  });

  /** Categories present in Discover — filtering to an empty chip helps nobody. */
  protected readonly categoryChips = computed(() => {
    const categories = [
      ...new Set(
        this.discoverable()
          .map(s => s.category)
          .filter((c): c is string => !!c),
      ),
    ].sort();
    return [this.ALL, ...categories];
  });

  protected readonly discoverLabel = computed(() => {
    const shown = this.discoverCards().length;
    return `${shown} ${shown === 1 ? 'skill' : 'skills'} available to turn on`;
  });

  private readonly matchingAuthored = computed<MySkill[]>(() =>
    this.mySkillService
      .skills$()
      .filter(skill => this.matches(skill.displayName, skill.description, skill.category)),
  );

  private readonly matchingCatalog = computed<UserSkill[]>(() =>
    this.skillService
      .skills()
      .filter(skill => this.matches(skill.displayName, skill.description, skill.category)),
  );

  private matches(...fields: (string | null | undefined)[]): boolean {
    const q = this.query().trim().toLowerCase();
    if (!q) return true;
    return fields.some(field => !!field && field.toLowerCase().includes(q));
  }

  private detailLink(skillId: string): string {
    // Encoded: ids are opaque catalog keys.
    return `/customize/skills/${encodeURIComponent(skillId)}`;
  }

  private editLink(skillId: string): string {
    return `/customize/skills/${encodeURIComponent(skillId)}/edit`;
  }

  /** An unparseable timestamp is dropped rather than rendered as "Invalid Date". */
  private dateLabel(iso: string | null): string | null {
    if (!iso) return null;
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? null : date.toLocaleDateString();
  }

  /**
   * `relativeTo` is load-bearing: `navigate([])` without it resolves the empty
   * command list against the ROOT, so the navigation lands on the same URL with
   * the query params dropped and the scope never changes.
   */
  protected setScope(scope: SkillScope): void {
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams: { scope: scope === 'yours' ? null : scope },
      queryParamsHandling: 'merge',
      replaceUrl: true,
    });
  }

  protected onSearch(event: Event): void {
    this.query.set((event.target as HTMLInputElement).value);
  }

  protected onCategory(chip: string): void {
    this.activeCategory.set(chip);
  }

  protected async onToggleById(skillId: string): Promise<void> {
    if (this.pending().has(skillId)) return;

    const name = this.skillService.getSkill(skillId)?.displayName ?? 'that skill';
    this.saveError.set(null);
    this.pending.update(set => new Set(set).add(skillId));
    try {
      // `respectAgentLock: false` — see the class comment.
      await this.skillService.toggleSkill(skillId, { respectAgentLock: false });
    } catch {
      this.saveError.set(`Couldn't save the change to ${name}. Please try again.`);
    } finally {
      this.pending.update(set => {
        const next = new Set(set);
        next.delete(skillId);
        return next;
      });
    }
  }

  protected async confirmDelete(skillId: string, displayName: string): Promise<void> {
    const data: ConfirmationDialogData = {
      title: 'Delete this skill?',
      message: `"${displayName}" and its supporting files will be permanently deleted. Agents that use it will stop seeing it.`,
      confirmText: 'Delete',
      cancelText: 'Cancel',
      destructive: true,
    };

    const dialogRef = this.dialog.open<boolean>(ConfirmationDialogComponent, { data });
    const confirmed = await firstValueFrom(dialogRef.closed);
    if (!confirmed) return;

    this.pending.update(set => new Set(set).add(skillId));
    try {
      await this.mySkillService.deleteSkill(skillId);
      // The deleted skill is still in the picker feed until it is re-read, and
      // a ghost row that 404s on click is worse than a brief spinner.
      await this.skillService.reload();
    } catch {
      // The service surfaces the message on `error$`; the banner renders it.
    } finally {
      this.pending.update(set => {
        const next = new Set(set);
        next.delete(skillId);
        return next;
      });
    }
  }
}
