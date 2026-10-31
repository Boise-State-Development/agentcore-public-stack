import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  inject,
  input,
  model,
  output,
  signal,
} from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Subject, catchError, debounceTime, distinctUntilChanged, of, switchMap } from 'rxjs';
import { UserApiService } from '../../../users/services/user-api.service';
import { UserSearchResult } from '../../../assistants/models/assistant.model';

let nextId = 0;

/**
 * Pick a person by email or name through the users directory (spec §6.4).
 *
 * The override form's bare user-id box is the model of what not to do: an
 * admin knows a person's email, never their id, and a mistyped id grants quota
 * to nobody. This searches `GET /users/search` (any signed-in user may call it,
 * so a delegated `admin.quota_requests` approver without `admin.users` can use
 * it too) and only ever yields a row the directory returned.
 *
 * An ARIA 1.2 combobox: arrow keys move through the options, Enter picks,
 * Escape closes the list.
 */
@Component({
  selector: 'app-quota-user-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block' },
  template: `
    <label [for]="inputId" class="block text-sm/6 font-medium text-gray-900 dark:text-white">{{ label() }}</label>
    @if (selected(); as user) {
      <div class="mt-2 flex items-center justify-between gap-3 rounded-2xl border border-gray-300 px-3 py-2 dark:border-gray-600">
        <div class="min-w-0">
          <p class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ user.name || user.email }}</p>
          @if (user.name) {
            <p class="truncate text-xs/5 text-gray-600 dark:text-gray-400">{{ user.email }}</p>
          }
        </div>
        @if (!locked()) {
          <button
            type="button"
            [id]="inputId"
            (click)="clear()"
            class="shrink-0 rounded-2xl px-2 py-1 text-sm/6 font-medium text-primary-accessible hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-primary-accessible-dark"
          >
            Change
          </button>
        }
      </div>
    } @else {
      <div class="relative mt-2">
        <input
          [id]="inputId"
          type="text"
          role="combobox"
          autocomplete="off"
          aria-autocomplete="list"
          [attr.aria-expanded]="open()"
          [attr.aria-controls]="listId"
          [attr.aria-activedescendant]="activeIndex() >= 0 ? optionId(activeIndex()) : null"
          [value]="query()"
          (input)="onInput($event)"
          (keydown)="onKeydown($event)"
          placeholder="Search by email or name"
          class="block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-accessible focus:outline-none focus:ring-2 focus:ring-primary-accessible dark:border-gray-600 dark:bg-gray-900 dark:text-white dark:placeholder:text-gray-500"
        />
        @if (open()) {
          <ul
            [id]="listId"
            role="listbox"
            aria-label="Matching people"
            class="absolute z-20 mt-1 max-h-56 w-full overflow-y-auto rounded-2xl border border-gray-200 bg-white py-1 shadow-lg dark:border-gray-700 dark:bg-gray-800"
          >
            @for (user of results(); track user.userId; let i = $index) {
              <li
                [id]="optionId(i)"
                role="option"
                [attr.aria-selected]="i === activeIndex()"
                (mousedown)="$event.preventDefault()"
                (click)="pick(user)"
                class="cursor-pointer px-3 py-2 text-sm/6"
                [class]="i === activeIndex() ? 'bg-gray-100 dark:bg-white/10' : ''"
              >
                <span class="block truncate font-medium text-gray-900 dark:text-white">{{ user.name || user.email }}</span>
                @if (user.name) {
                  <span class="block truncate text-xs/5 text-gray-600 dark:text-gray-400">{{ user.email }}</span>
                }
              </li>
            }
          </ul>
        }
      </div>
      <p class="mt-1 text-xs/5 text-gray-600 dark:text-gray-400" aria-live="polite">{{ status() }}</p>
    }
  `,
})
export class QuotaUserPickerComponent {
  private users = inject(UserApiService);

  readonly label = input('User');
  /** Fixed to the person whose page this is: shown, not changeable. */
  readonly locked = input(false);
  readonly selected = model<UserSearchResult | null>(null);
  /** For adding to a list: emit the pick and go back to an empty search box. */
  readonly clearOnPick = input(false);
  readonly picked = output<UserSearchResult>();

  readonly inputId = `quota-user-picker-${nextId++}`;
  readonly listId = `${this.inputId}-list`;

  readonly query = signal('');
  readonly results = signal<UserSearchResult[]>([]);
  readonly open = signal(false);
  readonly activeIndex = signal(-1);
  readonly status = signal('');

  private readonly queries = new Subject<string>();

  constructor() {
    this.queries
      .pipe(
        debounceTime(250),
        distinctUntilChanged(),
        switchMap(q => {
          if (q.length < 2) return of(null);
          this.status.set('Searching…');
          return this.users.searchUsers(q, 10).pipe(catchError(() => of({ users: [], failed: true })));
        }),
        takeUntilDestroyed(inject(DestroyRef)),
      )
      .subscribe(response => {
        if (response === null) {
          this.results.set([]);
          this.open.set(false);
          this.status.set('');
          return;
        }
        const users = response.users ?? [];
        this.results.set(users);
        this.activeIndex.set(users.length ? 0 : -1);
        this.open.set(users.length > 0);
        if ('failed' in response) this.status.set('The directory search failed. Try again.');
        else this.status.set(users.length ? `${users.length} matching` : 'No one matches that. Try their full email.');
      });
  }

  optionId(index: number): string {
    return `${this.inputId}-option-${index}`;
  }

  onInput(event: Event): void {
    const value = (event.target as HTMLInputElement).value;
    this.query.set(value);
    this.queries.next(value.trim());
  }

  onKeydown(event: KeyboardEvent): void {
    const count = this.results().length;
    switch (event.key) {
      case 'ArrowDown':
        if (!count) return;
        event.preventDefault();
        this.open.set(true);
        this.activeIndex.update(i => (i + 1) % count);
        break;
      case 'ArrowUp':
        if (!count) return;
        event.preventDefault();
        this.open.set(true);
        this.activeIndex.update(i => (i <= 0 ? count - 1 : i - 1));
        break;
      case 'Enter':
        if (this.open() && this.activeIndex() >= 0) {
          event.preventDefault();
          this.pick(this.results()[this.activeIndex()]);
        }
        break;
      case 'Escape':
        if (this.open()) {
          // Close the list, not the dialog around it.
          event.stopPropagation();
          this.open.set(false);
        }
        break;
    }
  }

  pick(user: UserSearchResult): void {
    this.open.set(false);
    this.results.set([]);
    this.picked.emit(user);
    if (this.clearOnPick()) {
      this.query.set('');
      this.status.set(`Added ${user.email}.`);
      return;
    }
    this.selected.set(user);
    this.status.set('');
  }

  clear(): void {
    this.selected.set(null);
    this.query.set('');
  }
}
