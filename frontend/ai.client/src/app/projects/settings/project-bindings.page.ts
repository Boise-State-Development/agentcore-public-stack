import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { formatDate } from '@angular/common';
import { firstValueFrom } from 'rxjs';
import { BindableItem } from '../../agents/models/agent.model';
import { AgentService } from '../../agents/services/agent.service';
import { ToolSelectorComponent } from '../../components/tool-selector/tool-selector.component';
import { ToolSelectorItem } from '../../components/tool-selector/tool-selector.model';
import { ToastService } from '../../services/toast/toast.service';
import { BindingRef, BindingsResponse, BoundBinding } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';
import { injectProjectPage } from './project-page-state';
import { ProjectSettingsShellComponent } from './project-settings-shell.component';
import { HasUnsavedChanges } from './unsaved-changes.guard';

export type BindingKind = 'tools' | 'skills';

const KIND_COPY: Record<BindingKind, { title: string; description: string; noun: string; nounPlural: string; saved: string }> = {
  tools: {
    title: 'Tools',
    description: 'What the project’s assistant can use in every conversation. Members without access to a tool get the assistant without it.',
    noun: 'tool',
    nounPlural: 'tools',
    saved: 'Tools saved',
  },
  skills: {
    title: 'Skills',
    description:
      'Playbooks the project’s assistant can follow in every conversation. A skill stays on the version it was added at until someone updates it.',
    noun: 'skill',
    nounPlural: 'skills',
    saved: 'Skills saved',
  },
};

/** Shown on a row someone else bound, which the caller can't use and can't remove. */
const FOREIGN_NOTE = 'Added by someone else. You don’t have access to it, but it stays.';

function sameRefs(a: ReadonlySet<string>, b: string[]): boolean {
  return a.size === b.length && b.every(ref => a.has(ref));
}

/**
 * `/projects/:id/tools` and `/projects/:id/skills` — pick the project assistant's
 * tools, or its skills. The kind comes from the route's `data`. Save writes the
 * selection as a settings version; the page asks before a link leaves it unsaved.
 *
 * A tool or skill the caller can't use themselves stays listed if someone else added
 * it, and a save keeps it: the server only checks what a save adds.
 *
 * Skills show the version each one runs. Update moves a pin to the skill's current
 * content and is saved straight away, apart from Save, like any settings change.
 */
@Component({
  selector: 'app-project-bindings-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ProjectSettingsShellComponent, ToolSelectorComponent],
  host: { '(window:beforeunload)': 'onBeforeUnload($event)' },
  template: `
    <app-project-settings-shell
      [project]="state.project()"
      [loading]="state.loading() || (!loaded() && !loadError())"
      [error]="state.error()"
      [heading]="copy().title"
      [description]="copy().description"
    >
      @if (state.canEdit() && loaded()) {
        <div shellActions class="flex items-center gap-3">
          <button
            type="button"
            (click)="discard()"
            [disabled]="!dirty() || saving()"
            class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
          >
            Discard changes
          </button>
          <button
            type="button"
            (click)="save()"
            [disabled]="!dirty() || saving()"
            class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {{ saving() ? 'Saving…' : 'Save ' + copy().nounPlural }}
          </button>
        </div>
      }

      @if (state.project()) {
        @if (loadError()) {
          <p role="alert" class="text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ loadError() }}</p>
        } @else {
          @if (error()) {
            <p role="alert" class="mb-4 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
          }
          <app-tool-selector
            [items]="items()"
            [selected]="selected()"
            (selectedChange)="selected.set($event)"
            (itemAction)="updatePin($event)"
            [label]="copy().title"
            [noun]="copy().noun"
            [nounPlural]="copy().nounPlural"
            [disabled]="!state.canEdit()"
            [emptyText]="'None are available to you.'"
            maxHeight="lg"
          />
        }
      }
    </app-project-settings-shell>
  `,
})
export class ProjectBindingsPage implements HasUnsavedChanges {
  private api = inject(ProjectApiService);
  private agents = inject(AgentService);
  private toast = inject(ToastService);

  /** Route param and route data (`withComponentInputBinding`). */
  readonly id = input.required<string>();
  readonly kind = input.required<BindingKind>();

  protected readonly state = injectProjectPage(this.id);
  protected readonly copy = computed(() => KIND_COPY[this.kind()]);

  private readonly choices = signal<ToolSelectorItem[]>([]);
  /** What the project binds as last saved, which Discard returns to. */
  private readonly saved = signal<string[]>([]);
  protected readonly selected = signal<ReadonlySet<string>>(new Set());
  private readonly pins = signal<Record<string, BoundBinding>>({});
  /** The skill whose pin is being updated. */
  private readonly pinning = signal<string | null>(null);
  protected readonly loaded = signal(false);
  protected readonly loadError = signal<string | null>(null);
  protected readonly saving = signal(false);
  protected readonly error = signal<string | null>(null);

  protected readonly items = computed<ToolSelectorItem[]>(() => {
    const choices = this.choices();
    if (this.kind() !== 'skills') return choices;
    const saved = this.saved();
    const pins = this.pins();
    const pinning = this.pinning();
    const busy = pinning !== null || this.saving();
    const canEdit = this.state.canEdit();
    return choices.map(item => {
      const pin = saved.includes(item.id) ? pins[item.id] : undefined;
      if (!pin) return item;
      const line = pinLine(pin);
      const offer = canEdit && !item.locked && (pin.updateAvailable || !pin.version);
      return {
        ...item,
        note: item.locked ? `${line} ${FOREIGN_NOTE}` : line,
        noteTone: pin.updateAvailable ? ('warning' as const) : ('muted' as const),
        action: offer
          ? {
              label: pinning === item.id ? 'Updating…' : pin.version ? 'Update' : 'Pin this version',
              ariaLabel: (pin.version ? 'Update ' : 'Pin the current version of ') + item.name,
              disabled: busy,
            }
          : undefined,
      };
    });
  });

  protected readonly dirty = computed(() => this.loaded() && !sameRefs(this.selected(), this.saved()));

  constructor() {
    effect(() => {
      const id = this.id();
      const kind = this.kind();
      untracked(() => void this.load(id, kind));
    });
  }

  private async load(projectId: string, kind: BindingKind): Promise<void> {
    this.loaded.set(false);
    this.loadError.set(null);
    this.error.set(null);
    try {
      const [bound, palette] = await Promise.all([
        firstValueFrom(this.api.bindings(projectId, kind)),
        this.agents.loadBindable(kind === 'tools' ? 'tool' : 'skill'),
      ]);
      if (projectId !== this.id() || kind !== this.kind()) return;
      const refs = bound.bindings.map(b => b.ref);
      this.choices.set(choices(refs, palette));
      this.saved.set(refs);
      this.selected.set(new Set(refs));
      this.pins.set(pinsOf(bound));
      this.loaded.set(true);
    } catch (err) {
      if (projectId === this.id()) this.loadError.set(projectErrorMessage(err, `The ${KIND_COPY[kind].nounPlural} could not be loaded.`));
    }
  }

  hasUnsavedChanges(): boolean {
    return this.dirty();
  }

  protected onBeforeUnload(event: BeforeUnloadEvent): void {
    if (this.dirty()) event.preventDefault();
  }

  protected discard(): void {
    this.selected.set(new Set(this.saved()));
    this.error.set(null);
  }

  protected async save(): Promise<void> {
    if (!this.dirty() || this.saving()) return;
    this.saving.set(true);
    this.error.set(null);
    try {
      const selected = this.selected();
      // Keep the palette's order so an unchanged selection is an unchanged list.
      const bindings: BindingRef[] = this.choices().filter(c => selected.has(c.id)).map(c => ({ ref: c.id }));
      const written = await firstValueFrom(this.api.saveBindings(this.id(), this.kind(), bindings));
      this.adopt(written);
      this.toast.success(this.copy().saved);
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'That change could not be saved.'));
    } finally {
      this.saving.set(false);
    }
  }

  /**
   * Move one skill's pin to its current content. Saved straight away, like any settings
   * change; unsaved checkbox changes are left as they are.
   */
  protected async updatePin(item: ToolSelectorItem): Promise<void> {
    if (this.kind() !== 'skills' || this.pinning() !== null) return;
    this.pinning.set(item.id);
    this.error.set(null);
    try {
      const written = await firstValueFrom(this.api.pinSkill(this.id(), item.id));
      this.pins.set(pinsOf(written));
      this.toast.success(`${item.name} updated`);
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'That skill could not be updated.'));
    } finally {
      this.pinning.set(null);
    }
  }

  /** A save's response is the new saved state. */
  private adopt(written: BindingsResponse): void {
    const refs = written.bindings.map(b => b.ref);
    this.saved.set(refs);
    this.selected.set(new Set(refs));
    this.pins.set(pinsOf(written));
  }
}

/** Pin details by ref, from a skills response. */
export function pinsOf(response: BindingsResponse): Record<string, BoundBinding> {
  return Object.fromEntries(response.bindings.map(b => [b.ref, b]));
}

/** "Version 3 · since Oct 8, 2026 · A newer version is available", or the pre-pin line. */
function pinLine(pin: BoundBinding): string {
  if (!pin.version) return 'Uses the latest version.';
  const parts = [`Version ${pin.version}`];
  if (pin.pinnedAt) parts.push(`since ${formatDate(pin.pinnedAt, 'MMM d, y', 'en-US')}`);
  if (pin.updateAvailable) parts.push('A newer version is available');
  return `${parts.join(' · ')}.`;
}

/**
 * The palette in its own order (a save keeps that order, so an unchanged selection is
 * an unchanged list), then anything bound that the caller can't use, locked as it is.
 */
function choices(bound: string[], palette: BindableItem[]): ToolSelectorItem[] {
  const known = new Set(palette.map(p => p.ref));
  return [
    ...palette.map(p => ({ id: p.ref, name: p.label, description: p.description, group: category(p) })),
    ...bound
      .filter(ref => !known.has(ref))
      .map(ref => ({ id: ref, name: ref, note: FOREIGN_NOTE, noteTone: 'muted' as const, locked: true })),
  ];
}

function category(item: BindableItem): string | undefined {
  const raw = item.meta?.['category'];
  return typeof raw === 'string' && raw.trim() ? raw : undefined;
}
