import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { formatDate } from '@angular/common';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { BindableItem } from '../../agents/models/agent.model';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';
import { ToolSelectorComponent } from '../../components/tool-selector/tool-selector.component';
import { ToolSelectorItem } from '../../components/tool-selector/tool-selector.model';
import { ToastService } from '../../services/toast/toast.service';
import { BindingRef, BindingsResponse, BoundBinding } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';

export type BindingKind = 'tools' | 'skills';

export interface ProjectBindingsDialogData {
  projectId: string;
  canEdit: boolean;
  kind: BindingKind;
  /** What the project binds today. */
  bound: string[];
  /** What the caller may bind (`AgentService.loadBindable`). */
  palette: BindableItem[];
  /** Skills only: the version each bound skill runs, by ref. */
  pins?: Record<string, BoundBinding>;
}

/**
 * The project's bindings of this kind after the last write: a Save, or for skills an
 * Update that moved a pin. `undefined` when nothing was written.
 */
export type ProjectBindingsDialogResult = BindingsResponse | undefined;

const KIND_COPY: Record<BindingKind, { title: string; description: string; noun: string; nounPlural: string; saved: string }> = {
  tools: {
    title: 'Tools',
    description: 'What the project’s assistant can use in every task. Members without access to a tool get the assistant without it.',
    noun: 'tool',
    nounPlural: 'tools',
    saved: 'Tools saved',
  },
  skills: {
    title: 'Skills',
    description:
      'Playbooks the project’s assistant can follow in every task. A skill stays on the version it was added at until someone updates it.',
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
 * Pick the project assistant's tools, or its skills: one kind per dialog, opened from
 * its own row on the rail. Save writes the selection as a settings version.
 *
 * A tool or skill the caller can't use themselves stays listed if someone else added
 * it, and a save keeps it: the server only checks what a save adds.
 *
 * Skills show the version each one runs. Update moves a pin to the skill's current
 * content and is saved straight away, apart from Save, like any settings change.
 */
@Component({
  selector: 'app-project-bindings-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, ToolSelectorComponent],
  template: `
    <app-dialog-shell [title]="copy.title" [description]="copy.description" size="lg" (closed)="cancel()">
      <app-tool-selector
        [items]="items()"
        [selected]="selected()"
        (selectedChange)="selected.set($event)"
        (itemAction)="updatePin($event)"
        [label]="copy.title"
        [noun]="copy.noun"
        [nounPlural]="copy.nounPlural"
        [disabled]="!data.canEdit"
        [emptyText]="'None are available to you.'"
        maxHeight="lg"
      />
      @if (error()) {
        <p role="alert" class="mt-4 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
      }

      <div dialogFooter class="flex justify-end gap-3 border-t border-gray-200 px-6 py-4 dark:border-gray-700">
        <button
          type="button"
          (click)="cancel()"
          class="rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-white"
        >
          {{ data.canEdit ? 'Cancel' : 'Close' }}
        </button>
        @if (data.canEdit) {
          <button
            type="button"
            (click)="save()"
            [disabled]="!dirty() || saving()"
            class="rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-semibold text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {{ saving() ? 'Saving…' : 'Save' }}
          </button>
        }
      </div>
    </app-dialog-shell>
  `,
})
export class ProjectBindingsDialogComponent {
  private dialogRef = inject<DialogRef<ProjectBindingsDialogResult>>(DialogRef);
  private api = inject(ProjectApiService);
  private toast = inject(ToastService);

  protected readonly data = inject<ProjectBindingsDialogData>(DIALOG_DATA);
  protected readonly copy = KIND_COPY[this.data.kind];

  private readonly saved = signal(this.data.bound);
  protected readonly selected = signal<ReadonlySet<string>>(new Set(this.data.bound));
  private readonly pins = signal<Record<string, BoundBinding>>(this.data.pins ?? {});
  /** The skill whose pin is being updated. */
  private readonly pinning = signal<string | null>(null);
  protected readonly saving = signal(false);
  protected readonly error = signal<string | null>(null);
  /** The last write, reported on close so the rail can update in place. */
  private written: BindingsResponse | undefined;

  private readonly choices = choices(this.data.bound, this.data.palette);

  protected readonly items = computed<ToolSelectorItem[]>(() => {
    if (this.data.kind !== 'skills') return this.choices;
    const saved = this.saved();
    const pins = this.pins();
    const pinning = this.pinning();
    const busy = pinning !== null || this.saving();
    return this.choices.map(item => {
      const pin = saved.includes(item.id) ? pins[item.id] : undefined;
      if (!pin) return item;
      const line = pinLine(pin);
      const offer = this.data.canEdit && !item.locked && (pin.updateAvailable || !pin.version);
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

  protected readonly dirty = computed(() => !sameRefs(this.selected(), this.saved()));

  protected async save(): Promise<void> {
    if (!this.dirty() || this.saving()) return;
    this.saving.set(true);
    this.error.set(null);
    try {
      const selected = this.selected();
      // Keep the palette's order so an unchanged selection is an unchanged list.
      const bindings: BindingRef[] = this.choices.filter(c => selected.has(c.id)).map(c => ({ ref: c.id }));
      this.written = await firstValueFrom(this.api.saveBindings(this.data.projectId, this.data.kind, bindings));
      this.toast.success(this.copy.saved);
      this.dialogRef.close(this.written);
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
    if (this.data.kind !== 'skills' || this.pinning() !== null) return;
    this.pinning.set(item.id);
    this.error.set(null);
    try {
      const saved = await firstValueFrom(this.api.pinSkill(this.data.projectId, item.id));
      this.written = saved;
      this.pins.set(pinsOf(saved));
      this.toast.success(`${item.name} updated`);
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'That skill could not be updated.'));
    } finally {
      this.pinning.set(null);
    }
  }

  /** Cancelling after an Update still reports it, so the rail's history version moves. */
  protected cancel(): void {
    this.dialogRef.close(this.written);
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
