import { NgTemplateOutlet } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { CdkMenu, CdkMenuItemRadio, CdkMenuTrigger } from '@angular/cdk/menu';
import { ConnectedPosition } from '@angular/cdk/overlay';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroBookOpen,
  heroCalendarDays,
  heroCheck,
  heroChevronDown,
  heroClock,
  heroCpuChip,
  heroDocumentText,
  heroFolder,
  heroListBullet,
  heroSparkles,
  heroUsers,
  heroWrenchScrewdriver,
} from '@ng-icons/heroicons/outline';
import { BindableItem } from '../../agents/models/agent.model';
import { AgentService } from '../../agents/services/agent.service';
import { ToastService } from '../../services/toast/toast.service';
import { ModelConfig, Project, ProjectSchedule } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { isUnavailable, projectErrorMessage } from '../services/projects.service';
import { personLabel } from '../../shared/utils/person';

interface Row {
  key: string;
  /** The row's page. Model has none: it is a menu on the row. */
  link: string[] | null;
  icon: string;
  label: string;
  /** The grey detail beside the label, if any. */
  meta: string | null;
  /** The trailing verb: what opening the row lets you do. None on a row that only reads. */
  action: string | null;
}

/** The rail's schedules summary: the list, or `'off'` while scheduled runs are switched off here. */
type SchedulesSummary = ProjectSchedule[] | 'off';

/** How many files the rail counts before it says "100+". */
const FILES_PAGE = 100;

/** Below the Model row, or above it when the viewport has no room. */
const MODEL_MENU_POSITIONS: ConnectedPosition[] = [
  { originX: 'end', originY: 'bottom', overlayX: 'end', overlayY: 'top', offsetY: 4 },
  { originX: 'end', originY: 'top', overlayX: 'end', overlayY: 'bottom', offsetY: -4 },
];

/**
 * The project's settings as a slim rail beside the composer: one row per thing
 * the assistant works from (instructions, files, memory, model, tools, skills,
 * schedules) and per thing about the project itself (members, activity, history).
 * Each row links to that setting's page under `/projects/:id/`, apart from Model,
 * a single choice, which is a menu on its row and saves as soon as one is picked.
 *
 * The summaries are loaded each time the overview is opened, each on its own, so a
 * refused or failed read blanks one row rather than the rail.
 */
@Component({
  selector: 'app-project-rail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CdkMenu, CdkMenuItemRadio, CdkMenuTrigger, NgIcon, NgTemplateOutlet, RouterLink],
  providers: [
    provideIcons({
      heroBookOpen,
      heroCalendarDays,
      heroCheck,
      heroChevronDown,
      heroClock,
      heroCpuChip,
      heroDocumentText,
      heroFolder,
      heroListBullet,
      heroSparkles,
      heroUsers,
      heroWrenchScrewdriver,
    }),
  ],
  host: { class: 'block' },
  template: `
    <nav aria-label="Project settings">
      <ul class="space-y-0.5">
        @for (row of rows(); track row.key) {
          <li>
            @if (row.link) {
              <a [routerLink]="row.link" [class]="rowClass">
                <ng-container [ngTemplateOutlet]="rowBody" [ngTemplateOutletContext]="{ $implicit: row }" />
              </a>
            } @else if (canChooseModel()) {
              <button
                type="button"
                [cdkMenuTriggerFor]="modelMenu"
                [cdkMenuPosition]="modelMenuPositions"
                (cdkMenuOpened)="modelMenuOpen.set(true)"
                (cdkMenuClosed)="modelMenuOpen.set(false)"
                [class]="rowClass"
              >
                <ng-container [ngTemplateOutlet]="rowBody" [ngTemplateOutletContext]="{ $implicit: row }" />
              </button>
            } @else {
              <div class="flex w-full items-center gap-3 px-3 py-2.5">
                <ng-container [ngTemplateOutlet]="rowBody" [ngTemplateOutletContext]="{ $implicit: row }" />
              </div>
            }
          </li>
        }
      </ul>
    </nav>

    <p class="mt-5 px-3 text-xs/5 text-gray-600 dark:text-gray-400">
      {{ footer() }}
    </p>

    <ng-template #rowBody let-row>
      <ng-icon [name]="row.icon" class="size-5 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
      <span class="flex min-w-0 flex-1 items-baseline gap-2">
        <span class="shrink-0 text-sm/6 font-medium text-gray-900 dark:text-white">{{ row.label }}</span>
        @if (row.meta) {
          <span class="truncate text-xs/5 text-gray-600 dark:text-gray-400">{{ row.meta }}</span>
        } @else if (!loaded()) {
          <span class="h-3 w-16 animate-pulse rounded bg-gray-200 dark:bg-gray-700" aria-hidden="true"></span>
        }
      </span>
      @if (row.action) {
        <span class="inline-flex shrink-0 items-center gap-1 text-sm/6 text-gray-600 group-hover:text-gray-900 dark:text-gray-400 dark:group-hover:text-white">
          {{ row.action }}
          @if (!row.link) {
            <ng-icon name="heroChevronDown" class="size-4 transition-transform" [class.rotate-180]="modelMenuOpen()" aria-hidden="true" />
          }
        </span>
      }
    </ng-template>

    <ng-template #modelMenu>
      <div
        cdkMenu
        aria-label="Project model"
        aria-describedby="project-model-note"
        class="max-h-[min(28rem,70vh)] w-80 overflow-y-auto rounded-xl bg-white p-1 shadow-lg ring-1 ring-black/5 focus:outline-hidden dark:bg-gray-800 dark:ring-white/10 animate-in fade-in slide-in-from-top-1 duration-200"
      >
        <p id="project-model-note" class="px-3 pt-2 pb-2 text-xs/5 text-gray-600 dark:text-gray-400">
          Every task in this project runs on this model. Members who can’t use it get their own default instead.
        </p>
        @for (m of models(); track m.ref) {
          <button
            cdkMenuItemRadio
            type="button"
            [cdkMenuItemChecked]="modelId() === m.ref"
            [cdkMenuItemDisabled]="savingModel()"
            (cdkMenuItemTriggered)="chooseModel(m)"
            class="flex w-full items-start gap-3 rounded-lg px-3 py-2 text-left outline-hidden hover:bg-gray-50 focus:bg-gray-50 disabled:cursor-not-allowed disabled:opacity-60 dark:hover:bg-gray-700 dark:focus:bg-gray-700"
          >
            <span class="min-w-0 flex-1">
              <span class="block text-sm/6 font-medium text-gray-900 dark:text-white">{{ m.label }}</span>
              @if (m.description) {
                <span class="block text-xs/5 text-gray-600 dark:text-gray-400">{{ m.description }}</span>
              }
            </span>
            @if (modelId() === m.ref) {
              <ng-icon name="heroCheck" class="mt-1 size-4 shrink-0 text-primary-accessible dark:text-primary-50" aria-hidden="true" />
            }
          </button>
        }
        @if (modelId() && !modelKnown()) {
          <p class="border-t border-gray-200 px-3 pt-2 pb-1.5 text-xs/5 text-gray-600 dark:border-gray-700 dark:text-gray-400">
            The project is set to <span class="font-medium">{{ modelId() }}</span>, which isn’t a model you can use. Choosing another replaces it.
          </p>
        }
      </div>
    </ng-template>
  `,
})
export class ProjectRailComponent {
  private api = inject(ProjectApiService);
  private agents = inject(AgentService);
  private toast = inject(ToastService);

  readonly project = input.required<Project>();

  protected readonly rowClass =
    'group flex w-full items-center gap-3 rounded-2xl px-3 py-2.5 text-left transition-colors hover:bg-gray-100 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:hover:bg-white/5';
  protected readonly modelMenuPositions = MODEL_MENU_POSITIONS;

  protected readonly loaded = signal(false);
  private readonly instructions = signal('');
  private readonly version = signal<number | null>(null);
  protected readonly modelId = signal<string | null>(null);
  protected readonly models = signal<BindableItem[]>([]);
  private readonly toolCount = signal(0);
  private readonly skillCount = signal(0);
  /** Bound skills with a newer version than the one the project runs. */
  private readonly skillUpdates = signal(0);
  private readonly fileCount = signal<number | null>(null);
  private readonly moreFiles = signal(false);
  /** Files in the project's shared memory; null when unknown (memory off, or the read failed). */
  private readonly memoryCount = signal<number | null>(null);
  /** Null when unknown (the read failed, or hasn't finished). */
  private readonly schedules = signal<SchedulesSummary | null>(null);
  protected readonly savingModel = signal(false);
  protected readonly modelMenuOpen = signal(false);

  protected readonly archived = computed(() => this.project().status === 'archived');
  protected readonly canEdit = computed(() => this.project().role !== 'viewer' && !this.archived());
  private readonly isEditor = computed(() => this.project().role !== 'viewer');
  /** The Model row opens its menu for an editor with models to choose from. */
  protected readonly canChooseModel = computed(() => this.canEdit() && this.loaded() && this.models().length > 0);
  protected readonly modelKnown = computed(() => this.models().some(m => m.ref === this.modelId()));

  /** The model's display name; the id when the catalog lacks it; null for "each member's default". */
  readonly modelLabel = computed(() => {
    const id = this.modelId();
    if (!id) return null;
    return this.models().find(m => m.ref === id)?.label ?? id;
  });

  protected readonly rows = computed<Row[]>(() => {
    const edit = this.canEdit();
    const loaded = this.loaded();
    const page = (path: string) => ['/projects', this.project().projectId, path];
    const rows: Row[] = [
      {
        key: 'instructions',
        link: page('instructions'),
        icon: 'heroDocumentText',
        label: 'Instructions',
        meta: loaded ? (this.instructions().trim() ? null : 'Not set') : null,
        action: edit ? 'Edit' : 'View',
      },
      {
        key: 'files',
        link: page('files'),
        icon: 'heroFolder',
        label: 'Files',
        meta: this.filesMeta(),
        action: edit ? 'Add' : 'View',
      },
      {
        key: 'memory',
        link: page('memory'),
        icon: 'heroBookOpen',
        label: 'Memory',
        meta: this.memoryMeta(),
        action: 'Open',
      },
      {
        key: 'model',
        link: null,
        icon: 'heroCpuChip',
        label: 'Model',
        meta: loaded ? (this.savingModel() ? 'Saving…' : (this.modelLabel() ?? 'Each member’s default')) : null,
        action: this.canChooseModel() ? 'Change' : null,
      },
      {
        key: 'tools',
        link: page('tools'),
        icon: 'heroWrenchScrewdriver',
        label: 'Tools',
        meta: loaded ? countOrNone(this.toolCount(), 'tool') : null,
        action: edit ? 'Edit' : 'View',
      },
      {
        key: 'skills',
        link: page('skills'),
        icon: 'heroSparkles',
        label: 'Skills',
        meta: loaded ? this.skillsMeta() : null,
        action: edit ? 'Edit' : 'View',
      },
      {
        key: 'schedules',
        link: page('schedules'),
        icon: 'heroCalendarDays',
        label: 'Schedules',
        meta: this.schedulesMeta(),
        action: edit ? 'Manage' : 'View',
      },
      {
        key: 'members',
        link: page('members'),
        icon: 'heroUsers',
        label: 'Members',
        meta: count(this.project().memberCount + 1, 'person', 'people'),
        action: edit ? 'Manage' : 'View',
      },
    ];
    if (this.isEditor()) {
      rows.push({ key: 'activity', link: page('activity'), icon: 'heroListBullet', label: 'Activity', meta: null, action: 'View' });
    }
    rows.push({
      key: 'history',
      link: page('history'),
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
      untracked(() => void this.load(id));
    });
  }

  /** "2 skills", plus how many have a newer version than the one the project runs. */
  private skillsMeta(): string {
    const base = countOrNone(this.skillCount(), 'skill');
    const updates = this.skillUpdates();
    return updates ? `${base} · ${count(updates, 'update')}` : base;
  }

  /** "2 schedules · 1 paused", "None", or "Not turned on" where scheduled runs are off. */
  private schedulesMeta(): string | null {
    const schedules = this.schedules();
    if (schedules === null) return null;
    if (schedules === 'off') return 'Not turned on';
    const base = countOrNone(schedules.length, 'schedule');
    const paused = schedules.filter(s => s.state !== 'active').length;
    return paused ? `${base} · ${paused} paused` : base;
  }

  /** The project's schedules; `'off'` when the routes 404 (`SCHEDULED_RUNS_ENABLED` off). */
  private async loadSchedules(projectId: string): Promise<SchedulesSummary> {
    try {
      return (await firstValueFrom(this.api.schedules(projectId))).schedules;
    } catch (err) {
      if (isUnavailable(err)) return 'off';
      throw err;
    }
  }

  private memoryMeta(): string | null {
    const n = this.memoryCount();
    if (n === null) return null;
    return n === 0 ? 'None yet' : count(n, 'file');
  }

  /** How many files the project's shared memory holds (two reads: the space, then its manifest). */
  private async loadMemoryCount(projectId: string): Promise<number | null> {
    const memory = await firstValueFrom(this.api.memory(projectId));
    if (!memory.sharedSpaceId) return null;
    return (await firstValueFrom(this.api.memoryEntries(memory.sharedSpaceId))).entries.length;
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
    this.memoryCount.set(null);
    this.schedules.set(null);
    const quiet = <T>(p: Promise<T>): Promise<T | null> => p.catch(() => null);
    const [instructions, model, tools, skills, files, models, memoryCount, schedules] = await Promise.all([
      quiet(firstValueFrom(this.api.instructions(projectId))),
      quiet(firstValueFrom(this.api.model(projectId))),
      quiet(firstValueFrom(this.api.bindings(projectId, 'tools'))),
      quiet(firstValueFrom(this.api.bindings(projectId, 'skills'))),
      quiet(firstValueFrom(this.api.files(projectId, FILES_PAGE, null))),
      quiet(this.agents.loadBindable('model')),
      quiet(this.loadMemoryCount(projectId)),
      quiet(this.loadSchedules(projectId)),
    ]);
    if (projectId !== this.projectId()) return;
    this.instructions.set(instructions?.instructions ?? '');
    this.version.set(instructions?.version ?? null);
    this.modelId.set(model?.modelConfig?.modelId ?? null);
    this.toolCount.set(tools?.bindings.length ?? 0);
    this.skillCount.set(skills?.bindings.length ?? 0);
    this.skillUpdates.set(skills?.bindings.filter(b => b.updateAvailable).length ?? 0);
    if (files) {
      this.fileCount.set(files.documents.length);
      this.moreFiles.set(!!files.nextToken);
    }
    this.memoryCount.set(memoryCount);
    this.schedules.set(schedules);
    this.models.set(models ?? []);
    this.loaded.set(true);
  }

  /**
   * Run every task in the project on this model. Saved as soon as it is picked, as
   * one settings version; a member who can't use it gets their own default
   * (shared-projects §9.6).
   */
  protected async chooseModel(model: BindableItem): Promise<void> {
    if (model.ref === this.modelId() || this.savingModel()) return;
    const provider = model.meta?.['provider'];
    const config: ModelConfig = { modelId: model.ref, ...(typeof provider === 'string' ? { provider } : {}) };
    this.savingModel.set(true);
    try {
      const saved = await firstValueFrom(this.api.saveModel(this.projectId(), config));
      this.modelId.set(saved.modelConfig?.modelId ?? null);
      this.version.set(saved.version);
      this.toast.success(`The project now runs on ${model.label}`);
    } catch (err) {
      this.toast.error('The model could not be saved', projectErrorMessage(err));
    } finally {
      this.savingModel.set(false);
    }
  }
}

function count(n: number, singular: string, plural = `${singular}s`): string {
  return `${n} ${n === 1 ? singular : plural}`;
}

function countOrNone(n: number, singular: string): string {
  return n === 0 ? 'None' : count(n, singular);
}
