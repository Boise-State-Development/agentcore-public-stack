import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
  OnInit,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { firstValueFrom } from 'rxjs';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroXMark,
  heroClipboard,
  heroArrowUpOnSquare,
  heroCheck,
} from '@ng-icons/heroicons/outline';
import {
  MAX_SHARE_NOTE_CHARS,
  ShareAccessLevel,
  ShareNotify,
  ShareService,
  ShareResponse,
} from '../../services/share/share.service';
import { ProjectApiService } from '../../../projects/services/project-api.service';
import { ProjectMember } from '../../../projects/models/project.model';
import { DialogShellComponent } from '../../../components/dialog/dialog-shell.component';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';

export interface ShareModalData {
  sessionId: string;
  ownerEmail: string;
  /**
   * The session's `preferences.projectId`, when it is a task in a project.
   * Adds the "Project members" option (and makes it the default).
   */
  projectId?: string | null;
}

type AccessLevel = ShareAccessLevel;

interface AccessOption {
  value: AccessLevel;
  label: string;
  description: string;
}

const BASE_ACCESS_OPTIONS: AccessOption[] = [
  { value: 'public', label: 'Public link', description: 'Any authenticated user with the link can view' },
  { value: 'specific', label: 'Limited share', description: 'Only you and designated email addresses can view' },
];

/** Who a project share tells. Nobody by default: a large project shouldn't get a bell per share. */
type NotifyMode = 'none' | 'all' | 'some';

const NOTIFY_OPTIONS: { value: NotifyMode; label: string }[] = [
  { value: 'none', label: 'No one' },
  { value: 'all', label: 'Everyone' },
  { value: 'some', label: 'Choose people' },
];

const PROJECT_ACCESS_OPTION: AccessOption = {
  value: 'project',
  label: 'Project members',
  description: 'Everyone in this task’s project can view it and continue it in their own task',
};

@Component({
  selector: 'app-share-modal',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, FormsModule, NgIcon, SpinnerComponent],
  providers: [
    provideIcons({ heroXMark, heroClipboard, heroArrowUpOnSquare, heroCheck }),
  ],
  host: { class: 'block' },
  template: `
    <app-dialog-shell title="Share conversation" (closed)="onClose()">
      <div
        dialogIcon
        class="flex size-10 shrink-0 items-center justify-center rounded-2xl bg-gray-100 dark:bg-gray-700"
      >
        <ng-icon name="heroArrowUpOnSquare" class="size-5 text-gray-500 dark:text-gray-400" aria-hidden="true" />
      </div>

      <!-- Access level options -->
      <fieldset class="space-y-2">
        <legend class="sr-only">Access level</legend>

        @for (option of accessOptions; track option.value) {
          <label
            class="flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors"
            [class]="selectedAccess() === option.value
              ? 'border-primary-500 bg-gray-100 dark:border-primary-400 dark:bg-gray-700'
              : 'border-gray-200 hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-white/5'"
          >
            <input
              type="radio"
              name="accessLevel"
              [value]="option.value"
              [checked]="selectedAccess() === option.value"
              [attr.cdkFocusInitial]="selectedAccess() === option.value ? '' : null"
              (change)="selectedAccess.set(option.value)"
              class="mt-0.5 size-4 text-primary-600 focus:ring-primary-500"
            />
            <div>
              <span class="text-sm font-medium text-gray-900 dark:text-white">{{ option.label }}</span>
              <p class="text-xs text-gray-600 dark:text-gray-300">{{ option.description }}</p>
            </div>
          </label>
        }
      </fieldset>

      <!-- Email input (specific access) -->
      @if (selectedAccess() === 'specific') {
        <div class="mt-4">
          <label for="email-input" class="block text-sm font-medium text-gray-700 dark:text-gray-300 mb-1">
            People with access
          </label>

          <!-- Email chips -->
          <div class="flex flex-wrap gap-1.5 mb-2">
            <!-- Owner chip (non-removable) -->
            <span class="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2.5 py-0.5 text-xs font-medium text-primary-accessible dark:bg-gray-700 dark:text-primary-50">
              {{ data.ownerEmail }} (you)
            </span>

            @for (email of allowedEmails(); track email) {
              <span class="inline-flex items-center gap-1 rounded-full bg-gray-100 px-2.5 py-0.5 text-xs font-medium text-gray-700 dark:bg-gray-700 dark:text-gray-300">
                {{ email }}
                <button
                  type="button"
                  (click)="removeEmail(email)"
                  class="ml-0.5 inline-flex size-3.5 items-center justify-center rounded-full hover:bg-gray-200 dark:hover:bg-gray-600"
                  [attr.aria-label]="'Remove ' + email"
                >
                  <ng-icon name="heroXMark" class="size-3" aria-hidden="true" />
                </button>
              </span>
            }
          </div>

          <!-- Email input -->
          <div class="flex gap-2">
            <input
              id="email-input"
              type="email"
              placeholder="Enter email address"
              [ngModel]="emailInput()"
              (ngModelChange)="emailInput.set($event)"
              (keydown.enter)="addEmail($event)"
              class="flex-1 rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500 dark:focus:border-primary-400 dark:focus:ring-primary-400"
            />
            <button
              type="button"
              (click)="addEmail()"
              [disabled]="!emailInput().trim()"
              class="rounded-2xl bg-primary-accessible px-3 py-1.5 text-sm font-medium text-white hover:brightness-95 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              Add
            </button>
          </div>
        </div>
      }

      <!-- Let people know (project shares) -->
      @if (selectedAccess() === 'project' && !shareResult()) {
        <div class="mt-4 space-y-3">
          <fieldset>
            <legend class="text-sm font-medium text-gray-700 dark:text-gray-300">Let people know</legend>
            <div class="mt-1.5 flex flex-wrap gap-x-5 gap-y-1.5">
              @for (option of notifyOptions; track option.value) {
                <label class="inline-flex cursor-pointer items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                  <input
                    type="radio"
                    name="notifyMode"
                    [value]="option.value"
                    [checked]="notifyMode() === option.value"
                    (change)="setNotifyMode(option.value)"
                    class="size-4 text-primary-600 focus:ring-primary-500"
                  />
                  {{ option.label }}
                </label>
              }
            </div>
          </fieldset>

          @if (notifyMode() === 'some') {
            <div>
              <label for="notify-filter" class="sr-only">Find a project member</label>
              <input
                id="notify-filter"
                type="search"
                autocomplete="off"
                placeholder="Find a project member"
                [value]="memberFilter()"
                (input)="memberFilter.set($any($event.target).value)"
                class="block w-full rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 placeholder:text-gray-500 focus:border-primary-500 focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-400"
              />
              @if (membersError()) {
                <p class="mt-2 text-xs text-state-danger-700 dark:text-state-danger-300">{{ membersError() }}</p>
              } @else if (!members()) {
                <p class="mt-2 text-xs text-gray-600 dark:text-gray-400">Loading members…</p>
              } @else {
                <ul
                  aria-label="Project members to notify"
                  class="mt-2 max-h-44 divide-y divide-gray-200 overflow-y-auto rounded-md border border-gray-200 dark:divide-gray-700 dark:border-gray-700"
                >
                  @for (member of filteredMembers(); track member.email) {
                    <li>
                      <label class="flex cursor-pointer items-center gap-3 px-3 py-2 hover:bg-gray-50 dark:hover:bg-white/5">
                        <input
                          type="checkbox"
                          [checked]="isChosen(member.email)"
                          (change)="toggleMember(member.email)"
                          class="size-4 rounded text-primary-600 focus:ring-primary-500"
                        />
                        <span class="min-w-0">
                          <span class="block truncate text-sm font-medium text-gray-900 dark:text-white">{{ member.name || member.email }}</span>
                          @if (member.name) {
                            <span class="block truncate text-xs text-gray-600 dark:text-gray-400">{{ member.email }}</span>
                          }
                        </span>
                      </label>
                    </li>
                  } @empty {
                    <li class="px-3 py-2 text-xs text-gray-600 dark:text-gray-400">
                      {{ others().length ? 'No members match.' : 'Nobody else is in this project yet.' }}
                    </li>
                  }
                </ul>
                <p class="mt-1 text-xs text-gray-600 dark:text-gray-400" aria-live="polite">
                  {{ chosen().length }} {{ chosen().length === 1 ? 'person' : 'people' }} selected
                </p>
              }
            </div>
          }

          <div>
            <label for="share-note" class="block text-sm font-medium text-gray-700 dark:text-gray-300">
              Note (optional)
            </label>
            <textarea
              id="share-note"
              rows="2"
              [attr.maxlength]="maxNoteChars"
              [value]="note()"
              (input)="note.set($any($event.target).value)"
              aria-describedby="share-note-hint"
              placeholder="What should they look at?"
              class="mt-1 block w-full resize-none rounded-md border border-gray-300 bg-white px-3 py-1.5 text-sm text-gray-900 placeholder:text-gray-500 focus:border-primary-500 focus:ring-1 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-400"
            ></textarea>
            <p id="share-note-hint" class="mt-1 flex justify-between gap-3 text-xs text-gray-600 dark:text-gray-400">
              <span>Shown with the task{{ notifyMode() === 'none' ? '' : ' and in the notification' }}.</span>
              <span>{{ maxNoteChars - note().length }} left</span>
            </p>
          </div>
        </div>
      }

      <!-- Existing shares info -->
      @if (existingShares().length > 0 && !shareResult()) {
        <div class="mt-4 rounded-md bg-state-info-50 p-3 dark:bg-state-info-500/10">
          <p class="text-xs text-state-info-700 dark:text-state-info-300">
            @if (replacesProjectShare()) {
              This task is already shared with the project. Sharing again replaces the snapshot the project sees.
            } @else {
              This conversation has {{ existingShares().length }} existing share{{ existingShares().length > 1 ? 's' : '' }}.
              Creating a new share will add another snapshot.
            }
          </p>
        </div>
      }

      <!-- Share result -->
      @if (shareResult()) {
        <div class="mt-4 rounded-md bg-state-success-50 p-3 dark:bg-state-success-500/10">
          <p class="text-sm font-medium text-state-success-800 dark:text-state-success-300 mb-2">
            {{ shareResult()!.accessLevel === 'project' ? 'Shared with the project' : 'Chat shared' }}
          </p>
          <p class="text-xs text-state-success-700 dark:text-state-success-400 mb-2">
            Future messages aren't included in the share.
            @if (shareResult()!.accessLevel === 'project') {
              It’s listed under Shared tasks in the project, replacing any earlier share of this task.
              @if (notifiedSummary()) {
                {{ notifiedSummary() }}
              }
            }
          </p>
          <div class="flex items-center gap-2">
            <input
              type="text"
              readonly
              aria-label="Share link"
              [value]="shareUrl()"
              class="flex-1 rounded-md border border-state-success-200 bg-white px-2.5 py-1.5 text-xs text-gray-700 dark:border-state-success-700 dark:bg-gray-700 dark:text-gray-300"
              (click)="$event.target"
            />
            <button
              type="button"
              (click)="copyLink()"
              class="inline-flex items-center gap-1 rounded-2xl bg-white px-2.5 py-1.5 text-xs font-medium text-gray-700 shadow-xs ring-1 ring-gray-300 ring-inset hover:bg-gray-50 dark:bg-gray-700 dark:text-gray-300 dark:ring-gray-600 dark:hover:bg-gray-600"
            >
              <ng-icon [name]="copied() ? 'heroCheck' : 'heroClipboard'" class="size-3.5" aria-hidden="true" />
              {{ copied() ? 'Copied' : 'Copy link' }}
            </button>
          </div>
        </div>
      }

      <!-- Error -->
      @if (error()) {
        <div class="mt-4 rounded-md bg-state-danger-50 p-3 dark:bg-state-danger-500/10">
          <p class="text-sm text-state-danger-700 dark:text-state-danger-300">{{ error() }}</p>
        </div>
      }

      <div dialogFooter class="flex justify-end gap-3 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        <button
          type="button"
          (click)="onClose()"
          class="rounded-2xl bg-white px-3 py-2 text-sm/6 font-semibold text-gray-900 shadow-xs ring-1 ring-gray-300 ring-inset hover:bg-gray-50 dark:bg-white/10 dark:text-white dark:shadow-none dark:ring-white/5 dark:hover:bg-white/20"
        >
          {{ shareResult() ? 'Done' : 'Cancel' }}
        </button>

        @if (!shareResult()) {
          <button
            type="button"
            (click)="onShare()"
            [disabled]="isSubmitting() || !canSubmit()"
            class="inline-flex items-center gap-1.5 rounded-2xl bg-primary-accessible px-3 py-2 text-sm/6 font-semibold text-white shadow-xs hover:brightness-95 disabled:opacity-50 disabled:cursor-not-allowed dark:shadow-none"
          >
            @if (isSubmitting()) {
              <app-spinner size="sm" variant="on-solid" label="Creating share link" />
            }
            Create share link
          </button>
        }
      </div>
    </app-dialog-shell>
  `,
})
export class ShareModalComponent implements OnInit {
  private dialogRef = inject(DialogRef<boolean>);
  protected data = inject<ShareModalData>(DIALOG_DATA);
  private shareService = inject(ShareService);
  private projectApi = inject(ProjectApiService);

  // State
  protected selectedAccess = signal<AccessLevel>(this.data.projectId ? 'project' : 'public');
  protected allowedEmails = signal<string[]>([]);
  protected emailInput = signal('');
  protected isSubmitting = signal(false);
  protected error = signal<string | null>(null);
  protected shareResult = signal<ShareResponse | null>(null);
  protected existingShares = signal<ShareResponse[]>([]);
  protected copied = signal(false);

  protected readonly notifyOptions = NOTIFY_OPTIONS;
  protected readonly maxNoteChars = MAX_SHARE_NOTE_CHARS;
  protected notifyMode = signal<NotifyMode>('none');
  protected note = signal('');
  /** The project's members, loaded the first time "Choose people" is picked. */
  protected members = signal<ProjectMember[] | null>(null);
  protected membersError = signal<string | null>(null);
  protected memberFilter = signal('');
  protected chosen = signal<string[]>([]);
  /** Shown after a share that told people: the server skips the sharer and dedupes, so this is what was asked. */
  protected notifiedSummary = signal('');

  /** Everyone the sharer could notify: never themselves. */
  protected others = computed(() => {
    const me = this.data.ownerEmail.toLowerCase();
    return (this.members() ?? []).filter((m) => m.email.toLowerCase() !== me);
  });

  protected filteredMembers = computed(() => {
    const q = this.memberFilter().trim().toLowerCase();
    if (!q) return this.others();
    return this.others().filter((m) => m.email.toLowerCase().includes(q) || (m.name ?? '').toLowerCase().includes(q));
  });

  protected readonly accessOptions: AccessOption[] = this.data.projectId
    ? [PROJECT_ACCESS_OPTION, ...BASE_ACCESS_OPTIONS]
    : BASE_ACCESS_OPTIONS;

  /**
   * A project share is listed once per task, newest wins (`SHARED_TASK#` pointer), so
   * sharing to the project again swaps what members see rather than adding a second entry.
   */
  protected replacesProjectShare = computed(
    () => this.selectedAccess() === 'project' && this.existingShares().some((s) => s.accessLevel === 'project'),
  );

  protected shareUrl = computed(() => {
    const result = this.shareResult();
    if (!result) return '';
    return `${window.location.origin}/shared/${result.shareId}`;
  });

  protected canSubmit = computed(() => {
    // For 'specific' access, owner email is always included automatically,
    // so sharing with just yourself (no additional emails) is valid.
    // "Choose people" with nobody chosen is a mistake, not "tell no one".
    return !(this.selectedAccess() === 'project' && this.notifyMode() === 'some' && this.chosen().length === 0);
  });

  async ngOnInit(): Promise<void> {
    try {
      const response = await this.shareService.listSharesForSession(this.data.sessionId);
      this.existingShares.set(response.shares);
    } catch {
      // No existing shares — that's fine
    }
  }

  protected addEmail(event?: Event): void {
    event?.preventDefault();
    const email = this.emailInput().trim().toLowerCase();
    if (!email || !email.includes('@')) return;
    if (email === this.data.ownerEmail.toLowerCase()) return;
    if (this.allowedEmails().includes(email)) return;

    this.allowedEmails.update((list: string[]) => [...list, email]);
    this.emailInput.set('');
  }

  protected setNotifyMode(mode: NotifyMode): void {
    this.notifyMode.set(mode);
    if (mode === 'some' && !this.members()) void this.loadMembers();
  }

  private async loadMembers(): Promise<void> {
    const projectId = this.data.projectId;
    if (!projectId) return;
    this.membersError.set(null);
    try {
      const response = await firstValueFrom(this.projectApi.members(projectId));
      this.members.set(response.members);
    } catch {
      this.membersError.set('Couldn’t load the project’s members. Choose Everyone, or try again later.');
    }
  }

  protected isChosen(email: string): boolean {
    return this.chosen().includes(email.toLowerCase());
  }

  protected toggleMember(email: string): void {
    const key = email.toLowerCase();
    this.chosen.update((list) => (list.includes(key) ? list.filter((e) => e !== key) : [...list, key]));
  }

  private notifyRequest(): ShareNotify | undefined {
    if (this.selectedAccess() !== 'project') return undefined;
    if (this.notifyMode() === 'all') return { all: true };
    if (this.notifyMode() === 'some' && this.chosen().length) return { emails: this.chosen() };
    return undefined;
  }

  protected removeEmail(email: string): void {
    this.allowedEmails.update((list: string[]) => list.filter((e: string) => e !== email));
  }

  protected async onShare(): Promise<void> {
    this.isSubmitting.set(true);
    this.error.set(null);

    try {
      const emails =
        this.selectedAccess() === 'specific'
          ? [this.data.ownerEmail, ...this.allowedEmails()]
          : undefined;

      // The failure is shown below, with the API's own sentence (for a
      // project share: "you're no longer a member" 403, "archived" 409,
      // "Not members of this project: …" 400).
      const notify = this.notifyRequest();
      const result = await this.shareService.createShare(
        this.data.sessionId,
        this.selectedAccess(),
        emails,
        {
          suppressErrorToast: true,
          notify,
          note: this.selectedAccess() === 'project' ? this.note().trim() || undefined : undefined,
        },
      );

      this.notifiedSummary.set(
        !notify
          ? ''
          : 'all' in notify
            ? 'Everyone in the project will be notified.'
            : `${notify.emails.length} ${notify.emails.length === 1 ? 'person' : 'people'} will be notified.`,
      );
      this.shareResult.set(result);
      this.existingShares.update(shares => [...shares, result]);
    } catch (err: unknown) {
      const errorDetail = (err as any)?.error?.detail;
      this.error.set(
        errorDetail || 'Failed to create share. Please try again.'
      );
    } finally {
      this.isSubmitting.set(false);
    }
  }

  protected async copyLink(): Promise<void> {
    try {
      await navigator.clipboard.writeText(this.shareUrl());
      this.copied.set(true);
      setTimeout(() => this.copied.set(false), 2000);
    } catch {
      // Fallback: select the input text so the user can copy manually
      const input = document.querySelector<HTMLInputElement>(
        'input[readonly][type="text"]'
      );
      if (input) {
        input.select();
        input.setSelectionRange(0, input.value.length);
      }
      this.error.set('Could not copy automatically. Please copy the selected link manually.');
    }
  }

  protected onClose(): void {
    this.dialogRef.close(!!this.shareResult());
  }
}
