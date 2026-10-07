import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked, viewChild } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { CdkMenu, CdkMenuItem, CdkMenuTrigger } from '@angular/cdk/menu';
import { ConnectedPosition } from '@angular/cdk/overlay';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArchiveBox,
  heroArrowRightOnRectangle,
  heroArrowUturnLeft,
  heroChevronDown,
  heroPencilSquare,
  heroTrash,
} from '@ng-icons/heroicons/outline';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog/confirmation-dialog.component';
import { ToastService } from '../../services/toast/toast.service';
import { Project } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { ProjectsService, isUnavailable, projectErrorMessage } from '../services/projects.service';
import {
  ProjectDetailsDialogComponent,
  ProjectDetailsDialogData,
  ProjectDetailsDialogResult,
} from '../components/project-details-dialog.component';
import { ProjectComposerComponent } from './project-composer.component';
import { ProjectPanel, ProjectRailComponent } from './project-rail.component';
import { ProjectProposalsComponent } from './project-proposals.component';
import { ProjectTasksComponent } from './project-tasks.component';

/**
 * What `/projects/:id/:tab` opens. The page has no tabs any more: a link that
 * names one lands on the page with that dialog open (the bell's Members link, an
 * Activity entry's Version link). `overview` and `tasks` are the page itself, and
 * the old `settings` tab is the Instructions dialog.
 */
const PANEL_FOR_TAB: Record<string, ProjectPanel | null> = {
  overview: null,
  tasks: null,
  settings: 'instructions',
  instructions: 'instructions',
  files: 'files',
  model: 'model',
  tools: 'tools',
  skills: 'tools',
  members: 'members',
  activity: 'activity',
  history: 'history',
};

const MENU_POSITIONS: ConnectedPosition[] = [
  { originX: 'start', originY: 'bottom', overlayX: 'start', overlayY: 'top', offsetY: 4 },
  { originX: 'start', originY: 'top', overlayX: 'start', overlayY: 'bottom', offsetY: -4 },
];

/**
 * `/projects/:id[/:tab]` — one project (shared-projects §6).
 *
 * A composer and the caller's recent tasks down the middle, and the project's
 * settings as a rail of one-line summaries beside them (`app-project-rail`), each
 * opening a dialog. The project's own actions (rename, archive, delete, leave)
 * live in the menu on its name in the breadcrumb.
 *
 * The URL's `tab` segment is kept for links that predate the rail: it opens the
 * matching dialog once and is then replaced with `overview`, so a reload shows the
 * page, not the dialog again. Dialogs hand back a changed project (`projectChange`)
 * so the title, the rail and the composer always agree about its name, the
 * caller's role and whether it is archived.
 */
@Component({
  selector: 'app-project-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink, CdkMenu, CdkMenuItem, CdkMenuTrigger, ProjectComposerComponent, ProjectProposalsComponent, ProjectRailComponent, ProjectTasksComponent],
  providers: [provideIcons({ heroArchiveBox, heroArrowRightOnRectangle, heroArrowUturnLeft, heroChevronDown, heroPencilSquare, heroTrash })],
  templateUrl: './project-detail.page.html',
})
export class ProjectDetailPage {
  private api = inject(ProjectApiService);
  private projects = inject(ProjectsService);
  private router = inject(Router);
  private dialog = inject(Dialog);
  private toast = inject(ToastService);

  /** Route params (`withComponentInputBinding`). */
  readonly id = input.required<string>();
  readonly tab = input<string | undefined>(undefined);

  protected readonly menuPositions = MENU_POSITIONS;
  protected readonly project = signal<Project | null>(null);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);

  private readonly rail = viewChild(ProjectRailComponent);
  /** The rail knows the model; the composer shows it beneath the input. */
  protected readonly modelLabel = computed(() => this.rail()?.modelLabel() ?? null);

  protected readonly isOwner = computed(() => this.project()?.role === 'owner');
  protected readonly archived = computed(() => this.project()?.status === 'archived');
  protected readonly canEdit = computed(() => {
    const p = this.project();
    return !!p && p.role !== 'viewer' && p.status === 'active';
  });

  constructor() {
    // An effect rather than ngOnInit: moving between projects reuses this page, and
    // only the input changes.
    effect(() => {
      const id = this.id();
      untracked(() => void this.load(id));
    });
    // A tab in the URL opens its dialog once the rail exists, then leaves the URL.
    effect(() => {
      const tab = this.tab();
      const project = this.project();
      const rail = this.rail();
      if (!tab || tab === 'overview' || !project || !rail) return;
      untracked(() => {
        const panel = PANEL_FOR_TAB[tab] ?? null;
        if (panel) void rail.open(panel);
        void this.router.navigate(['/projects', project.projectId, 'overview'], { replaceUrl: true });
      });
    });
  }

  private async load(id: string): Promise<void> {
    this.loading.set(true);
    this.error.set(null);
    try {
      this.project.set(await firstValueFrom(this.api.get(id)));
    } catch (err) {
      this.error.set(
        isUnavailable(err)
          ? 'This project doesn’t exist, or you’re not a member of it.'
          : projectErrorMessage(err, 'This project could not be loaded.'),
      );
    } finally {
      this.loading.set(false);
    }
  }

  protected onProjectChange(project: Project): void {
    this.project.set(project);
    this.projects.upsert(project);
  }

  // ---- the project menu -----------------------------------------------------

  protected async editDetails(): Promise<void> {
    const project = this.project();
    if (!project) return;
    const ref = this.dialog.open<ProjectDetailsDialogResult, ProjectDetailsDialogData>(ProjectDetailsDialogComponent, {
      data: { project },
    });
    const saved = await firstValueFrom(ref.closed);
    if (saved) this.onProjectChange(saved);
  }

  protected async setArchived(archived: boolean): Promise<void> {
    const project = this.project();
    if (!project) return;
    if (archived) {
      const ok = await this.confirm({
        title: 'Archive this project?',
        message: 'It becomes read-only for everyone and can’t start new tasks. You can restore it later.',
        confirmText: 'Archive',
      });
      if (!ok) return;
    }
    try {
      this.onProjectChange(
        await firstValueFrom(this.api.update(project.projectId, { status: archived ? 'archived' : 'active' })),
      );
      this.toast.success(archived ? 'Project archived' : 'Project restored');
    } catch (err) {
      this.toast.error('That change could not be saved', projectErrorMessage(err));
    }
  }

  protected async deleteProject(): Promise<void> {
    const project = this.project();
    if (!project) return;
    const ok = await this.confirm({
      title: `Delete ${project.name}?`,
      message: 'This permanently removes the project, its settings and its files for everyone. Members keep their own tasks. This can’t be undone.',
      confirmText: 'Delete project',
      destructive: true,
    });
    if (!ok) return;
    try {
      await firstValueFrom(this.api.delete(project.projectId));
      this.projects.remove(project.projectId);
      this.toast.success('Project deleted');
      await this.router.navigate(['/projects']);
    } catch (err) {
      this.toast.error('The project could not be deleted', projectErrorMessage(err));
    }
  }

  protected async leave(): Promise<void> {
    const project = this.project();
    if (!project) return;
    const ok = await this.confirm({
      title: 'Leave this project?',
      message: 'You lose access to it and to the tasks shared in it. Someone would have to add you again.',
      confirmText: 'Leave project',
      destructive: true,
    });
    if (!ok) return;
    try {
      await firstValueFrom(this.api.leave(project.projectId));
      this.projects.remove(project.projectId);
      await this.router.navigate(['/projects']);
    } catch (err) {
      this.toast.error('You could not leave the project', projectErrorMessage(err));
    }
  }

  private async confirm(data: ConfirmationDialogData): Promise<boolean> {
    const ref = this.dialog.open<boolean>(ConfirmationDialogComponent, { data });
    return (await firstValueFrom(ref.closed)) === true;
  }
}
