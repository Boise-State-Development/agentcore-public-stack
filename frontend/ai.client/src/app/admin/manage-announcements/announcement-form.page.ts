import {
  ChangeDetectionStrategy,
  Component,
  OnInit,
  computed,
  inject,
  signal,
} from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import { FormControl, FormGroup, ReactiveFormsModule, Validators } from '@angular/forms';
import { MarkdownComponent } from 'ngx-markdown';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowLeft } from '@ng-icons/heroicons/outline';
import { AnnouncementsAdminService } from './services/announcements-admin.service';
import {
  AnnouncementCreateRequest,
  AnnouncementSeverity,
  AnnouncementSurface,
} from './models/announcement.model';
import { AppRolesService } from '../roles/services/app-roles.service';
import { SpinnerComponent } from '../../components/spinner/spinner.component';

const URL_PATTERN = /^https?:\/\/.+/i;
const TITLE_MAX = 140;
const BODY_MAX_BYTES = 16 * 1024;

/** Bytes, not characters — the server caps `body_markdown` at 16 KB of UTF-8. */
function byteLength(value: string): number {
  return new TextEncoder().encode(value).length;
}

/**
 * Author or edit one announcement.
 *
 * Two rules the server enforces are mirrored here so the admin finds out
 * before submitting rather than via a 422:
 *  - `expires_at` is required when a banner or modal is selected (§5). An
 *    unbounded loud surface is announcement fatigue with no backstop.
 *  - `cta_label` and `cta_url` travel together, and the URL must be http(s).
 *
 * The panel checkbox is deliberately fixed on: the server forces it anyway
 * (§D1), and showing it as a disabled, checked box explains *why* far better
 * than silently adding it after save.
 */
@Component({
  selector: 'app-announcement-form-page',
  imports: [RouterLink, ReactiveFormsModule, MarkdownComponent, NgIcon, SpinnerComponent],
  providers: [provideIcons({ heroArrowLeft })],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="max-w-4xl">
      <a
        routerLink="/admin/manage-announcements"
        class="mb-6 inline-flex items-center gap-2 text-sm/6 font-medium text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
      >
        <ng-icon name="heroArrowLeft" class="size-4" />
        Back to Announcements
      </a>

      <h1 class="mb-6 text-3xl/9 font-bold text-gray-900 dark:text-white">
        {{ isEdit() ? 'Edit announcement' : 'New announcement' }}
      </h1>

      @if (loadError()) {
        <div class="mb-4 rounded-sm border border-state-danger-300 bg-state-danger-50 p-4 text-sm/6 text-state-danger-700 dark:border-state-danger-700 dark:bg-state-danger-900/20 dark:text-state-danger-300">
          {{ loadError() }}
        </div>
      }

      <form [formGroup]="form" (ngSubmit)="onSubmit()" class="space-y-6">
        <!-- Content -->
        <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
          <div class="grid grid-cols-1 gap-4">
            <div>
              <label for="title" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                Title <span class="text-state-danger-600">*</span>
              </label>
              <input
                id="title"
                type="text"
                formControlName="title"
                [maxlength]="TITLE_MAX"
                class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-500 dark:bg-gray-700 dark:text-white"
                placeholder="e.g. Mid-turn steering is here"
              />
              <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                Used as the banner line and the What's New heading. {{ titleRemaining() }} characters left.
              </p>
              @if (showError('title')) {
                <p class="mt-1 text-xs text-state-danger-600 dark:text-state-danger-400">Title is required.</p>
              }
            </div>

            <div>
              <label for="summary" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                Banner summary
              </label>
              <input
                id="summary"
                type="text"
                formControlName="summary"
                maxlength="280"
                class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-500 dark:bg-gray-700 dark:text-white"
                placeholder="Optional one-liner, used when the title is too long for a banner"
              />
            </div>
          </div>
        </div>

        <!-- Body + live preview -->
        <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
          <label for="body_markdown" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
            Body (Markdown) <span class="text-state-danger-600">*</span>
          </label>
          <p class="mt-1 mb-3 text-xs text-gray-500 dark:text-gray-400">
            Shown in What's New and in the modal. Supports CommonMark: headings, lists,
            tables, links, code, emphasis. Markdown is sanitized on render.
          </p>
          <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
            <textarea
              id="body_markdown"
              formControlName="body_markdown"
              rows="14"
              class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 font-mono text-sm text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-500 dark:bg-gray-700 dark:text-white"
              placeholder="Say what changed, and what the user can now do.&#10;&#10;- One&#10;- Two"
            ></textarea>
            <div class="rounded-sm border border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-700 dark:bg-gray-900">
              <p class="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Preview</p>
              <!-- message-block is the app's markdown stylesheet, so this
                   preview matches what the panel actually renders. The prose
                   classes would be inert: the Tailwind typography plugin is
                   not installed. -->
              <div class="message-block text-sm/6 text-gray-700 dark:text-gray-300">
                <markdown [data]="previewMarkdown()" />
              </div>
            </div>
          </div>
          <p class="mt-1 text-xs" [class]="bodyOverLimit() ? 'text-state-danger-600 dark:text-state-danger-400' : 'text-gray-500 dark:text-gray-400'">
            {{ bodyBytes() }} / {{ BODY_MAX_BYTES }} bytes
          </p>
          @if (showError('body_markdown')) {
            <p class="mt-1 text-xs text-state-danger-600 dark:text-state-danger-400">Body is required.</p>
          }
        </div>

        <!-- Surfaces + severity -->
        <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
          <fieldset>
            <legend class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">How loudly?</legend>
            <p class="mt-1 mb-3 text-xs text-gray-500 dark:text-gray-400">
              Users see at most one banner and one modal at a time, whichever is most
              severe and oldest.
            </p>

            <div class="space-y-3">
              <div class="flex items-start gap-2">
                <input
                  id="surface-panel"
                  type="checkbox"
                  checked
                  disabled
                  class="mt-1 size-4 rounded border-gray-300 text-primary-600 focus:ring-primary-500"
                />
                <label for="surface-panel" class="text-sm/6 text-gray-700 dark:text-gray-300">
                  <span class="font-medium">What's New panel</span> — always on
                  <span class="block text-xs text-gray-500 dark:text-gray-400">
                    Every announcement is browsable from the user menu, so dismissing a
                    banner or modal never destroys the information.
                  </span>
                </label>
              </div>

              <div class="flex items-start gap-2">
                <input
                  id="surface-banner"
                  type="checkbox"
                  formControlName="banner"
                  class="mt-1 size-4 rounded border-gray-300 text-primary-600 focus:ring-primary-500"
                />
                <label for="surface-banner" class="text-sm/6 text-gray-700 dark:text-gray-300">
                  <span class="font-medium">Banner</span> — a strip below the top nav
                  <span class="block text-xs text-gray-500 dark:text-gray-400">
                    Ambient. Dismissible with a ✕.
                  </span>
                </label>
              </div>

              <div class="flex items-start gap-2">
                <input
                  id="surface-modal"
                  type="checkbox"
                  formControlName="modal"
                  class="mt-1 size-4 rounded border-gray-300 text-primary-600 focus:ring-primary-500"
                />
                <label for="surface-modal" class="text-sm/6 text-gray-700 dark:text-gray-300">
                  <span class="font-medium">Modal</span> — a dialog on next load
                  <span class="block text-xs text-gray-500 dark:text-gray-400">
                    Interruptive. Reserve it for policy changes. It never opens over an
                    in-progress response.
                  </span>
                </label>
              </div>

              @if (modalSelected()) {
                <div class="ml-6 flex items-start gap-2">
                  <input
                    id="requires_ack"
                    type="checkbox"
                    formControlName="requires_ack"
                    class="mt-1 size-4 rounded border-gray-300 text-primary-600 focus:ring-primary-500"
                  />
                  <label for="requires_ack" class="text-sm/6 text-gray-700 dark:text-gray-300">
                    Require an explicit acknowledgement
                    <span class="block text-xs text-gray-500 dark:text-gray-400">
                      The modal cannot be dismissed by clicking away or pressing Escape,
                      and the acknowledgement is kept as a durable record.
                    </span>
                  </label>
                </div>
              }
            </div>
          </fieldset>

          <div class="mt-5">
            <label for="severity" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
              Severity
            </label>
            <select
              id="severity"
              formControlName="severity"
              class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 md:w-64 dark:border-gray-500 dark:bg-gray-700 dark:text-white"
            >
              <option value="info">Info</option>
              <option value="success">Success</option>
              <option value="warning">Warning</option>
            </select>
            <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
              Sets the banner colour, and breaks the tie when more than one wants the slot.
            </p>
          </div>
        </div>

        <!-- Schedule -->
        <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
          <div class="grid grid-cols-1 gap-4 md:grid-cols-2">
            <div>
              <label for="publish_at" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                Publish at
              </label>
              <input
                id="publish_at"
                type="datetime-local"
                formControlName="publish_at"
                class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-500 dark:bg-gray-700 dark:text-white"
              />
              <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                Leave blank for now. Nothing is visible until you press Publish, whatever
                this says.
              </p>
            </div>

            <div>
              <label for="expires_at" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                Expires at
                @if (loudSurfaceSelected()) {
                  <span class="text-state-danger-600">*</span>
                }
              </label>
              <input
                id="expires_at"
                type="datetime-local"
                formControlName="expires_at"
                class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-500 dark:bg-gray-700 dark:text-white"
              />
              <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                @if (loudSurfaceSelected()) {
                  Required for a banner or modal — a loud surface has to say when it stops.
                } @else {
                  Optional. The panel entry stays browsable either way.
                }
              </p>
              @if (expiryMissing()) {
                <p class="mt-1 text-xs text-state-danger-600 dark:text-state-danger-400">
                  An expiry is required when a banner or modal is selected.
                </p>
              }
              @if (expiryBeforePublish()) {
                <p class="mt-1 text-xs text-state-danger-600 dark:text-state-danger-400">
                  The expiry must be after the publish date.
                </p>
              }
            </div>
          </div>
        </div>

        <!-- Audience -->
        <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
          <fieldset>
            <legend class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">Who sees it?</legend>
            <p class="mt-1 mb-3 text-xs text-gray-500 dark:text-gray-400">
              This filters who the announcement is shown to. It grants nothing and
              changes no permissions.
            </p>

            <div class="flex items-center gap-2">
              <input
                id="all_roles"
                type="checkbox"
                formControlName="all_roles"
                class="size-4 rounded border-gray-300 text-primary-600 focus:ring-primary-500"
              />
              <label for="all_roles" class="text-sm/6 text-gray-700 dark:text-gray-300">Everyone</label>
            </div>

            @if (!allRolesSelected()) {
              <div class="mt-3">
                @if (roles().length === 0) {
                  <p class="text-xs text-state-warning-600 dark:text-state-warning-400">
                    No roles available. Everyone will see this announcement.
                  </p>
                } @else {
                  <div class="flex flex-wrap gap-2">
                    @for (role of roles(); track role.roleId) {
                      <button
                        type="button"
                        (click)="toggleRole(role.roleId)"
                        [attr.aria-pressed]="isRoleSelected(role.roleId)"
                        class="rounded-sm border px-2.5 py-1 text-xs/5 font-medium focus:outline-none focus:ring-2 focus:ring-primary-500"
                        [class]="roleChipClass(role.roleId)"
                      >
                        {{ role.displayName || role.roleId }}
                      </button>
                    }
                  </div>
                  @if (selectedRoles().length === 0) {
                    <p class="mt-2 text-xs text-state-danger-600 dark:text-state-danger-400">
                      Pick at least one role, or choose Everyone.
                    </p>
                  }
                }
              </div>
            }

            <div class="mt-5 flex items-start gap-2">
              <input
                id="show_to_new_users"
                type="checkbox"
                formControlName="show_to_new_users"
                class="mt-1 size-4 rounded border-gray-300 text-primary-600 focus:ring-primary-500"
              />
              <label for="show_to_new_users" class="text-sm/6 text-gray-700 dark:text-gray-300">
                Also show to people who join later
                <span class="block text-xs text-gray-500 dark:text-gray-400">
                  Off by default, and usually right to leave off. Someone who signs up next
                  year should not be met with a queue of notices about features that, to
                  them, have always existed. Turn it on only for a standing notice that
                  genuinely applies to everyone who ever joins — an acceptable-use policy,
                  not a feature launch.
                </span>
              </label>
            </div>
          </fieldset>
        </div>

        <!-- Call to action -->
        <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
          <div class="grid grid-cols-1 gap-4 md:grid-cols-2">
            <div>
              <label for="cta_label" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                Link label
              </label>
              <input
                id="cta_label"
                type="text"
                formControlName="cta_label"
                maxlength="64"
                placeholder="e.g. Read the docs"
                class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-500 dark:bg-gray-700 dark:text-white"
              />
            </div>
            <div>
              <label for="cta_url" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                Link URL
              </label>
              <input
                id="cta_url"
                type="url"
                formControlName="cta_url"
                maxlength="2048"
                placeholder="https://example.com/whats-new"
                class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-500 dark:bg-gray-700 dark:text-white"
              />
              @if (ctaIncomplete()) {
                <p class="mt-1 text-xs text-state-danger-600 dark:text-state-danger-400">
                  A label and an http(s) URL must be given together, or both left blank.
                </p>
              }
            </div>
          </div>
        </div>

        @if (submitError()) {
          <div class="rounded-sm border border-state-danger-300 bg-state-danger-50 p-4 text-sm/6 text-state-danger-700 dark:border-state-danger-700 dark:bg-state-danger-900/20 dark:text-state-danger-300">
            {{ submitError() }}
          </div>
        }

        <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p class="text-xs text-gray-500 dark:text-gray-400">
            @if (isEdit()) {
              Editing does not re-show this to anyone who already dismissed it. Use
              <strong>Show again</strong> on the list for that.
            } @else {
              Saved as a draft. Publish it from the list when you are ready.
            }
          </p>
          <div class="flex justify-end gap-3">
            <a
              routerLink="/admin/manage-announcements"
              class="rounded-sm border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-gray-500 dark:border-gray-500 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
            >
              Cancel
            </a>
            <button
              type="submit"
              [disabled]="!canSubmit()"
              class="inline-flex items-center gap-2 rounded-sm bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus:outline-none focus:ring-2 focus:ring-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
            >
              @if (isSubmitting()) {
                <app-spinner size="sm" variant="on-solid" label="Saving" />
              }
              {{ isEdit() ? 'Save changes' : 'Create draft' }}
            </button>
          </div>
        </div>
      </form>
    </div>
  `,
})
export class AnnouncementFormPage implements OnInit {
  private readonly service = inject(AnnouncementsAdminService);
  private readonly rolesService = inject(AppRolesService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  protected readonly TITLE_MAX = TITLE_MAX;
  protected readonly BODY_MAX_BYTES = BODY_MAX_BYTES;

  protected readonly form = new FormGroup({
    title: new FormControl<string>('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(TITLE_MAX)],
    }),
    summary: new FormControl<string>('', { nonNullable: true }),
    body_markdown: new FormControl<string>('', {
      nonNullable: true,
      validators: [Validators.required],
    }),
    banner: new FormControl<boolean>(false, { nonNullable: true }),
    modal: new FormControl<boolean>(false, { nonNullable: true }),
    requires_ack: new FormControl<boolean>(false, { nonNullable: true }),
    severity: new FormControl<AnnouncementSeverity>('info', { nonNullable: true }),
    publish_at: new FormControl<string>('', { nonNullable: true }),
    expires_at: new FormControl<string>('', { nonNullable: true }),
    all_roles: new FormControl<boolean>(true, { nonNullable: true }),
    show_to_new_users: new FormControl<boolean>(false, { nonNullable: true }),
    cta_label: new FormControl<string>('', { nonNullable: true }),
    cta_url: new FormControl<string>('', { nonNullable: true }),
  });

  protected readonly isSubmitting = signal(false);
  protected readonly submitError = signal<string | null>(null);
  protected readonly loadError = signal<string | null>(null);
  private readonly editingId = signal<string | null>(null);
  protected readonly isEdit = computed(() => this.editingId() !== null);
  protected readonly selectedRoles = signal<string[]>([]);

  // FormControl.valueChanges is rxjs, so mirror what the template reacts to.
  private readonly titleSig = signal('');
  private readonly bodySig = signal('');
  private readonly bannerSig = signal(false);
  private readonly modalSig = signal(false);
  private readonly allRolesSig = signal(true);
  private readonly publishAtSig = signal('');
  private readonly expiresAtSig = signal('');
  private readonly ctaLabelSig = signal('');
  private readonly ctaUrlSig = signal('');
  // Form validity is not a signal on FormGroup, so mirror it like the rest.
  private readonly formValid = signal(false);

  protected readonly modalSelected = this.modalSig.asReadonly();
  protected readonly allRolesSelected = this.allRolesSig.asReadonly();

  protected readonly previewMarkdown = computed(
    () => this.bodySig() || '*(nothing to preview yet)*',
  );
  protected readonly titleRemaining = computed(() =>
    Math.max(0, TITLE_MAX - this.titleSig().length),
  );
  protected readonly bodyBytes = computed(() => byteLength(this.bodySig()));
  protected readonly bodyOverLimit = computed(() => this.bodyBytes() > BODY_MAX_BYTES);
  protected readonly loudSurfaceSelected = computed(
    () => this.bannerSig() || this.modalSig(),
  );
  protected readonly expiryMissing = computed(
    () => this.loudSurfaceSelected() && !this.expiresAtSig(),
  );
  protected readonly expiryBeforePublish = computed(() => {
    const publish = this.publishAtSig();
    const expires = this.expiresAtSig();
    if (!publish || !expires) return false;
    return new Date(expires).getTime() <= new Date(publish).getTime();
  });
  protected readonly ctaIncomplete = computed(() => {
    const label = this.ctaLabelSig().trim();
    const url = this.ctaUrlSig().trim();
    if (!label && !url) return false;
    if (!label || !url) return true;
    return !URL_PATTERN.test(url);
  });

  protected readonly roles = computed(() => this.rolesService.getRoles());

  /**
   * Whether the form can be submitted.
   *
   * ⚠️ **Every dependency is read unconditionally, and form validity comes from
   * a signal.** Both details are load-bearing, and getting either wrong
   * deadlocks the button.
   *
   * A `computed` tracks the signals actually read during its *last* execution,
   * so an early `return` shortens its dependency set. This began as a chain of
   * guard clauses with `if (this.form.invalid) return false` near the top —
   * and `FormGroup.invalid` is a plain getter, not a signal. On the first
   * evaluation the form was empty, so it returned there having read only
   * `isSubmitting()`; nothing else was tracked, no later edit could schedule a
   * recompute, and `isSubmitting` only changes inside `onSubmit`, which the
   * disabled button prevented. The submit button could never enable.
   *
   * So: mirror validity into `formValid` (fed by `statusChanges`), read every
   * input before combining them, and never guard-clause out of this computed.
   */
  protected readonly canSubmit = computed(() => {
    const submitting = this.isSubmitting();
    const formValid = this.formValid();
    const overLimit = this.bodyOverLimit();
    const missingExpiry = this.expiryMissing();
    const badExpiry = this.expiryBeforePublish();
    const badCta = this.ctaIncomplete();
    const rolesChosen =
      this.allRolesSig() || this.roles().length === 0 || this.selectedRoles().length > 0;

    return (
      !submitting && formValid && !overLimit && !missingExpiry && !badExpiry &&
      !badCta && rolesChosen
    );
  });

  async ngOnInit(): Promise<void> {
    const c = this.form.controls;
    c.title.valueChanges.subscribe(v => this.titleSig.set(v));
    c.body_markdown.valueChanges.subscribe(v => this.bodySig.set(v));
    c.banner.valueChanges.subscribe(v => this.bannerSig.set(v));
    c.modal.valueChanges.subscribe(v => {
      this.modalSig.set(v);
      // requiresAck is modal-only; clear it so an unchecked modal cannot leave
      // a stale flag on the record.
      if (!v) c.requires_ack.setValue(false, { emitEvent: false });
    });
    c.all_roles.valueChanges.subscribe(v => this.allRolesSig.set(v));
    c.publish_at.valueChanges.subscribe(v => this.publishAtSig.set(v));
    c.expires_at.valueChanges.subscribe(v => this.expiresAtSig.set(v));
    c.cta_label.valueChanges.subscribe(v => this.ctaLabelSig.set(v));
    c.cta_url.valueChanges.subscribe(v => this.ctaUrlSig.set(v));
    this.form.statusChanges.subscribe(status => this.formValid.set(status === 'VALID'));
    this.formValid.set(this.form.valid);

    const id = this.route.snapshot.paramMap.get('id');
    if (!id) return;

    this.editingId.set(id);
    try {
      const a = await this.service.get(id);
      const targeted = (a.target_roles ?? []).filter(r => r !== '*');
      const everyone = (a.target_roles ?? []).includes('*') || targeted.length === 0;

      this.form.patchValue({
        title: a.title,
        summary: a.summary ?? '',
        body_markdown: a.body_markdown,
        banner: a.surfaces.includes('banner'),
        modal: a.surfaces.includes('modal'),
        requires_ack: a.requires_ack,
        severity: a.severity,
        publish_at: toLocalInput(a.publish_at),
        expires_at: toLocalInput(a.expires_at),
        all_roles: everyone,
        show_to_new_users: a.show_to_new_users,
        cta_label: a.cta_label ?? '',
        cta_url: a.cta_url ?? '',
      });
      this.selectedRoles.set(targeted);
    } catch (err) {
      this.loadError.set(
        err instanceof Error ? err.message : 'Failed to load the announcement.',
      );
    }
  }

  protected isRoleSelected(roleId: string): boolean {
    return this.selectedRoles().includes(roleId);
  }

  protected toggleRole(roleId: string): void {
    this.selectedRoles.update(prev =>
      prev.includes(roleId) ? prev.filter(r => r !== roleId) : [...prev, roleId],
    );
  }

  protected roleChipClass(roleId: string): string {
    return this.isRoleSelected(roleId)
      ? 'border-primary-600 bg-primary-600 text-white'
      : 'border-gray-300 bg-white text-gray-700 hover:bg-gray-100 dark:border-gray-500 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600';
  }

  protected showError(name: 'title' | 'body_markdown'): boolean {
    const c = this.form.get(name);
    return !!c && c.invalid && (c.touched || c.dirty);
  }

  protected async onSubmit(): Promise<void> {
    if (!this.canSubmit()) {
      this.form.markAllAsTouched();
      return;
    }
    this.isSubmitting.set(true);
    this.submitError.set(null);

    const raw = this.form.getRawValue();
    const surfaces: AnnouncementSurface[] = ['panel'];
    if (raw.banner) surfaces.push('banner');
    if (raw.modal) surfaces.push('modal');

    const label = raw.cta_label.trim();
    const url = raw.cta_url.trim();

    const payload: AnnouncementCreateRequest = {
      title: raw.title.trim(),
      body_markdown: raw.body_markdown,
      summary: raw.summary.trim() || null,
      surfaces,
      severity: raw.severity,
      state: 'draft',
      publish_at: toIsoOrNull(raw.publish_at),
      expires_at: toIsoOrNull(raw.expires_at),
      target_roles: raw.all_roles ? ['*'] : this.selectedRoles(),
      show_to_new_users: raw.show_to_new_users,
      requires_ack: raw.modal && raw.requires_ack,
      cta_label: label || null,
      cta_url: url || null,
    };

    try {
      const id = this.editingId();
      if (id) {
        // `state` is not sent on an edit — publish/archive own that transition.
        const { state: _state, ...updates } = payload;
        await this.service.update(id, updates);
      } else {
        await this.service.create(payload);
      }
      this.router.navigate(['/admin/manage-announcements']);
    } catch (err: unknown) {
      const detail =
        (err as { error?: { detail?: string }; message?: string })?.error?.detail ??
        (err as Error)?.message ??
        'Failed to save the announcement.';
      this.submitError.set(
        typeof detail === 'string' ? detail : 'Failed to save the announcement.',
      );
    } finally {
      this.isSubmitting.set(false);
    }
  }
}

/** ISO-8601 → the `datetime-local` shape, in the admin's own timezone. */
function toLocalInput(iso: string | null | undefined): string {
  if (!iso) return '';
  const date = new Date(iso.replace('+00:00Z', 'Z'));
  if (Number.isNaN(date.getTime())) return '';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

/** `datetime-local` → ISO-8601 UTC, which is what the server stores. */
function toIsoOrNull(local: string): string | null {
  if (!local) return null;
  const date = new Date(local);
  return Number.isNaN(date.getTime()) ? null : date.toISOString();
}
