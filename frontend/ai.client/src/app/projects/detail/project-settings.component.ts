import { ChangeDetectionStrategy, Component, computed, effect, inject, input, output, signal, untracked, viewChild } from '@angular/core';
import { FormControl, ReactiveFormsModule, Validators } from '@angular/forms';
import { Router } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroChevronDown } from '@ng-icons/heroicons/outline';
import { AgentService } from '../../agents/services/agent.service';
import { BindableItem } from '../../agents/models/agent.model';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog/confirmation-dialog.component';
import { ToastService } from '../../services/toast/toast.service';
import { ProjectHistoryComponent } from '../components/project-history.component';
import { BindingRef, ModelConfig, Project } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { ProjectsService, projectErrorMessage } from '../services/projects.service';

const INSTRUCTIONS_MAX = 100_000;

interface Choice {
  ref: string;
  label: string;
  description: string;
  /** Bound by someone else, and not one the caller can use. Kept on save. */
  foreign: boolean;
}

/**
 * A project's Settings (shared-projects PR-1.5a): what its assistant is told, which
 * model it runs, and which tools and skills it may use.
 *
 * Each section saves on its own, because each save is a version in the history. A
 * tool or skill the caller can't use themselves stays listed if someone else added
 * it, and a save keeps it; the server only checks what a save adds. Members who
 * can't use a model or tool still get a working assistant without it (§9.6).
 */
@Component({
  selector: 'app-project-settings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, ReactiveFormsModule, ProjectHistoryComponent],
  providers: [provideIcons({ heroChevronDown })],
  templateUrl: './project-settings.component.html',
})
export class ProjectSettingsComponent {
  private api = inject(ProjectApiService);
  private projects = inject(ProjectsService);
  private agents = inject(AgentService);
  private toast = inject(ToastService);
  private dialog = inject(Dialog);
  private router = inject(Router);

  readonly project = input.required<Project>();
  readonly projectChange = output<Project>();

  private readonly history = viewChild(ProjectHistoryComponent);

  protected readonly instructionsMax = INSTRUCTIONS_MAX;
  protected readonly name = new FormControl('', { nonNullable: true, validators: [Validators.required, Validators.maxLength(200), Validators.pattern(/\S/)] });
  protected readonly description = new FormControl('', { nonNullable: true, validators: [Validators.maxLength(2000)] });
  protected readonly instructions = new FormControl('', { nonNullable: true, validators: [Validators.maxLength(INSTRUCTIONS_MAX)] });

  protected readonly savedInstructions = signal('');
  protected readonly instructionsValue = signal('');
  protected readonly version = signal<number | null>(null);
  protected readonly modelId = signal<string | null>(null);
  protected readonly savedModelId = signal<string | null>(null);
  protected readonly models = signal<BindableItem[]>([]);
  protected readonly tools = signal<Choice[]>([]);
  protected readonly skills = signal<Choice[]>([]);
  protected readonly selectedTools = signal<Set<string>>(new Set());
  protected readonly selectedSkills = signal<Set<string>>(new Set());
  private readonly savedTools = signal<string[]>([]);
  private readonly savedSkills = signal<string[]>([]);

  protected readonly loaded = signal(false);
  protected readonly saving = signal<string | null>(null);
  protected readonly error = signal<string | null>(null);
  protected readonly historyOpen = signal(false);

  protected readonly archived = computed(() => this.project().status === 'archived');
  protected readonly isOwner = computed(() => this.project().role === 'owner');
  protected readonly canEdit = computed(() => this.project().role !== 'viewer' && !this.archived());
  protected readonly modelKnown = computed(() => this.models().some(m => m.ref === this.modelId()));
  protected readonly instructionsDirty = computed(() => this.instructionsValue() !== this.savedInstructions());
  protected readonly toolsDirty = computed(() => !sameRefs([...this.selectedTools()], this.savedTools()));
  protected readonly skillsDirty = computed(() => !sameRefs([...this.selectedSkills()], this.savedSkills()));

  private readonly projectId = computed(() => this.project().projectId);

  constructor() {
    this.instructions.valueChanges.subscribe(v => this.instructionsValue.set(v));
    effect(() => {
      const id = this.projectId();
      untracked(() => void this.load(id));
    });
    effect(() => {
      const p = this.project();
      untracked(() => {
        this.name.setValue(p.name);
        this.description.setValue(p.description);
        const editable = p.role !== 'viewer' && p.status === 'active';
        for (const control of [this.name, this.description, this.instructions]) {
          if (editable) control.enable({ emitEvent: false });
          else control.disable({ emitEvent: false });
        }
      });
    });
  }

  private async load(projectId: string): Promise<void> {
    this.loaded.set(false);
    try {
      const [instructions, model, tools, skills, modelPalette, toolPalette, skillPalette] = await Promise.all([
        firstValueFrom(this.api.instructions(projectId)),
        firstValueFrom(this.api.model(projectId)),
        firstValueFrom(this.api.bindings(projectId, 'tools')),
        firstValueFrom(this.api.bindings(projectId, 'skills')),
        this.agents.loadBindable('model'),
        this.agents.loadBindable('tool'),
        this.agents.loadBindable('skill'),
      ]);
      this.savedInstructions.set(instructions.instructions);
      this.instructions.setValue(instructions.instructions);
      this.version.set(instructions.version);
      this.models.set(modelPalette);
      this.modelId.set(model.modelConfig?.modelId ?? null);
      this.savedModelId.set(model.modelConfig?.modelId ?? null);
      this.setBindings('tools', tools.bindings.map(b => b.ref), toolPalette);
      this.setBindings('skills', skills.bindings.map(b => b.ref), skillPalette);
      this.error.set(null);
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'Settings could not be loaded.'));
    } finally {
      this.loaded.set(true);
    }
  }

  private setBindings(kind: 'tools' | 'skills', bound: string[], palette: BindableItem[]): void {
    const known = new Set(palette.map(p => p.ref));
    const choices: Choice[] = [
      ...palette.map(p => ({ ref: p.ref, label: p.label, description: p.description, foreign: false })),
      ...bound.filter(ref => !known.has(ref)).map(ref => ({ ref, label: ref, description: '', foreign: true })),
    ];
    if (kind === 'tools') {
      this.tools.set(choices);
      this.selectedTools.set(new Set(bound));
      this.savedTools.set(bound);
    } else {
      this.skills.set(choices);
      this.selectedSkills.set(new Set(bound));
      this.savedSkills.set(bound);
    }
  }

  // ---- details ------------------------------------------------------------

  protected async saveDetails(): Promise<void> {
    if (this.name.invalid || this.description.invalid) return;
    await this.run('details', 'Details saved', async () => {
      const project = await firstValueFrom(
        this.api.update(this.project().projectId, {
          name: this.name.value.trim(),
          description: this.description.value.trim(),
        }),
      );
      this.projectChange.emit(project);
    });
  }

  protected detailsDirty(): boolean {
    const p = this.project();
    return this.name.value.trim() !== p.name || this.description.value.trim() !== p.description;
  }

  // ---- instructions, model, tools, skills ---------------------------------

  protected async saveInstructions(): Promise<void> {
    if (this.instructions.invalid) return;
    await this.run('instructions', 'Instructions saved', async () => {
      const response = await firstValueFrom(this.api.saveInstructions(this.project().projectId, this.instructions.value));
      this.savedInstructions.set(response.instructions);
      this.instructions.setValue(response.instructions);
      this.version.set(response.version);
    });
  }

  protected async saveModel(modelId: string): Promise<void> {
    this.modelId.set(modelId);
    const item = this.models().find(m => m.ref === modelId);
    const provider = item?.meta?.['provider'] as string | undefined;
    const config: ModelConfig = { modelId, ...(provider ? { provider } : {}) };
    await this.run('model', 'Model saved', async () => {
      const response = await firstValueFrom(this.api.saveModel(this.project().projectId, config));
      this.savedModelId.set(response.modelConfig?.modelId ?? null);
      this.version.set(response.version);
    }, () => this.modelId.set(this.savedModelId()));
  }

  protected toggle(kind: 'tools' | 'skills', ref: string): void {
    const target = kind === 'tools' ? this.selectedTools : this.selectedSkills;
    target.update(current => {
      const next = new Set(current);
      if (next.has(ref)) next.delete(ref);
      else next.add(ref);
      return next;
    });
  }

  protected async saveBindings(kind: 'tools' | 'skills'): Promise<void> {
    const selected = kind === 'tools' ? this.selectedTools : this.selectedSkills;
    const saved = kind === 'tools' ? this.savedTools : this.savedSkills;
    const choices = kind === 'tools' ? this.tools() : this.skills();
    // Keep the palette's order so an unchanged selection is an unchanged list.
    const refs = choices.map(c => c.ref).filter(ref => selected().has(ref));
    const bindings: BindingRef[] = refs.map(ref => ({ ref }));
    await this.run(kind, kind === 'tools' ? 'Tools saved' : 'Skills saved', async () => {
      const response = await firstValueFrom(this.api.saveBindings(this.project().projectId, kind, bindings));
      const bound = response.bindings.map(b => b.ref);
      saved.set(bound);
      selected.set(new Set(bound));
      this.version.set(response.version);
    });
  }

  protected resetBindings(kind: 'tools' | 'skills'): void {
    if (kind === 'tools') this.selectedTools.set(new Set(this.savedTools()));
    else this.selectedSkills.set(new Set(this.savedSkills()));
  }

  protected toggleHistory(): void {
    this.historyOpen.update(open => !open);
  }

  // ---- owner --------------------------------------------------------------

  protected async setEditorsManageMembers(value: boolean): Promise<void> {
    await this.run('access', 'Saved', async () => {
      this.projectChange.emit(await firstValueFrom(this.api.update(this.project().projectId, { editorsManageMembers: value })));
    });
  }

  protected async setArchived(archived: boolean): Promise<void> {
    if (archived) {
      const ok = await this.confirm({
        title: 'Archive this project?',
        message: 'It becomes read-only for everyone and can’t start new tasks. You can restore it later.',
        confirmText: 'Archive',
      });
      if (!ok) return;
    }
    await this.run('status', archived ? 'Project archived' : 'Project restored', async () => {
      this.projectChange.emit(
        await firstValueFrom(this.api.update(this.project().projectId, { status: archived ? 'archived' : 'active' })),
      );
    });
  }

  protected async deleteProject(): Promise<void> {
    const ok = await this.confirm({
      title: `Delete ${this.project().name}?`,
      message: 'This permanently removes the project, its settings and its files for everyone. Members keep their own tasks. This can’t be undone.',
      confirmText: 'Delete project',
      destructive: true,
    });
    if (!ok) return;
    await this.run('delete', 'Project deleted', async () => {
      await firstValueFrom(this.api.delete(this.project().projectId));
      this.projects.remove(this.project().projectId);
      await this.router.navigate(['/projects']);
    });
  }

  // ---- helpers --------------------------------------------------------------

  private async run(key: string, success: string, action: () => Promise<void>, onError?: () => void): Promise<void> {
    this.saving.set(key);
    this.error.set(null);
    try {
      await action();
      this.toast.success(success);
      if (this.historyOpen()) void this.history()?.load();
    } catch (err) {
      onError?.();
      this.error.set(projectErrorMessage(err, 'That change could not be saved.'));
    } finally {
      this.saving.set(null);
    }
  }

  private async confirm(data: ConfirmationDialogData): Promise<boolean> {
    const ref = this.dialog.open<boolean>(ConfirmationDialogComponent, { data });
    return (await firstValueFrom(ref.closed)) === true;
  }
}

function sameRefs(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every(ref => b.includes(ref));
}
