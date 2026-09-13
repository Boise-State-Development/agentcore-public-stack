import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroMagnifyingGlass, heroArrowTopRightOnSquare } from '@ng-icons/heroicons/outline';
import { SkillService, UserSkill } from '../../services/skill/skill.service';
import { monogramFor } from '../../shared/utils/monogram';
import { SpinnerComponent } from '../../components/spinner/spinner.component';
import { CustomizeTabsComponent } from '../components/customize-tabs.component';
import { CustomizeCardComponent } from '../components/customize-card.component';

/** A skill paired with everything the card needs, resolved once per render. */
interface SkillCard {
  skill: UserSkill;
  name: string;
  description: string;
  monogram: string;
  enabled: boolean;
}

/**
 * Customize → Skills. The browsable home for skill enablement (spec step 1).
 *
 * ⚠️ Reads `skillService.skills()` and `skill.isEnabled`, NOT `visibleSkills()` /
 * `isSkillShownEnabled()`, and writes with `respectAgentLock: false`. See
 * `docs/specs/customize-surface.md` §"The agent-lock seam" and the matching note
 * on `CustomizeToolsPage`.
 *
 * Skills default **off** (Skills v2 D6 opt-in — the reverse of tools), so the
 * empty-ish state here is the expected one, not a fault. The copy says so rather
 * than leaving a page of grey switches to imply something is broken.
 */
@Component({
  selector: 'app-customize-skills',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    NgIcon,
    RouterLink,
    SpinnerComponent,
    CustomizeTabsComponent,
    CustomizeCardComponent,
  ],
  providers: [provideIcons({ heroMagnifyingGlass, heroArrowTopRightOnSquare })],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
        <app-customize-tabs />

        <div class="mt-6 mb-10 flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 class="text-2xl font-bold tracking-tight text-gray-900 sm:text-3xl dark:text-white">Skills</h1>
            <p class="mt-1.5 max-w-2xl text-sm/6 text-gray-600 dark:text-gray-400">
              Instructions your assistant can pull in on demand. Skills are off until you
              turn them on, and apply to every conversation.
            </p>
          </div>
          <!-- The first nav path /my-skills has ever had. Its absence was a
               standing open question in the sidenav (see sidenav.html). -->
          <a
            routerLink="/my-skills"
            class="inline-flex shrink-0 items-center gap-1.5 rounded-xl border border-gray-200 bg-white px-3.5 py-1.5 text-sm/6 font-medium text-gray-700 transition-colors hover:border-gray-300 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:text-white"
          >
            My skills
            <ng-icon name="heroArrowTopRightOnSquare" class="size-4" aria-hidden="true" />
          </a>
        </div>

        <div class="relative max-w-md">
          <ng-icon
            name="heroMagnifyingGlass"
            class="pointer-events-none absolute left-4 top-1/2 size-4 -translate-y-1/2 text-gray-400 dark:text-gray-500"
            aria-hidden="true"
          />
          <label for="customize-skill-search" class="sr-only">Search skills</label>
          <input
            type="search"
            id="customize-skill-search"
            [value]="query()"
            (input)="onSearch($event)"
            placeholder="Search skills…"
            class="block w-full rounded-full border border-gray-300 bg-white py-2.5 pl-10 pr-4 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
          />
        </div>

        @if (!query() && categoryChips().length > 1) {
          <div class="mt-4 flex flex-wrap gap-2" role="group" aria-label="Filter by category">
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

        @if (skillService.error(); as loadError) {
          <div
            role="alert"
            class="mt-6 rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-300"
          >
            {{ loadError }}
          </div>
        }

        @if (saveError()) {
          <div
            role="alert"
            class="mt-6 rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-300"
          >
            {{ saveError() }}
          </div>
        }

        @if (skillService.loading() && !skillService.initialized()) {
          <div class="mt-8 flex items-center gap-3 text-sm/6 text-gray-500 dark:text-gray-400">
            <app-spinner size="sm" label="Loading skills" />
            Loading skills…
          </div>
        } @else if (cards().length === 0) {
          <div
            class="mt-8 rounded-2xl border border-dashed border-gray-300 p-8 text-center dark:border-gray-700"
          >
            <p class="text-sm/6 font-medium text-gray-900 dark:text-white">
              {{ query() ? 'No skills match your search' : 'No skills available' }}
            </p>
            <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
              {{
                query()
                  ? 'Try a different word, or clear the search to browse everything.'
                  : 'Your roles do not grant access to any skills yet, and you have not written one.'
              }}
            </p>
          </div>
        } @else {
          <p class="mt-6 text-sm/6 text-gray-500 dark:text-gray-400" aria-live="polite">
            {{ enabledLabel() }}
          </p>
          <ul class="mt-3 grid gap-4 sm:grid-cols-2 2xl:grid-cols-3">
            @for (card of cards(); track card.skill.skillId) {
              <li>
                <app-customize-card
                  [name]="card.name"
                  [description]="card.description"
                  [monogram]="card.monogram"
                  [enabled]="card.enabled"
                  [pending]="pending().has(card.skill.skillId)"
                  (toggled)="onToggle(card.skill)"
                />
              </li>
            }
          </ul>
        }
      </div>
    </div>
  `,
})
export class CustomizeSkillsPage {
  protected readonly skillService = inject(SkillService);

  /** The chip meaning "don't filter". Not a category id, so it can never collide. */
  protected readonly ALL = '__all__';

  protected readonly query = signal('');
  protected readonly activeCategory = signal<string>(this.ALL);
  protected readonly pending = signal<ReadonlySet<string>>(new Set());
  protected readonly saveError = signal<string | null>(null);

  constructor() {
    // Unlike ToolService, SkillService does not load in its constructor — the
    // load is deferred to whichever surface needs it first. This is now one of
    // them.
    if (!this.skillService.initialized() && !this.skillService.loading()) {
      void this.skillService.loadSkills();
    }
  }

  protected readonly categoryChips = computed(() => {
    const categories = [
      ...new Set(
        this.skillService
          .skills()
          .map(s => s.category)
          .filter((c): c is string => !!c),
      ),
    ].sort();
    return [this.ALL, ...categories];
  });

  protected readonly cards = computed<SkillCard[]>(() => {
    const q = this.query().trim().toLowerCase();
    const category = this.activeCategory();

    // `skills()`, never `visibleSkills()` — see the class comment.
    let skills = this.skillService.skills();

    if (q) {
      skills = skills.filter(skill =>
        [skill.displayName, skill.description, skill.category]
          .filter((field): field is string => !!field)
          .some(field => field.toLowerCase().includes(q)),
      );
    } else if (category !== this.ALL) {
      skills = skills.filter(skill => skill.category === category);
    }

    return skills.map(skill => ({
      skill,
      name: skill.displayName,
      description: skill.description || 'No description recorded.',
      monogram: monogramFor(skill.displayName),
      // `isEnabled`, never `isSkillShownEnabled()` — see the class comment.
      enabled: skill.isEnabled,
    }));
  });

  protected readonly enabledLabel = computed(() => {
    const total = this.skillService.skills().length;
    const on = this.skillService.skills().filter(s => s.isEnabled).length;
    return `${on} of ${total} ${total === 1 ? 'skill' : 'skills'} on`;
  });

  protected onSearch(event: Event): void {
    this.query.set((event.target as HTMLInputElement).value);
  }

  protected onCategory(chip: string): void {
    this.activeCategory.set(chip);
  }

  protected async onToggle(skill: UserSkill): Promise<void> {
    const id = skill.skillId;
    if (this.pending().has(id)) return;

    this.saveError.set(null);
    this.pending.update(set => new Set(set).add(id));
    try {
      // `respectAgentLock: false` — see the class comment.
      await this.skillService.toggleSkill(id, { respectAgentLock: false });
    } catch {
      this.saveError.set(`Couldn't save the change to ${skill.displayName}. Please try again.`);
    } finally {
      this.pending.update(set => {
        const next = new Set(set);
        next.delete(id);
        return next;
      });
    }
  }
}
