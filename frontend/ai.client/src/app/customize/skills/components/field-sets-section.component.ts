import { ChangeDetectionStrategy, Component, effect, inject, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroPencilSquare, heroPlus, heroExclamationTriangle } from '@ng-icons/heroicons/outline';
import { FieldSetService } from '../services/field-set.service';

/** One row of the section: what the list says, plus what opening the set adds. */
interface FieldSetRow {
  name: string;
  title: string | null;
  fieldCount: number | null;
  version: string | null;
  /** The parser's message when the stored file no longer opens as a form. */
  problem: string | null;
}

/**
 * The **Field sets** section of a skill's edit form (field-set-authoring §3).
 *
 * Lists each set with its title, field count and version, and links to the
 * editor. The list route names the sets without reading them, so each set is
 * then opened to fill in the rest. A skill holds a handful of sets at most,
 * and the reads run in parallel. A set whose file no longer parses still
 * lists, marked, because it is the one most in need of opening.
 *
 * Rendered by the skill form only when `features.fieldSetAuthoring` is on.
 */
@Component({
  selector: 'app-field-sets-section',
  imports: [RouterLink, NgIcon],
  changeDetection: ChangeDetectionStrategy.OnPush,
  viewProviders: [provideIcons({ heroPencilSquare, heroPlus, heroExclamationTriangle })],
  template: `
    <section
      aria-labelledby="field-sets-heading"
      class="rounded-2xl border border-gray-200 p-4 dark:border-gray-700"
    >
      <div class="flex flex-wrap items-center justify-between gap-3">
        <h2 id="field-sets-heading" class="text-sm/6 font-medium text-gray-900 dark:text-white">
          Field sets
        </h2>
        <a
          [routerLink]="['/customize/skills', skillId(), 'field-sets', 'new']"
          class="inline-flex items-center gap-1.5 rounded-2xl border border-gray-300 bg-white px-3 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
        >
          <ng-icon name="heroPlus" class="size-4" aria-hidden="true" />
          New field set
        </a>
      </div>
      <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
        A named list of fields to pull out of documents, as a table. When this skill is active the
        agent can run a set over the documents in a conversation.
      </p>

      @if (loading()) {
        <p role="status" class="mt-4 text-sm/6 text-gray-600 dark:text-gray-400">Loading field sets…</p>
      } @else if (error()) {
        <p role="alert" class="mt-4 text-sm/6 text-state-danger-700 dark:text-state-danger-400">
          {{ error() }}
        </p>
      } @else if (rows().length === 0) {
        <p class="mt-4 text-sm/6 text-gray-600 dark:text-gray-400">No field sets yet.</p>
      } @else {
        <ul role="list" class="mt-4 divide-y divide-gray-100 dark:divide-gray-700">
          @for (row of rows(); track row.name) {
            <li class="flex items-center justify-between gap-3 py-2">
              <div class="min-w-0">
                <p class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">
                  {{ row.title ?? row.name }}
                </p>
                @if (row.problem) {
                  <p class="flex items-center gap-1 text-xs/5 text-state-warning-800 dark:text-state-warning-200">
                    <ng-icon name="heroExclamationTriangle" class="size-3.5 shrink-0" aria-hidden="true" />
                    <span class="truncate">Can't be opened as a form. Open it to fix.</span>
                  </p>
                } @else if (row.fieldCount !== null) {
                  <p class="text-xs/5 text-gray-600 dark:text-gray-400">
                    <span class="font-mono">{{ row.name }}</span>
                    · {{ row.fieldCount }} {{ row.fieldCount === 1 ? 'field' : 'fields' }} · version
                    {{ row.version }}
                  </p>
                }
              </div>
              <a
                [routerLink]="['/customize/skills', skillId(), 'field-sets', row.name]"
                [attr.aria-label]="'Edit ' + (row.title ?? row.name)"
                class="inline-flex shrink-0 items-center gap-1.5 rounded-2xl px-2.5 py-1.5 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-300 dark:hover:bg-gray-700"
              >
                <ng-icon name="heroPencilSquare" class="size-4" aria-hidden="true" />
                Edit
              </a>
            </li>
          }
        </ul>
      }
    </section>
  `,
})
export class FieldSetsSectionComponent {
  private fieldSets = inject(FieldSetService);

  readonly skillId = input.required<string>();

  protected readonly rows = signal<FieldSetRow[]>([]);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);

  constructor() {
    effect(() => {
      void this.load(this.skillId());
    });
  }

  private async load(skillId: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      const listed = await this.fieldSets.list(skillId);
      const rows = await Promise.all(
        listed.fieldSets.map(async (summary): Promise<FieldSetRow> => {
          try {
            const result = await this.fieldSets.read(skillId, summary.name);
            return result.ok
              ? {
                  name: summary.name,
                  title: result.set.title,
                  fieldCount: result.set.fields.length,
                  version: result.set.version,
                  problem: null,
                }
              : { name: summary.name, title: null, fieldCount: null, version: null, problem: result.error.message };
          } catch {
            return { name: summary.name, title: null, fieldCount: null, version: null, problem: null };
          }
        }),
      );
      if (skillId === this.skillId()) {
        this.rows.set(rows);
      }
    } catch (err) {
      this.error.set(this.fieldSets.messageFor(err, "Couldn't load this skill's field sets."));
    } finally {
      this.loading.set(false);
    }
  }
}
