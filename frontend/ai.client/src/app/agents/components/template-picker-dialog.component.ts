import { Component, ChangeDetectionStrategy, inject, signal, computed, OnInit } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { DialogRef } from '@angular/cdk/dialog';
import { Router } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowRight,
  heroArrowPath,
  heroDocumentDuplicate,
  heroMagnifyingGlass,
} from '@ng-icons/heroicons/outline';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';
import { ToastService } from '../../services/toast/toast.service';
import {
  AGENT_TEMPLATE_DRAFT_KEY,
  TemplateCatalogEntry,
  TemplateDraft,
} from '../agent-form/agent-templates';
import { AgentTemplatesService } from '../services/agent-templates.service';
import { AGENT_DUPLICATED_TOAST, AgentService } from '../services/agent.service';
import { Agent } from '../models/agent.model';
import { AgentIconComponent } from './agent-icon.component';

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
  imports: [DialogShellComponent, NgIcon, AgentIconComponent],
  providers: [provideIcons({ heroArrowRight, heroArrowPath, heroDocumentDuplicate, heroMagnifyingGlass })],
  host: { class: 'block' },
  template: `
    <app-dialog-shell
      title="Start from a template"
      description="Pick a starting point. We'll open the agent builder pre-filled — you finish it, add your documents, and save."
      (closed)="onClose()"
    >
      <div
        role="tablist"
        aria-label="Start from"
        class="inline-flex gap-1 rounded-xl border border-gray-200 bg-gray-100 p-1 dark:border-gray-700 dark:bg-gray-800"
      >
        <button
          type="button"
          role="tab"
          [attr.aria-selected]="tab() === 'templates'"
          (click)="selectTab('templates')"
          class="rounded-lg px-3 py-1 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
          [class]="
            tab() === 'templates'
              ? 'bg-white text-gray-900 shadow-xs dark:bg-gray-900 dark:text-white'
              : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white'
          "
        >
          Templates
        </button>
        <button
          type="button"
          role="tab"
          [attr.aria-selected]="tab() === 'existing'"
          (click)="selectTab('existing')"
          class="rounded-lg px-3 py-1 text-sm font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500"
          [class]="
            tab() === 'existing'
              ? 'bg-white text-gray-900 shadow-xs dark:bg-gray-900 dark:text-white'
              : 'text-gray-500 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white'
          "
        >
          Existing agent
        </button>
      </div>

      @if (tab() === 'templates') {
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
      } @else {
        <!-- Existing agent -->
        <p class="mt-3 text-sm text-gray-500 dark:text-gray-400">
          Creates a private copy you own. Knowledge base files aren't copied. You'll add them to the new agent.
        </p>
        <div class="relative mt-3">
          <ng-icon
            name="heroMagnifyingGlass"
            class="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-gray-400"
            aria-hidden="true"
          />
          <input
            type="search"
            aria-label="Search your agents"
            placeholder="Search your agents"
            [value]="query()"
            (input)="query.set($any($event.target).value)"
            class="w-full rounded-lg border border-gray-300 bg-white py-2 pr-3 pl-9 text-sm text-gray-900 placeholder:text-gray-400 focus-visible:outline-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          />
        </div>
        @if (duplicateError()) {
          <p role="alert" class="mt-3 text-sm text-state-danger-600 dark:text-state-danger-400">{{ duplicateError() }}</p>
        }
        @if (agentsLoading()) {
          <div
            class="mt-2 flex items-center justify-center gap-2 py-10 text-sm text-gray-500 dark:text-gray-400"
            role="status"
            aria-live="polite"
          >
            <ng-icon name="heroArrowPath" class="size-5 animate-spin" aria-hidden="true" />
            <span>Loading your agents…</span>
          </div>
        } @else if (editableAgents().length === 0) {
          <div class="mt-3 rounded-2xl border border-dashed border-gray-300 p-6 text-center dark:border-gray-600">
            <p class="text-sm text-gray-500 dark:text-gray-400">You don't have any agents you can edit yet.</p>
          </div>
        } @else {
          <ul class="mt-3 space-y-3">
            @for (agent of editableAgents(); track agent.agentId) {
              <li>
                <button
                  type="button"
                  (click)="onDuplicate(agent)"
                  [disabled]="duplicating()"
                  class="group flex w-full items-center gap-3 rounded-2xl border border-gray-200 bg-white p-4 text-left transition duration-150 hover:border-primary-400 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50 dark:border-gray-700 dark:bg-gray-800 dark:hover:border-primary-500 dark:hover:bg-gray-700/50"
                >
                  <app-agent-icon [agentId]="agent.agentId" [iconUrl]="agent.iconUrl" [emoji]="agent.emoji" [size]="40" />
                  <span class="min-w-0 flex-1">
                    <span class="block truncate text-sm font-semibold text-gray-900 dark:text-white">{{ agent.name }}</span>
                    <span class="mt-0.5 block truncate text-sm/5 text-gray-500 dark:text-gray-400">{{ agent.description }}</span>
                  </span>
                  <ng-icon
                    name="heroDocumentDuplicate"
                    class="size-5 shrink-0 text-gray-300 group-hover:text-primary-500 dark:text-gray-600"
                    aria-hidden="true"
                  />
                </button>
              </li>
            }
          </ul>
        }
      }
    </app-dialog-shell>
  `,
})
export class TemplatePickerDialogComponent implements OnInit {
  private readonly dialogRef = inject(DialogRef);
  private readonly router = inject(Router);
  private readonly templatesService = inject(AgentTemplatesService);
  private readonly agentService = inject(AgentService);
  private readonly toast = inject(ToastService);

  readonly tab = signal<'templates' | 'existing'>('templates');
  readonly query = signal('');
  readonly duplicating = signal(false);
  readonly duplicateError = signal<string | null>(null);
  readonly agentsLoading = signal(false);

  /** The caller's agents they own or can edit, narrowed by the search box. */
  readonly editableAgents = computed(() => {
    const q = this.query().trim().toLowerCase();
    return this.agentService
      .agents$()
      .filter((a) => ['owner', 'editor'].includes(a.userPermission ?? 'owner'))
      .filter((a) => !q || `${a.name} ${a.description ?? ''}`.toLowerCase().includes(q));
  });

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

  selectTab(tab: 'templates' | 'existing'): void {
    this.tab.set(tab);
    this.duplicateError.set(null);
    if (tab === 'existing' && this.agentService.agents$().length === 0) {
      this.agentsLoading.set(true);
      this.agentService
        .loadAgents(true)
        .catch((err: unknown) => {
          console.error('Failed to load agents:', err);
          this.duplicateError.set("We couldn't load your agents. Please try again.");
        })
        .finally(() => this.agentsLoading.set(false));
    }
  }

  /** Duplicate one of the caller's agents and open the copy in the editor. */
  async onDuplicate(agent: Agent): Promise<void> {
    if (this.duplicating()) return;
    this.duplicating.set(true);
    this.duplicateError.set(null);
    try {
      const copy = await this.agentService.duplicateAgent(agent.agentId);
      this.dialogRef.close();
      void this.router.navigate(['/agents', copy.agentId, 'edit']);
      this.toast.info(AGENT_DUPLICATED_TOAST.title, AGENT_DUPLICATED_TOAST.message);
    } catch (err) {
      console.error('Failed to duplicate agent:', err);
      this.duplicateError.set(
        (err as HttpErrorResponse)?.error?.detail ?? "Couldn't duplicate that agent. Please try again.",
      );
    } finally {
      this.duplicating.set(false);
    }
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
