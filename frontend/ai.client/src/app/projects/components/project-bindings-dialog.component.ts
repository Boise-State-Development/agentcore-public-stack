import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { BindableItem } from '../../agents/models/agent.model';
import { DialogShellComponent } from '../../components/dialog/dialog-shell.component';
import { ToolSelectorComponent } from '../../components/tool-selector/tool-selector.component';
import { ToolSelectorItem } from '../../components/tool-selector/tool-selector.model';
import { ToastService } from '../../services/toast/toast.service';
import { BindingRef, BindingsResponse } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { projectErrorMessage } from '../services/projects.service';

export type BindingKind = 'tools' | 'skills';

export interface ProjectBindingsDialogData {
  projectId: string;
  canEdit: boolean;
  /** What the project binds today, per kind. */
  bound: Record<BindingKind, string[]>;
  /** What the caller may bind, per kind (`AgentService.loadBindable`). */
  palette: Record<BindingKind, BindableItem[]>;
}

/** What was saved, per kind; a kind that wasn't saved is absent. `undefined` when nothing was. */
export type ProjectBindingsDialogResult = Partial<Record<BindingKind, BindingsResponse>> | undefined;

const KIND_COPY: Record<BindingKind, { heading: string; blurb: string; noun: string; nounPlural: string }> = {
  tools: {
    heading: 'Tools',
    blurb: 'What the assistant can use. Members without access to a tool get the assistant without it.',
    noun: 'tool',
    nounPlural: 'tools',
  },
  skills: {
    heading: 'Skills',
    blurb: 'Playbooks the assistant can follow. Skills run their latest version.',
    noun: 'skill',
    nounPlural: 'skills',
  },
};

/** Shown on a row someone else bound, which the caller can't use and can't remove. */
const FOREIGN_NOTE = 'Added by someone else. You don’t have access to it, but it stays.';

function sameRefs(a: ReadonlySet<string>, b: string[]): boolean {
  return a.size === b.length && b.every(ref => a.has(ref));
}

/**
 * Pick the project assistant's tools and skills. Each kind saves on its own (each
 * save is a version in the history), and Save writes whichever kinds changed.
 *
 * A tool or skill the caller can't use themselves stays listed if someone else
 * added it, and a save keeps it: the server only checks what a save adds.
 */
@Component({
  selector: 'app-project-bindings-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [DialogShellComponent, ToolSelectorComponent],
  template: `
    <app-dialog-shell
      title="Tools & skills"
      description="What the project’s assistant can use and follow in every task."
      size="lg"
      (closed)="cancel()"
    >
      <div class="space-y-8">
        @for (kind of kinds; track kind) {
          <section [attr.aria-labelledby]="'bindings-' + kind">
            <h3 [id]="'bindings-' + kind" class="text-sm/6 font-semibold text-gray-900 dark:text-white">{{ copy[kind].heading }}</h3>
            <p [id]="'bindings-' + kind + '-blurb'" class="mt-0.5 text-xs/5 text-gray-600 dark:text-gray-400">{{ copy[kind].blurb }}</p>
            <app-tool-selector
              class="mt-3"
              [items]="kind === 'tools' ? tools : skills"
              [selected]="kind === 'tools' ? selectedTools() : selectedSkills()"
              (selectedChange)="onSelectionChange(kind, $event)"
              [labelledBy]="'bindings-' + kind"
              [describedBy]="'bindings-' + kind + '-blurb'"
              [noun]="copy[kind].noun"
              [nounPlural]="copy[kind].nounPlural"
              [disabled]="!data.canEdit"
              [emptyText]="'None are available to you.'"
              maxHeight="sm"
            />
          </section>
        }
      </div>
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
  protected readonly kinds: BindingKind[] = ['tools', 'skills'];
  protected readonly copy = KIND_COPY;

  protected readonly tools = choices(this.data.bound.tools, this.data.palette.tools);
  protected readonly skills = choices(this.data.bound.skills, this.data.palette.skills);
  private readonly savedTools = signal(this.data.bound.tools);
  private readonly savedSkills = signal(this.data.bound.skills);
  protected readonly selectedTools = signal<ReadonlySet<string>>(new Set(this.data.bound.tools));
  protected readonly selectedSkills = signal<ReadonlySet<string>>(new Set(this.data.bound.skills));
  protected readonly saving = signal(false);
  protected readonly error = signal<string | null>(null);
  /** Kinds already written by a Save that then failed on the other kind. */
  private readonly partial: NonNullable<ProjectBindingsDialogResult> = {};

  protected readonly toolsDirty = computed(() => !sameRefs(this.selectedTools(), this.savedTools()));
  protected readonly skillsDirty = computed(() => !sameRefs(this.selectedSkills(), this.savedSkills()));
  protected readonly dirty = computed(() => this.toolsDirty() || this.skillsDirty());

  protected onSelectionChange(kind: BindingKind, selected: ReadonlySet<string>): void {
    (kind === 'tools' ? this.selectedTools : this.selectedSkills).set(selected);
  }

  protected async save(): Promise<void> {
    if (!this.dirty() || this.saving()) return;
    this.saving.set(true);
    this.error.set(null);
    const result = this.partial;
    try {
      for (const kind of this.kinds) {
        const kindDirty = kind === 'tools' ? this.toolsDirty() : this.skillsDirty();
        if (!kindDirty) continue;
        const selected = kind === 'tools' ? this.selectedTools() : this.selectedSkills();
        const choices = kind === 'tools' ? this.tools : this.skills;
        // Keep the palette's order so an unchanged selection is an unchanged list.
        const bindings: BindingRef[] = choices.filter(c => selected.has(c.id)).map(c => ({ ref: c.id }));
        result[kind] = await firstValueFrom(this.api.saveBindings(this.data.projectId, kind, bindings));
      }
      this.toast.success('Tools & skills saved');
      this.dialogRef.close(result);
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'That change could not be saved.'));
      // A kind that did save stays saved; the dialog stays open so the other can be retried.
      if (result.tools) this.savedTools.set(result.tools.bindings.map(b => b.ref));
      if (result.skills) this.savedSkills.set(result.skills.bindings.map(b => b.ref));
    } finally {
      this.saving.set(false);
    }
  }

  /** Cancelling after a half-saved Save still reports what was saved. */
  protected cancel(): void {
    this.dialogRef.close(Object.keys(this.partial).length ? this.partial : undefined);
  }
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
