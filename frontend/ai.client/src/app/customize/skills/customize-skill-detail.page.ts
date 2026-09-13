import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroArrowTopRightOnSquare,
  heroDocumentText,
} from '@ng-icons/heroicons/outline';
import { MarkdownComponent } from 'ngx-markdown';
import { SkillService } from '../../services/skill/skill.service';
import {
  SkillDetail,
  SkillDetailResource,
  SkillDetailService,
} from '../../services/skill-detail/skill-detail.service';
import { monogramFor } from '../../shared/utils/monogram';
import { SpinnerComponent } from '../../components/spinner/spinner.component';

/** Bundle-directory labels, in the order agentskills.io lists them. */
const KIND_ORDER: Record<string, number> = { reference: 0, script: 1, asset: 2 };

/**
 * Customize → Skills → one skill. What the browse card has no room for: the
 * SKILL.md body the skill actually injects, the supporting files behind it,
 * and the catalog facts.
 *
 * The sibling of `CustomizeToolDetailPage`, with one structural difference
 * worth knowing before reading it: **the tool page needed no backend work.**
 * `GET /tools/` already returns the whole `Tool` including `serverTools`. The
 * skills picker returns six thin fields, so everything below the switch here
 * comes from `GET /skills/{id}` — an access-checked read added alongside this
 * page (`apis/app_api/skills/routes.py`).
 *
 * ⚠️ Reads and writes **global** state, like the list it drills in from:
 * `skill.isEnabled`, never `isSkillShownEnabled()`, and
 * `toggleSkill(..., { respectAgentLock: false })`. See
 * `docs/specs/customize-surface.md` §"The agent-lock seam".
 *
 * Two deliberate differences from the tool page:
 *
 * - **This page closes no functional gap.** The tool page had to exist the
 *   moment #1079 deleted the drawer, because per-sub-tool enablement had
 *   nowhere else to live. A skill has no sub-unit; the only control here is
 *   the same on/off the card already offers. Its value is informational —
 *   answering "what will this actually put in my prompt?" — so the
 *   instructions body is the page's centre of gravity, not a disclosure.
 * - **Instructions render expanded.** They are not a secret from a user the
 *   skill is granted to: this is the text their own turns load on dispatch.
 *   Rendered through `ngx-markdown` **with sanitization on** — do not add
 *   `[disableSanitizer]`; a SKILL.md body can be authored by a non-admin
 *   (Skills v2 PR-3 user tier), and the same reasoning as
 *   `announcement-modal.component.ts` applies.
 *
 * Costs nothing against the model: everything here is catalog data read for
 * display. Nothing reaches the system prompt or `toolConfig`.
 */
@Component({
  selector: 'app-customize-skill-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink, SpinnerComponent, MarkdownComponent],
  providers: [
    provideIcons({ heroArrowLeft, heroArrowTopRightOnSquare, heroDocumentText }),
  ],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
        <a
          routerLink="/customize/skills"
          class="inline-flex items-center gap-1.5 text-sm/6 font-medium text-gray-500 transition-colors hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:text-white"
        >
          <ng-icon name="heroArrowLeft" class="size-4" aria-hidden="true" />
          Skills
        </a>

        @if (skill(); as s) {
          <!-- Identity -->
          <div class="mt-6 flex items-start gap-4">
            <span
              aria-hidden="true"
              class="grid size-12 shrink-0 place-items-center rounded-2xl border border-gray-200 bg-gray-50 font-mono text-base font-semibold text-gray-600 dark:border-white/10 dark:bg-white/5 dark:text-gray-300"
              >{{ monogram() }}</span
            >
            <div class="min-w-0 flex-1">
              <h1 class="text-2xl/8 font-bold break-words text-gray-900 dark:text-white">
                {{ s.displayName }}
              </h1>
              <p class="mt-0.5 font-mono text-xs/5 break-all text-gray-500 dark:text-gray-400">
                {{ s.skillId }}
              </p>
              <div class="mt-2 flex flex-wrap items-center gap-1.5">
                @for (chip of chips(); track chip) {
                  <span
                    class="rounded-sm bg-gray-100 px-1.5 font-mono text-[10px]/5 font-medium text-gray-600 dark:bg-white/10 dark:text-gray-300"
                    >{{ chip }}</span
                  >
                }
              </div>
            </div>
            <!-- Editing lives on /my-skills, which owns the authoring form and
                 the upload path. This is a read surface; it links there rather
                 than growing a second editor. -->
            @if (s.isOwned) {
              <a
                [routerLink]="['/my-skills', s.skillId, 'edit']"
                class="inline-flex shrink-0 items-center gap-1.5 rounded-2xl border border-gray-200 bg-white px-3.5 py-1.5 text-sm/6 font-medium text-gray-700 transition-colors hover:border-gray-300 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300 dark:hover:text-white"
              >
                Edit in My Skills
                <ng-icon name="heroArrowTopRightOnSquare" class="size-4" aria-hidden="true" />
              </a>
            }
          </div>

          @if (s.description) {
            <p class="mt-4 text-sm/6 text-gray-600 dark:text-gray-300">{{ s.description }}</p>
          }

          <!-- Master switch -->
          <div
            class="mt-4 flex items-center justify-between gap-4 rounded-2xl border border-gray-200 bg-gray-50 px-4 py-3 dark:border-white/10 dark:bg-white/5"
          >
            <div class="min-w-0">
              <span
                id="skill-detail-state"
                class="block text-sm/6 font-medium text-gray-900 dark:text-white"
                >{{ enabled() ? 'On' : 'Off' }}</span
              >
              <span class="block text-xs/5 text-gray-500 dark:text-gray-400">
                Applies to every conversation, including ones already open.
              </span>
            </div>
            <button
              type="button"
              role="switch"
              [attr.aria-checked]="enabled()"
              aria-labelledby="skill-detail-state"
              [disabled]="saving() || !skillService.initialized()"
              (click)="onToggle()"
              class="relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
              [class]="enabled() ? 'bg-primary-600 dark:bg-primary-500' : 'bg-gray-200 dark:bg-gray-700'"
            >
              <span
                aria-hidden="true"
                class="pointer-events-none inline-block size-5 transform rounded-full bg-white shadow-sm ring-0 transition duration-200 ease-in-out"
                [class.translate-x-5]="enabled()"
                [class.translate-x-0]="!enabled()"
              ></span>
            </button>
          </div>

          @if (saveError()) {
            <div
              role="alert"
              class="mt-4 rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-300"
            >
              {{ saveError() }}
            </div>
          }

          <!-- Instructions: the reason to open this page at all. -->
          <section class="mt-8">
            <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">Instructions</h2>
            <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
              What the assistant reads when it picks this skill up.
            </p>
            @if (s.instructions) {
              <div
                class="message-block mt-3 overflow-x-auto rounded-2xl border border-gray-200 px-4 py-3 text-sm/6 text-gray-700 dark:border-white/10 dark:text-gray-200"
              >
                <markdown [data]="s.instructions" />
              </div>
            } @else {
              <p class="mt-3 text-sm/6 text-gray-500 dark:text-gray-400">
                This skill has no instructions body — only the one-line description above
                reaches the model.
              </p>
            }
          </section>

          <!-- Supporting files -->
          @if (s.resources.length > 0) {
            <section class="mt-8">
              <h2
                class="flex items-baseline gap-2 text-base/7 font-semibold text-gray-900 dark:text-white"
              >
                Files
                <span
                  class="font-mono text-xs/5 font-normal text-gray-500 tabular-nums dark:text-gray-400"
                  >{{ s.resources.length }}</span
                >
              </h2>
              <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
                Reference material the assistant can open on demand — not loaded up front.
              </p>
              <ul class="mt-3 flex flex-col gap-2">
                @for (file of files(); track file.filename) {
                  <li
                    class="flex items-center justify-between gap-4 rounded-2xl border border-gray-200 px-4 py-3 dark:border-white/10"
                  >
                    <span class="flex min-w-0 items-center gap-2">
                      <ng-icon
                        name="heroDocumentText"
                        class="size-4 shrink-0 text-gray-400 dark:text-gray-500"
                        aria-hidden="true"
                      />
                      <span class="min-w-0">
                        <span
                          class="block truncate font-mono text-sm/6 font-medium text-gray-900 dark:text-white"
                          >{{ file.filename }}</span
                        >
                        <span class="block text-xs/5 text-gray-500 capitalize dark:text-gray-400">
                          {{ file.kind }} · {{ sizeLabel(file.size) }}
                        </span>
                      </span>
                    </span>
                    <!-- A plain link, not a fetch: the backend serves these
                         attachment + nosniff with an inert CSP, so letting the
                         browser handle it is both simpler and the safe path.
                         Opened in a new tab with rel=noopener so a download
                         never navigates the SPA away from this page. -->
                    <a
                      [href]="resourceUrl(s.skillId, file.filename)"
                      target="_blank"
                      rel="noopener"
                      class="shrink-0 text-sm/6 font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark"
                      >Open</a
                    >
                  </li>
                }
              </ul>
            </section>
          }

          <!-- Composed skills -->
          @if (s.compose.length > 0) {
            <section class="mt-8">
              <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">Builds on</h2>
              <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
                Turning this on brings these in too.
              </p>
              <div class="mt-3 flex flex-wrap gap-2">
                @for (id of s.compose; track id) {
                  <span
                    class="rounded-full bg-gray-100 px-3 py-1 font-mono text-xs/5 text-gray-700 dark:bg-white/10 dark:text-gray-200"
                    >{{ id }}</span
                  >
                }
              </div>
            </section>
          }

          <!-- Advisory tool names -->
          @if (s.allowedTools.length > 0) {
            <section class="mt-8">
              <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">
                Tools it mentions
              </h2>
              <!-- Skills v2 D4: the allowed-tools frontmatter is ADVISORY. The
                   platform never grants, mounts or folds a tool because a skill
                   names it. Saying so is the whole point of rendering it — a bare
                   list here would read as a grant. -->
              <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
                Named in the skill's own frontmatter as useful to it. This is a note from
                its author, not a grant — your tools are whatever Customize → Tools says
                they are.
              </p>
              <div class="mt-3 flex flex-wrap gap-2">
                @for (name of s.allowedTools; track name) {
                  <span
                    class="rounded-full bg-gray-100 px-3 py-1 font-mono text-xs/5 text-gray-700 dark:bg-white/10 dark:text-gray-200"
                    >{{ name }}</span
                  >
                }
              </div>
            </section>
          }

          <!-- About -->
          <section class="mt-8">
            <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">About</h2>
            <dl class="mt-3 flex flex-col gap-2.5">
              @for (fact of facts(); track fact.label) {
                <div
                  class="flex items-baseline justify-between gap-4 border-b border-gray-100 pb-2.5 last:border-0 dark:border-white/5"
                >
                  <dt class="text-sm/6 text-gray-500 dark:text-gray-400">{{ fact.label }}</dt>
                  <dd
                    class="min-w-0 text-right font-mono text-sm/6 break-words text-gray-700 dark:text-gray-200"
                  >
                    {{ fact.value }}
                  </dd>
                </div>
              }
            </dl>
          </section>
        } @else if (detailService.isLoading(skillId()) || !resolved()) {
          <div class="mt-8 flex items-center gap-3 text-sm/6 text-gray-500 dark:text-gray-400">
            <app-spinner size="sm" label="Loading skill" />
            Loading…
          </div>
        } @else if (detailService.hasFailed(skillId())) {
          <!-- Distinct from not-found: the skill may well exist and we simply
               could not read it. Telling the user it is gone would be a lie. -->
          <div
            role="alert"
            class="mt-8 rounded-2xl border border-state-danger-200 bg-state-danger-50 px-4 py-3 text-sm/6 text-state-danger-800 dark:border-state-danger-900 dark:bg-state-danger-900/20 dark:text-state-danger-300"
          >
            Couldn't load this skill. Please try again.
          </div>
        } @else {
          <!-- Resolved 404: retired, or the user's roles no longer grant it.
               Both read the same to them, and deliberately so — a message that
               distinguished them would confirm a skill someone else holds. -->
          <div
            class="mt-8 rounded-2xl border border-dashed border-gray-300 p-8 text-center dark:border-gray-700"
          >
            <p class="text-sm/6 font-medium text-gray-900 dark:text-white">Skill not found</p>
            <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
              This skill no longer exists, or your roles don't grant it.
            </p>
            <a
              routerLink="/customize/skills"
              class="mt-4 inline-block text-sm/6 font-semibold text-primary-accessible hover:underline dark:text-primary-accessible-dark"
              >Back to Skills</a
            >
          </div>
        }
      </div>
    </div>
  `,
})
export class CustomizeSkillDetailPage {
  protected readonly skillService = inject(SkillService);
  protected readonly detailService = inject(SkillDetailService);

  /** Bound from the `:skillId` route param by `withComponentInputBinding()`. */
  readonly skillId = input.required<string>();

  protected readonly saving = signal(false);
  protected readonly saveError = signal<string | null>(null);

  constructor() {
    // The toggle writes through SkillService, which silently no-ops on a skill
    // it has never loaded — so a deep link to this page has to warm the list,
    // not just the detail record. The switch stays disabled until it lands.
    if (!this.skillService.initialized() && !this.skillService.loading()) {
      void this.skillService.loadSkills();
    }

    effect(() => void this.detailService.ensure(this.skillId()));
  }

  protected readonly skill = computed<SkillDetail | null>(() =>
    this.detailService.detailFor(this.skillId()),
  );

  /** The detail read has settled, whatever the answer. */
  protected readonly resolved = computed(
    () =>
      !!this.skill() ||
      this.detailService.isMissing(this.skillId()) ||
      this.detailService.hasFailed(this.skillId()),
  );

  protected readonly monogram = computed(() =>
    monogramFor(this.skill()?.displayName ?? ''),
  );

  /**
   * The switch reads SkillService once it has loaded, so this page and the
   * list it came from can never disagree about a toggle made on either. The
   * detail record covers the gap before that load lands.
   */
  protected readonly enabled = computed(() => {
    const row = this.skillService.skills().find(s => s.skillId === this.skillId());
    return row ? row.isEnabled : (this.skill()?.isEnabled ?? false);
  });

  /** Category, ownership, and status only when it is worth saying out loud. */
  protected readonly chips = computed(() => {
    const s = this.skill();
    if (!s) return [];
    const chips: string[] = [];
    if (s.category) chips.push(s.category);
    if (s.isOwned) chips.push('yours');
    if (s.status !== 'active') chips.push(s.status);
    return chips;
  });

  /** Reference files first, then scripts, then assets; alphabetical within. */
  protected readonly files = computed<SkillDetailResource[]>(() =>
    [...(this.skill()?.resources ?? [])].sort(
      (a, b) =>
        (KIND_ORDER[a.kind] ?? 9) - (KIND_ORDER[b.kind] ?? 9) ||
        a.filename.localeCompare(b.filename),
    ),
  );

  /** Catalog facts the card has no room for. */
  protected readonly facts = computed(() => {
    const s = this.skill();
    if (!s) return [];
    const rows: { label: string; value: string }[] = [
      { label: 'Skill ID', value: s.skillId },
      { label: 'Source', value: s.isOwned ? 'you wrote this' : 'catalog' },
      { label: 'Status', value: s.status },
    ];
    if (s.category) rows.push({ label: 'Category', value: s.category });
    if (s.resources.length > 0) {
      rows.push({ label: 'Files', value: `${s.resources.length}` });
    }
    const updated = this.dateLabel(s.updatedAt);
    if (updated) rows.push({ label: 'Updated', value: updated });
    return rows;
  });

  protected resourceUrl(skillId: string, filename: string): string {
    return this.detailService.resourceUrl(skillId, filename);
  }

  protected sizeLabel(bytes: number): string {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  }

  /** An unparseable timestamp is dropped rather than rendered as "Invalid Date". */
  private dateLabel(iso: string | null): string | null {
    if (!iso) return null;
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? null : date.toLocaleDateString();
  }

  /**
   * A switch that snaps back on its own looks like a bug in the switch, so a
   * failed save says so. `SkillService` has already reverted its optimistic
   * update by the time this runs.
   */
  protected async onToggle(): Promise<void> {
    const s = this.skill();
    if (!s || this.saving()) return;

    this.saving.set(true);
    this.saveError.set(null);
    try {
      // `respectAgentLock: false` — see the class comment.
      await this.skillService.toggleSkill(s.skillId, { respectAgentLock: false });
    } catch {
      this.saveError.set(
        `Couldn't save the change to ${s.displayName}. Please try again.`,
      );
    } finally {
      this.saving.set(false);
    }
  }
}
