import { ChangeDetectionStrategy, Component, computed, effect, inject, input, signal, untracked } from '@angular/core';
import { RouterLink } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArchiveBox, heroArrowLeft, heroEye, heroLockClosed, heroPencilSquare } from '@ng-icons/heroicons/outline';
import { Project, ProjectRole } from '../models/project.model';
import { ProjectApiService } from '../services/project-api.service';
import { ProjectsService, isUnavailable, projectErrorMessage } from '../services/projects.service';
import { ProjectOverviewComponent } from './project-overview.component';
import { ProjectMembersComponent } from './project-members.component';
import { ProjectSettingsComponent } from './project-settings.component';

export type ProjectTab = 'overview' | 'members' | 'settings';

const TABS: { value: ProjectTab; label: string }[] = [
  { value: 'overview', label: 'Overview' },
  { value: 'members', label: 'Members' },
  { value: 'settings', label: 'Settings' },
];

const ROLE_LABELS: Record<ProjectRole, string> = { owner: 'Owner', editor: 'Editor', viewer: 'Viewer' };

/**
 * `/projects/:id[/:tab]` — one project (shared-projects §6).
 *
 * The tab is part of the URL so a link can point at Members or Settings. Tabs
 * receive the loaded project and hand back a changed one (`projectChange`), so the
 * header and every tab always agree about the name, the role and whether the
 * project is archived.
 */
@Component({
  selector: 'app-project-detail',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink, ProjectOverviewComponent, ProjectMembersComponent, ProjectSettingsComponent],
  providers: [provideIcons({ heroArchiveBox, heroArrowLeft, heroEye, heroLockClosed, heroPencilSquare })],
  templateUrl: './project-detail.page.html',
})
export class ProjectDetailPage {
  private api = inject(ProjectApiService);
  private projects = inject(ProjectsService);

  /** Route params (`withComponentInputBinding`). */
  readonly id = input.required<string>();
  readonly tab = input<string | undefined>(undefined);

  protected readonly tabs = TABS;
  protected readonly roleLabels = ROLE_LABELS;
  protected readonly project = signal<Project | null>(null);
  protected readonly loading = signal(true);
  protected readonly error = signal<string | null>(null);

  protected readonly activeTab = computed<ProjectTab>(() => {
    const t = this.tab();
    return TABS.some(x => x.value === t) ? (t as ProjectTab) : 'overview';
  });

  constructor() {
    // An effect rather than ngOnInit: moving between projects reuses this page, and
    // only the input changes.
    effect(() => {
      const id = this.id();
      untracked(() => void this.load(id));
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
}
