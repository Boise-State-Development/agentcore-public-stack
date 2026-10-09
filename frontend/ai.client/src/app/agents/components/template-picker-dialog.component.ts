import { Component, ChangeDetectionStrategy, inject, signal, OnInit } from '@angular/core';
import { DialogRef } from '@angular/cdk/dialog';
import { Router } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArrowRight, heroArrowPath } from '@ng-icons/heroicons/outline';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';
import {
  AGENT_TEMPLATE_DRAFT_KEY,
  TemplateCatalogEntry,
  TemplateDraft,
} from '../agent-form/agent-templates';
import { AgentTemplatesService } from '../services/agent-templates.service';

/**
 * "Start from a template" — the front door for Agent Template Prefill.
 *
 * Loads the curated templates from the backend catalog (`GET /templates/` via
 * `AgentTemplatesService`) — the template DATA is no longer hardcoded in the client.
 * Choosing one:
 *   1. writes that template's `draft` JSON to `localStorage[AGENT_TEMPLATE_DRAFT_KEY]`, then
 *   2. routes to the create-agent form (`/agents/new`, create mode = no id).
 *
 * This component owns ONLY load + write + navigate. The form's `ngOnInit` read/parse/
 * reconcile/populate side lives in `agent-form.page.ts` and is deliberately not touched
 * here — the two halves meet only at the key name and the route, both asserted by spec.
 */
@Component({
  selector: 'app-template-picker-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, NgIcon],
  providers: [provideIcons({ heroArrowRight, heroArrowPath })],
  host: { class: 'block' },
  template: `
    <app-dialog-shell
      title="Start from a template"
      description="Pick a starting point. We'll open the agent builder pre-filled — you finish it, add your documents, and save."
      (closed)="onClose()"
    >
      @if (loading()) {
        <!-- Loading -->
        <div
          class="mt-2 flex items-center justify-center gap-2 py-10 text-sm text-gray-500 dark:text-gray-400"
          role="status"
          aria-live="polite"
        >
          <ng-icon name="heroArrowPath" class="size-5 animate-spin" aria-hidden="true" />
          <span>Loading templates…</span>
        </div>
      } @else if (loadError()) {
        <!-- Error -->
        <div class="mt-2 rounded-2xl border border-gray-200 bg-gray-50 p-6 text-center dark:border-gray-700 dark:bg-gray-800/50">
          <p class="text-sm text-gray-700 dark:text-gray-300">
            We couldn't load the templates. Please try again.
          </p>
          <button
            type="button"
            (click)="retry()"
            class="mt-3 inline-flex items-center gap-1.5 rounded-md bg-primary-600 px-3 py-1.5 text-sm font-semibold text-white hover:bg-primary-500 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
          >
            <ng-icon name="heroArrowPath" class="size-4" aria-hidden="true" />
            Try again
          </button>
        </div>
      } @else if (templates().length === 0) {
        <!-- Empty -->
        <div class="mt-2 rounded-2xl border border-dashed border-gray-300 p-6 text-center dark:border-gray-600">
          <p class="text-sm text-gray-500 dark:text-gray-400">
            No templates are available yet. You can start from a blank agent instead.
          </p>
          <button
            type="button"
            (click)="onClose()"
            class="mt-3 inline-flex items-center rounded-md bg-white px-3 py-1.5 text-sm font-semibold text-gray-900 ring-1 ring-gray-300 ring-inset hover:bg-gray-50 dark:bg-gray-700 dark:text-white dark:ring-gray-600 dark:hover:bg-gray-600"
          >
            Start from scratch
          </button>
        </div>
      } @else {
        <!-- Template list -->
        <ul class="mt-2 space-y-3">
          @for (entry of templates(); track entry.draft.templateId) {
            <li>
              <button
                type="button"
                (click)="onSelect(entry.draft)"
                class="group flex w-full items-center gap-3 rounded-2xl border border-gray-200 bg-white p-4 text-left transition duration-150 hover:border-primary-400 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-700 dark:bg-gray-800 dark:hover:border-primary-500 dark:hover:bg-gray-700/50"
              >
                <span
                  class="grid size-11 shrink-0 place-items-center rounded-xl bg-gray-100 text-2xl dark:bg-gray-700"
                  aria-hidden="true"
                >
                  {{ entry.draft.emoji }}
                </span>
                <span class="min-w-0 flex-1">
                  <span class="block truncate text-sm font-semibold text-gray-900 dark:text-white">
                    {{ entry.draft.name }}
                  </span>
                  <span class="mt-0.5 block text-sm/5 text-gray-500 dark:text-gray-400">
                    {{ entry.pitch }}
                  </span>
                </span>
                <ng-icon
                  name="heroArrowRight"
                  class="size-5 shrink-0 text-gray-300 transition-transform duration-150 group-hover:translate-x-0.5 group-hover:text-primary-500 dark:text-gray-600"
                  aria-hidden="true"
                />
              </button>
            </li>
          }
        </ul>
      }
    </app-dialog-shell>
  `,
})
export class TemplatePickerDialogComponent implements OnInit {
  private readonly dialogRef = inject(DialogRef);
  private readonly router = inject(Router);
  private readonly templatesService = inject(AgentTemplatesService);

  /** The catalog fetched from the backend. Empty until the load resolves. */
  readonly templates = signal<TemplateCatalogEntry[]>([]);
  readonly loading = signal(true);
  readonly loadError = signal(false);

  ngOnInit(): void {
    this.load();
  }

  private load(): void {
    this.loading.set(true);
    this.loadError.set(false);
    this.templatesService
      .loadTemplates()
      .then((list) => this.templates.set(list))
      .catch((err: unknown) => {
        console.error('Failed to load agent templates:', err);
        this.loadError.set(true);
      })
      .finally(() => this.loading.set(false));
  }

  /** Re-fetch after a load error. */
  retry(): void {
    this.load();
  }

  /**
   * Hand a template off to the create-agent form: stash the payload in `localStorage`,
   * navigate to the create route, then close. The form reads the key on init, populates
   * itself, and clears the key. This handoff is unchanged from the hardcoded-array
   * version — only the source of the list moved to the backend.
   */
  onSelect(draft: TemplateDraft): void {
    try {
      localStorage.setItem(AGENT_TEMPLATE_DRAFT_KEY, JSON.stringify(draft));
    } catch (err) {
      // A blocked/full localStorage shouldn't strand the user — fall through and still
      // open the (empty) builder rather than swallowing the click silently.
      console.error('Failed to stash template draft:', err);
    }
    this.dialogRef.close();
    void this.router.navigate(['/agents/new']);
  }

  onClose(): void {
    this.dialogRef.close();
  }
}
