import { ChangeDetectionStrategy, Component, computed, effect, inject, input, output, signal, untracked } from '@angular/core';
import { Dialog, DialogRef } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroClock,
  heroCpuChip,
  heroDocumentText,
  heroFolder,
  heroListBullet,
  heroUsers,
  heroWrenchScrewdriver,
} from '@ng-icons/heroicons/outline';
import { BindableItem } from '../../agents/models/agent.model';
import { AgentService } from '../../agents/services/agent.service';
import { Project } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { personLabel } from '../../shared/utils/person';
import {
  ProjectActivityDialogComponent,
  ProjectActivityDialogData,
  ProjectActivityDialogResult,
} from '../components/project-activity-dialog.component';
import {
  ProjectBindingsDialogComponent,
  ProjectBindingsDialogData,
  ProjectBindingsDialogResult,
} from '../components/project-bindings-dialog.component';
import {
  ProjectFilesDialogComponent,
  ProjectFilesDialogData,
  ProjectFilesDialogResult,
} from '../components/project-files-dialog.component';
import {
  ProjectHistoryDialogComponent,
  ProjectHistoryDialogData,
  ProjectHistoryDialogResult,
} from '../components/project-history-dialog.component';
import {
  ProjectInstructionsDialogComponent,
  ProjectInstructionsDialogData,
  ProjectInstructionsDialogResult,
} from '../components/project-instructions-dialog.component';
import {
  ProjectMembersDialogComponent,
  ProjectMembersDialogData,
  ProjectMembersDialogResult,
} from '../components/project-members-dialog.component';
import {
  ProjectModelDialogComponent,
  ProjectModelDialogData,
  ProjectModelDialogResult,
} from '../components/project-model-dialog.component';

/** A dialog the rail can open. The page maps legacy tab URLs onto these. */
export type ProjectPanel = 'instructions' | 'files' | 'model' | 'tools' | 'members' | 'activity' | 'history';

interface Row {
  panel: ProjectPanel;
  icon: string;
  label: string;
  /** The grey detail beside the label, if any. */
  meta: string | null;
  /** The trailing verb: what opening the row lets you do. */
  action: string;
}

/** How many files the rail counts before it says "100+". */
const FILES_PAGE = 100;

/**
 * The project's settings as a slim rail beside the composer: one row per thing
 * the assistant works from (instructions, files, model, tools and skills) and per
 * thing about the project itself (members, activity, history). Each row opens a
 * dialog; the rail only ever shows a one-line summary.
 *
 * The summaries are loaded once per project, each on its own, so a refused or
 * failed read blanks one row rather than the rail. Dialogs hand back what they
 * saved and the rail updates in place, so nothing is re-fetched after a save.
 */
@Component({
  selector: 'app-project-rail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({ heroClock, heroCpuChip, heroDocumentText, heroFolder, heroListBullet, heroUsers, heroWrenchScrewdriver }),
  ],
  host: { class: 'block' },
  template: `
    <nav aria-label="Project settings">
      <ul class="space-y-0.5">
        @for (row of rows(); track row.panel) {
          <li>
            <button
              type="button"
              (click)="open(row.panel)"
              class="group flex w-full items-center gap-3 rounded-2xl px-3 py-2.5 text-left transition-colors hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-white/5"
            >
              <ng-icon [name]="row.icon" class="size-5 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
              <span class="flex min-w-0 flex-1 items-baseline gap-2">
                <span class="shrink-0 text-sm/6 font-medium text-gray-900 dark:text-white">{{ row.label }}</span>
                @if (row.meta) {
                  <span class="truncate text-xs/5 text-gray-600 dark:text-gray-400">{{ row.meta }}</span>
                } @else if (!loaded()) {
                  <span class="h-3 w-16 animate-pulse rounded bg-gray-200 dark:bg-gray-700" aria-hidden="true"></span>
                }
              </span>
              <span class="shrink-0 text-sm/6 text-gray-600 group-hover:text-gray-900 dark:text-gray-400 dark:group-hover:text-white">{{ row.action }}</span>
            </button>
          </li>
        }
      </ul>
    </nav>

    <p class="mt-5 px-3 text-xs/5 text-gray-600 dark:text-gray-400">
      {{ footer() }}
    </p>
  `,
})
export class ProjectRailComponent {
  private api = inject(ProjectApiService);
  private agents = inject(AgentService);
  private dialog = inject(Dialog);

  readonly project = input.required<Project>();
  /** The project after a change made in a dialog (a transfer, a new member count). */
  readonly projectChange = output<Project>();

  protected readonly loaded = signal(false);
  private readonly instructions = signal('');
  private readonly version = signal<number | null>(null);
  private readonly modelId = signal<string | null>(null);
  private readonly models = signal<BindableItem[]>([]);
  private readonly toolPalette = signal<BindableItem[]>([]);
  private readonly skillPalette = signal<BindableItem[]>([]);
  private readonly tools = signal<string[]>([]);
  private readonly skills = signal<string[]>([]);
  private readonly fileCount = signal<number | null>(null);
  private readonly moreFiles = signal(false);

  private open$: DialogRef<unknown> | null = null;
  /** The summary read in flight, which a dialog opened early must wait for. */
  private loading: Promise<void> = Promise.resolve();

  protected readonly archived = computed(() => this.project().status === 'archived');
  protected readonly canEdit = computed(() => this.project().role !== 'viewer' && !this.archived());
  private readonly isEditor = computed(() => this.project().role !== 'viewer');

  /** The model's display name; the id when the catalog lacks it; null for "each member's default". */
  readonly modelLabel = computed(() => {
    const id = this.modelId();
    if (!id) return null;
    return this.models().find(m => m.ref === id)?.label ?? id;
  });

  protected readonly rows = computed<Row[]>(() => {
    const edit = this.canEdit();
    const loaded = this.loaded();
    const rows: Row[] = [
      {
        panel: 'instructions',
        icon: 'heroDocumentText',
        label: 'Instructions',
        meta: loaded ? (this.instructions().trim() ? null : 'Not set') : null,
        action: edit ? 'Edit' : 'View',
      },
      {
        panel: 'files',
        icon: 'heroFolder',
        label: 'Files',
        meta: this.filesMeta(),
        action: edit ? 'Add' : 'View',
      },
      {
        panel: 'model',
        icon: 'heroCpuChip',
        label: 'Model',
        meta: loaded ? (this.modelLabel() ?? 'Each member’s default') : null,
        action: edit ? 'Change' : 'View',
      },
      {
        panel: 'tools',
        icon: 'heroWrenchScrewdriver',
        label: 'Tools & skills',
        meta: loaded ? `${count(this.tools().length, 'tool')} · ${count(this.skills().length, 'skill')}` : null,
        action: edit ? 'Edit' : 'View',
      },
      {
        panel: 'members',
        icon: 'heroUsers',
        label: 'Members',
        meta: count(this.project().memberCount + 1, 'person', 'people'),
        action: edit ? 'Manage' : 'View',
      },
    ];
    if (this.isEditor()) {
      rows.push({ panel: 'activity', icon: 'heroListBullet', label: 'Activity', meta: null, action: 'View' });
    }
    rows.push({
      panel: 'history',
      icon: 'heroClock',
      label: 'History',
      meta: loaded ? (this.version() ? `Version ${this.version()}` : 'No changes yet') : null,
      action: 'View',
    });
    return rows;
  });

  protected readonly footer = computed(() => {
    const p = this.project();
    const owner = p.role === 'owner' ? 'you' : personLabel(p.ownerName, p.ownerEmail);
    const people = p.memberCount + 1;
    const scope = people === 1 ? 'Only you can see this project.' : `Shared with ${count(p.memberCount, 'other person', 'other people')}.`;
    const role = p.role === 'editor' ? ' You’re an editor.' : p.role === 'viewer' ? ' You can view it but not change it.' : '';
    return `${scope} Owned by ${owner}.${role}`;
  });

  private readonly projectId = computed(() => this.project().projectId);

  constructor() {
    effect(() => {
      const id = this.projectId();
      untracked(() => {
        this.loading = this.load(id);
      });
    });
  }

  private filesMeta(): string | null {
    const n = this.fileCount();
    if (n === null) return null;
    if (n === 0) return 'None yet';
    return `${count(n, 'file')}${this.moreFiles() ? '+' : ''}`;
  }

  private async load(projectId: string): Promise<void> {
    this.loaded.set(false);
    this.fileCount.set(null);
    const quiet = <T>(p: Promise<T>): Promise<T | null> => p.catch(() => null);
    const [instructions, model, tools, skills, files, models, toolPalette, skillPalette] = await Promise.all([
      quiet(firstValueFrom(this.api.instructions(projectId))),
      quiet(firstValueFrom(this.api.model(projectId))),
      quiet(firstValueFrom(this.api.bindings(projectId, 'tools'))),
      quiet(firstValueFrom(this.api.bindings(projectId, 'skills'))),
      quiet(firstValueFrom(this.api.files(projectId, FILES_PAGE, null))),
      quiet(this.agents.loadBindable('model')),
      quiet(this.agents.loadBindable('tool')),
      quiet(this.agents.loadBindable('skill')),
    ]);
    if (projectId !== this.projectId()) return;
    this.instructions.set(instructions?.instructions ?? '');
    this.version.set(instructions?.version ?? null);
    this.modelId.set(model?.modelConfig?.modelId ?? null);
    this.tools.set(tools?.bindings.map(b => b.ref) ?? []);
    this.skills.set(skills?.bindings.map(b => b.ref) ?? []);
    if (files) {
      this.fileCount.set(files.documents.length);
      this.moreFiles.set(!!files.nextToken);
    }
    this.models.set(models ?? []);
    this.toolPalette.set(toolPalette ?? []);
    this.skillPalette.set(skillPalette ?? []);
    this.loaded.set(true);
  }

  /**
   * Open one of the rail's dialogs. Only one is open at a time: opening another
   * closes the first, which is what a Version link inside Activity relies on.
   */
  async open(panel: ProjectPanel): Promise<void> {
    if (panel === 'activity' && !this.isEditor()) return;
    // A link can open a dialog before the summary it starts from has arrived.
    await this.loading;
    this.open$?.close();
    const project = this.project();
    const common = { projectId: project.projectId, canEdit: this.canEdit() };
    switch (panel) {
      case 'instructions': {
        const ref = this.dialog.open<ProjectInstructionsDialogResult, ProjectInstructionsDialogData>(ProjectInstructionsDialogComponent, {
          data: { ...common, projectName: project.name, instructions: this.instructions(), version: this.version() },
        });
        const saved = await this.track(ref);
        if (saved) {
          this.instructions.set(saved.instructions);
          this.version.set(saved.version);
        }
        return;
      }
      case 'model': {
        const ref = this.dialog.open<ProjectModelDialogResult, ProjectModelDialogData>(ProjectModelDialogComponent, {
          data: { ...common, modelId: this.modelId(), models: this.models() },
        });
        const saved = await this.track(ref);
        if (saved) {
          this.modelId.set(saved.modelConfig?.modelId ?? null);
          this.version.set(saved.version);
        }
        return;
      }
      case 'tools': {
        const ref = this.dialog.open<ProjectBindingsDialogResult, ProjectBindingsDialogData>(ProjectBindingsDialogComponent, {
          data: {
            ...common,
            bound: { tools: this.tools(), skills: this.skills() },
            palette: { tools: this.toolPalette(), skills: this.skillPalette() },
          },
        });
        const saved = await this.track(ref);
        if (saved?.tools) this.tools.set(saved.tools.bindings.map(b => b.ref));
        if (saved?.skills) this.skills.set(saved.skills.bindings.map(b => b.ref));
        const version = saved?.skills?.version ?? saved?.tools?.version;
        if (version !== undefined) this.version.set(version);
        return;
      }
      case 'files': {
        const ref = this.dialog.open<ProjectFilesDialogResult, ProjectFilesDialogData>(ProjectFilesDialogComponent, {
          data: {
            project: this.project,
            onCountChange: n => {
              this.fileCount.set(n);
              this.moreFiles.set(false);
            },
          },
        });
        await this.track(ref);
        return;
      }
      case 'members': {
        const ref = this.dialog.open<ProjectMembersDialogResult, ProjectMembersDialogData>(ProjectMembersDialogComponent, {
          data: { project: this.project, onProjectChange: p => this.projectChange.emit(p) },
        });
        await this.track(ref);
        return;
      }
      case 'activity': {
        const ref = this.dialog.open<ProjectActivityDialogResult, ProjectActivityDialogData>(ProjectActivityDialogComponent, {
          data: { project: this.project },
        });
        await this.track(ref);
        return;
      }
      case 'history': {
        const ref = this.dialog.open<ProjectHistoryDialogResult, ProjectHistoryDialogData>(ProjectHistoryDialogComponent, {
          data: { projectId: project.projectId },
        });
        await this.track(ref);
        return;
      }
    }
  }

  private async track<R>(ref: DialogRef<R>): Promise<R | undefined> {
    this.open$ = ref as DialogRef<unknown>;
    try {
      return await firstValueFrom(ref.closed);
    } finally {
      if (this.open$ === ref) this.open$ = null;
    }
  }
}

function count(n: number, singular: string, plural = `${singular}s`): string {
  return `${n} ${n === 1 ? singular : plural}`;
}
