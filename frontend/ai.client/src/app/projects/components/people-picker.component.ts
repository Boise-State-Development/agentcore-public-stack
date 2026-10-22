import { ChangeDetectionStrategy, Component, DestroyRef, computed, inject, input, output, signal } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Subject, catchError, debounceTime, distinctUntilChanged, of, switchMap } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroChevronDown, heroXMark } from '@ng-icons/heroicons/outline';
import { DirectoryPerson, MemberRole } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';

const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

export interface PeoplePickerSubmit {
  emails: string[];
  role: MemberRole;
}

/**
 * Find people for a project, or paste a list of emails (shared-projects §9.1).
 *
 * The directory only knows people who have signed in, so anything that looks like
 * an email can be added whether it matches or not: membership is keyed by email and
 * starts the moment they sign in. People already in the project are shown but
 * can't be picked again (`memberRole` from `/directory`).
 */
@Component({
  selector: 'app-people-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroChevronDown, heroXMark })],
  template: `
    <div class="space-y-3">
      <div class="relative">
        <label for="people-picker-input" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Add people</label>
        <input
          id="people-picker-input"
          type="text"
          autocomplete="off"
          role="combobox"
          aria-autocomplete="list"
          [attr.aria-expanded]="showResults()"
          aria-controls="people-picker-results"
          aria-describedby="people-picker-hint"
          [value]="query()"
          (input)="onQuery($any($event.target).value)"
          (paste)="onPaste($event)"
          (keydown.enter)="$event.preventDefault(); addTyped()"
          placeholder="Name or email — or paste a list of emails"
          class="mt-1 block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-500 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-400"
        />
        <p id="people-picker-hint" class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400">
          Can’t find someone? Enter their email. They’ll have access as soon as they sign in.
        </p>

        @if (showResults()) {
          <ul
            id="people-picker-results"
            role="listbox"
            aria-label="People"
            class="mt-2 max-h-64 divide-y divide-gray-200 overflow-y-auto rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800"
          >
            @for (person of results(); track person.email) {
              <li>
                <button
                  type="button"
                  role="option"
                  [attr.aria-selected]="false"
                  [disabled]="!!person.memberRole || isStaged(person.email)"
                  (click)="stage([person.email])"
                  class="flex w-full items-center justify-between gap-3 px-4 py-2.5 text-left text-sm/6 hover:bg-gray-50 focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-default disabled:hover:bg-transparent dark:hover:bg-gray-700/50"
                >
                  <span class="min-w-0">
                    <span class="block truncate font-medium text-gray-900 dark:text-white">{{ person.name || person.email }}</span>
                    @if (person.name) {
                      <span class="block truncate text-xs/5 text-gray-600 dark:text-gray-400">{{ person.email }}</span>
                    } @else if (!person.hasSignedIn) {
                      <span class="block text-xs/5 text-gray-600 dark:text-gray-400">Hasn’t signed in yet</span>
                    }
                  </span>
                  <span class="shrink-0 text-xs/5 text-gray-600 dark:text-gray-400">
                    @if (person.memberRole) { Already a member } @else if (isStaged(person.email)) { Added }
                  </span>
                </button>
              </li>
            } @empty {
              <li class="px-4 py-2.5 text-sm/6 text-gray-600 dark:text-gray-400">
                {{ searching() ? 'Searching…' : 'Nobody by that name. Try their email.' }}
              </li>
            }
          </ul>
        }
      </div>

      @if (staged().length > 0) {
        <div>
          <p class="sr-only" aria-live="polite">{{ staged().length }} {{ staged().length === 1 ? 'person' : 'people' }} ready to add</p>
          <ul class="flex flex-wrap gap-2" aria-label="Ready to add">
            @for (email of staged(); track email) {
              <li class="inline-flex items-center gap-1 rounded-full border border-gray-300 bg-white py-0.5 pr-1 pl-2.5 text-xs/5 font-medium text-gray-700 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200">
                {{ email }}
                <button
                  type="button"
                  (click)="unstage(email)"
                  [attr.aria-label]="'Remove ' + email"
                  class="grid size-5 place-items-center rounded-full text-gray-500 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-gray-600 dark:hover:text-white"
                >
                  <ng-icon name="heroXMark" class="size-3.5" aria-hidden="true" />
                </button>
              </li>
            }
          </ul>
          <div class="mt-3 flex flex-wrap items-center gap-3">
            <label for="people-picker-role" class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">As</label>
            <div class="relative inline-flex">
              <select
                id="people-picker-role"
                [value]="role()"
                (change)="role.set($any($event.target).value)"
                class="appearance-none rounded-2xl border border-gray-300 bg-white py-1.5 pr-8 pl-3 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
              >
                <option value="viewer">Viewer — can use the project</option>
                <option value="editor">Editor — can also change it</option>
              </select>
              <ng-icon name="heroChevronDown" class="pointer-events-none absolute top-1/2 right-2.5 size-3.5 -translate-y-1/2 text-gray-500 dark:text-gray-400" aria-hidden="true" />
            </div>
            <button
              type="button"
              (click)="submit()"
              [disabled]="busy()"
              class="rounded-2xl bg-primary-accessible px-4 py-1.5 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {{ busy() ? 'Adding…' : 'Add ' + staged().length }}
            </button>
          </div>
        </div>
      }
    </div>
  `,
})
export class PeoplePickerComponent {
  private api = inject(ProjectApiService);

  readonly projectId = input.required<string>();
  readonly busy = input(false);
  readonly submitted = output<PeoplePickerSubmit>();

  protected readonly query = signal('');
  protected readonly results = signal<DirectoryPerson[]>([]);
  protected readonly searching = signal(false);
  protected readonly staged = signal<string[]>([]);
  protected readonly role = signal<MemberRole>('viewer');
  protected readonly showResults = computed(() => this.query().trim().length >= 2);

  private readonly queries = new Subject<string>();

  constructor() {
    this.queries
      .pipe(
        debounceTime(250),
        distinctUntilChanged(),
        switchMap(q => {
          if (q.length < 2) return of({ people: [] as DirectoryPerson[] });
          this.searching.set(true);
          return this.api.directory(this.projectId(), q).pipe(catchError(() => of({ people: [] as DirectoryPerson[] })));
        }),
        takeUntilDestroyed(inject(DestroyRef)),
      )
      .subscribe(response => {
        this.searching.set(false);
        this.results.set(response.people);
      });
  }

  protected onQuery(value: string): void {
    this.query.set(value);
    this.queries.next(value.trim());
  }

  /** A pasted list becomes staged people at once, instead of a query nobody would match. */
  protected onPaste(event: ClipboardEvent): void {
    const text = event.clipboardData?.getData('text') ?? '';
    const emails = splitEmails(text);
    if (emails.length > 1) {
      event.preventDefault();
      this.stage(emails);
    }
  }

  protected addTyped(): void {
    const emails = splitEmails(this.query());
    if (emails.length) this.stage(emails);
  }

  protected stage(emails: string[]): void {
    const normalized = emails.map(e => e.trim().toLowerCase()).filter(e => EMAIL.test(e));
    this.staged.update(current => [...new Set([...current, ...normalized])]);
    this.query.set('');
    this.results.set([]);
    this.queries.next('');
  }

  protected unstage(email: string): void {
    this.staged.update(current => current.filter(e => e !== email));
  }

  protected isStaged(email: string): boolean {
    return this.staged().includes(email.toLowerCase());
  }

  protected submit(): void {
    if (!this.staged().length || this.busy()) return;
    this.submitted.emit({ emails: this.staged(), role: this.role() });
  }

  /** Called by the parent once the people were added. */
  reset(): void {
    this.staged.set([]);
    this.role.set('viewer');
  }
}

export function splitEmails(text: string): string[] {
  return text
    .split(/[\s,;]+/)
    .map(part => part.replace(/^<|>$/g, '').trim())
    .filter(part => EMAIL.test(part));
}
