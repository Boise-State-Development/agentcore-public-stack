import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { ActivatedRoute, Router, RouterLink } from '@angular/router';
import {
  FormControl,
  FormGroup,
  ReactiveFormsModule,
  Validators,
} from '@angular/forms';
import { MarkdownComponent } from 'ngx-markdown';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowLeft } from '@ng-icons/heroicons/outline';
import { UserMenuLinksService } from './services/user-menu-links.service';
import {
  UserMenuLinkFormData,
  UserMenuLinkKind,
} from './models/user-menu-link.model';

const URL_PATTERN = /^https?:\/\/.+/i;

@Component({
  selector: 'app-user-menu-link-form-page',
  imports: [RouterLink, ReactiveFormsModule, MarkdownComponent, NgIcon],
  providers: [provideIcons({ heroArrowLeft })],
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
        <a
          routerLink="/admin/manage-user-menu-links"
          class="mb-6 inline-flex items-center gap-2 text-sm/6 font-medium text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
        >
          <ng-icon name="heroArrowLeft" class="size-4" />
          Back to User Menu Links
        </a>

        <h1 class="mb-6 text-3xl/9 font-bold text-gray-900 dark:text-white">
          {{ isEdit() ? 'Edit user-menu link' : 'New user-menu link' }}
        </h1>

        @if (loadError()) {
          <div class="mb-4 rounded-sm border border-red-300 bg-red-50 p-4 text-sm/6 text-red-700 dark:border-red-700 dark:bg-red-900/20 dark:text-red-300">
            {{ loadError() }}
          </div>
        }

        <form [formGroup]="form" (ngSubmit)="onSubmit()" class="space-y-6">
          <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
            <div class="grid grid-cols-1 gap-4 md:grid-cols-2">
              <div class="md:col-span-2">
                <label for="label" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                  Label <span class="text-red-600">*</span>
                </label>
                <input
                  id="label"
                  type="text"
                  formControlName="label"
                  maxlength="64"
                  class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-500 dark:bg-gray-700 dark:text-white"
                  placeholder="e.g. Privacy policy"
                />
                @if (showError('label')) {
                  <p class="mt-1 text-xs text-red-600 dark:text-red-400">Label is required.</p>
                }
              </div>

              <div>
                <label for="kind" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                  Type
                </label>
                <select
                  id="kind"
                  formControlName="kind"
                  class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-500 dark:bg-gray-700 dark:text-white"
                >
                  <option value="external">External URL (new tab)</option>
                  <option value="modal">In-app modal (rich text)</option>
                </select>
              </div>

              <div>
                <label for="order" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                  Order
                </label>
                <input
                  id="order"
                  type="number"
                  min="0"
                  max="10000"
                  formControlName="order"
                  class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-500 dark:bg-gray-700 dark:text-white"
                />
                <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">Lower numbers appear first.</p>
              </div>

              <div class="md:col-span-2 flex items-center gap-2">
                <input
                  id="enabled"
                  type="checkbox"
                  formControlName="enabled"
                  class="size-4 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
                />
                <label for="enabled" class="text-sm/6 text-gray-700 dark:text-gray-300">
                  Visible to users
                </label>
              </div>
            </div>
          </div>

          @if (kindValue() === 'external') {
            <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
              <label for="url" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                URL <span class="text-red-600">*</span>
              </label>
              <input
                id="url"
                type="url"
                formControlName="url"
                maxlength="2048"
                placeholder="https://example.com/privacy"
                class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-500 dark:bg-gray-700 dark:text-white"
              />
              @if (showError('url')) {
                <p class="mt-1 text-xs text-red-600 dark:text-red-400">A valid http(s) URL is required.</p>
              }
              <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">Opens in a new tab with <code>rel="noopener noreferrer"</code>.</p>
            </div>
          } @else {
            <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
              <label for="body_markdown" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                Body (Markdown) <span class="text-red-600">*</span>
              </label>
              <p class="mt-1 mb-3 text-xs text-gray-500 dark:text-gray-400">
                Supports CommonMark: headings, lists, links, code, emphasis. Links open in a new tab.
              </p>
              <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
                <textarea
                  id="body_markdown"
                  formControlName="body_markdown"
                  rows="14"
                  maxlength="50000"
                  class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 font-mono text-sm text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-500 dark:bg-gray-700 dark:text-white"
                  placeholder="# Welcome&#10;&#10;Some **rich** text..."
                ></textarea>
                <div class="rounded-sm border border-gray-200 bg-gray-50 px-4 py-3 dark:border-gray-700 dark:bg-gray-900">
                  <p class="mb-2 text-xs font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Preview</p>
                  <div class="prose prose-sm max-w-none dark:prose-invert">
                    <markdown [data]="previewMarkdown()" />
                  </div>
                </div>
              </div>
              @if (showError('body_markdown')) {
                <p class="mt-1 text-xs text-red-600 dark:text-red-400">Body is required for modal links.</p>
              }
            </div>
          }

          @if (submitError()) {
            <div class="rounded-sm border border-red-300 bg-red-50 p-4 text-sm/6 text-red-700 dark:border-red-700 dark:bg-red-900/20 dark:text-red-300">
              {{ submitError() }}
            </div>
          }

          <div class="flex justify-end gap-3">
            <a
              routerLink="/admin/manage-user-menu-links"
              class="rounded-sm border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-100 focus:outline-none focus:ring-2 focus:ring-gray-500 dark:border-gray-500 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
            >
              Cancel
            </a>
            <button
              type="submit"
              [disabled]="form.invalid || isSubmitting()"
              class="inline-flex items-center gap-2 rounded-sm bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
            >
              @if (isSubmitting()) {
                <span class="size-4 animate-spin rounded-full border-2 border-white border-t-transparent" aria-hidden="true"></span>
              }
              {{ isEdit() ? 'Save changes' : 'Create link' }}
            </button>
          </div>
        </form>
      </div>
    </div>
  `,
})
export class UserMenuLinkFormPage implements OnInit {
  private readonly service = inject(UserMenuLinksService);
  private readonly route = inject(ActivatedRoute);
  private readonly router = inject(Router);

  protected readonly form = new FormGroup({
    label: new FormControl<string>('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(64)],
    }),
    kind: new FormControl<UserMenuLinkKind>('external', { nonNullable: true }),
    enabled: new FormControl<boolean>(true, { nonNullable: true }),
    order: new FormControl<number>(0, {
      nonNullable: true,
      validators: [Validators.min(0), Validators.max(10_000)],
    }),
    url: new FormControl<string>('', { nonNullable: true }),
    body_markdown: new FormControl<string>('', { nonNullable: true }),
  });

  protected readonly isSubmitting = signal(false);
  protected readonly submitError = signal<string | null>(null);
  protected readonly loadError = signal<string | null>(null);
  private readonly editingId = signal<string | null>(null);
  protected readonly isEdit = computed(() => this.editingId() !== null);

  // Mirrored signals for reactive template (FormControl.valueChanges is rxjs).
  private readonly kindSig = signal<UserMenuLinkKind>('external');
  private readonly bodySig = signal<string>('');
  protected readonly kindValue = this.kindSig.asReadonly();
  protected readonly previewMarkdown = computed(() => this.bodySig() || '*(empty preview)*');

  async ngOnInit(): Promise<void> {
    this.form.controls.kind.valueChanges.subscribe(value => {
      this.kindSig.set(value);
      this.syncKindValidators(value);
    });
    this.form.controls.body_markdown.valueChanges.subscribe(value => {
      this.bodySig.set(value);
    });
    this.syncKindValidators(this.form.controls.kind.value);

    const id = this.route.snapshot.paramMap.get('id');
    if (id) {
      this.editingId.set(id);
      try {
        const link = await this.service.getLink(id);
        this.form.patchValue({
          label: link.label,
          kind: link.kind,
          enabled: link.enabled,
          order: link.order,
          url: link.url ?? '',
          body_markdown: link.body_markdown ?? '',
        });
        this.kindSig.set(link.kind);
        this.bodySig.set(link.body_markdown ?? '');
      } catch (err) {
        this.loadError.set(
          err instanceof Error ? err.message : 'Failed to load link.',
        );
      }
    }
  }

  private syncKindValidators(kind: UserMenuLinkKind): void {
    const url = this.form.controls.url;
    const body = this.form.controls.body_markdown;
    if (kind === 'external') {
      url.setValidators([Validators.required, Validators.pattern(URL_PATTERN)]);
      body.clearValidators();
    } else {
      body.setValidators([Validators.required]);
      url.clearValidators();
    }
    url.updateValueAndValidity({ emitEvent: false });
    body.updateValueAndValidity({ emitEvent: false });
  }

  protected showError(name: 'label' | 'url' | 'body_markdown'): boolean {
    const c = this.form.get(name);
    return !!c && c.invalid && (c.touched || c.dirty);
  }

  protected async onSubmit(): Promise<void> {
    if (this.form.invalid) {
      this.form.markAllAsTouched();
      return;
    }
    this.isSubmitting.set(true);
    this.submitError.set(null);

    const raw = this.form.getRawValue();
    const data: UserMenuLinkFormData = {
      label: raw.label.trim(),
      kind: raw.kind,
      enabled: raw.enabled,
      order: Number(raw.order ?? 0),
      url: raw.kind === 'external' ? raw.url.trim() : null,
      body_markdown: raw.kind === 'modal' ? raw.body_markdown : null,
    };

    try {
      const id = this.editingId();
      if (id) {
        await this.service.updateLink(id, data);
      } else {
        await this.service.createLink(data);
      }
      this.router.navigate(['/admin/manage-user-menu-links']);
    } catch (err: unknown) {
      const detail = (err as { error?: { detail?: string }; message?: string })?.error?.detail
        ?? (err as Error)?.message
        ?? 'Failed to save link.';
      this.submitError.set(detail);
    } finally {
      this.isSubmitting.set(false);
    }
  }
}
