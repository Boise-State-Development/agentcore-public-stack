import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArchiveBox, heroEye, heroFolder, heroLockClosed, heroPencilSquare, heroPlus } from '@ng-icons/heroicons/outline';
import { CreateProjectDialogComponent, CreateProjectDialogResult } from './components/create-project-dialog.component';
import { Project, ProjectRole } from './models/project.model';
import { ProjectsService } from './services/projects.service';

type ListFilter = 'all' | 'mine' | 'shared' | 'archived';

const FILTERS: { value: ListFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'mine', label: 'Mine' },
  { value: 'shared', label: 'Shared with me' },
  { value: 'archived', label: 'Archived' },
];

const ROLE_LABELS: Record<ProjectRole, string> = { owner: 'Owner', editor: 'Editor', viewer: 'Viewer' };

/**
 * `/projects` — the projects you own and the ones shared with you (shared-projects §6).
 *
 * Every card names your role, because it decides what you can do inside. The
 * filters run in the browser over one `GET /projects`; "All" hides archived
 * projects, which have their own filter.
 */
@Component({
  selector: 'app-projects',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink],
  providers: [provideIcons({ heroArchiveBox, heroEye, heroFolder, heroLockClosed, heroPencilSquare, heroPlus })],
  templateUrl: './projects.page.html',
})
export class ProjectsPage implements OnInit {
  private projectsService = inject(ProjectsService);
  private dialog = inject(Dialog);
  private router = inject(Router);

  protected readonly filters = FILTERS;
  protected readonly roleLabels = ROLE_LABELS;
  protected readonly filter = signal<ListFilter>('all');

  protected readonly loading = this.projectsService.loading$;
  protected readonly error = this.projectsService.error$;
  protected readonly available = this.projectsService.available$;
  private readonly projects = this.projectsService.projects$;

  protected readonly visible = computed(() => {
    const f = this.filter();
    return this.projects().filter(p => {
      if (f === 'archived') return p.status === 'archived';
      if (p.status === 'archived') return false;
      if (f === 'mine') return p.role === 'owner';
      if (f === 'shared') return p.role !== 'owner';
      return true;
    });
  });

  protected readonly hasAny = computed(() => this.projects().length > 0);

  ngOnInit(): void {
    void this.projectsService.load();
  }

  protected setFilter(value: ListFilter): void {
    this.filter.set(value);
  }

  protected people(project: Project): string {
    const n = project.memberCount + 1;
    return n === 1 ? 'Just you' : `${n} people`;
  }

  async onCreate(): Promise<void> {
    const ref = this.dialog.open<CreateProjectDialogResult>(CreateProjectDialogComponent);
    const project = await firstValueFrom(ref.closed);
    if (project) {
      await this.router.navigate(['/projects', project.projectId]);
    }
  }
}
