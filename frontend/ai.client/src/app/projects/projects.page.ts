import { ChangeDetectionStrategy, Component, OnInit, computed, inject, signal } from '@angular/core';
import { Router, RouterLink } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroArchiveBox, heroEye, heroEyeSlash, heroFolder, heroLockClosed, heroPencilSquare, heroPlus } from '@ng-icons/heroicons/outline';
import { CreateProjectDialogComponent, CreateProjectDialogResult } from './components/create-project-dialog.component';
import { Project, ProjectRole } from './models/project.model';
import { ProjectsService } from './services/projects.service';
import { personLabel } from '../shared/utils/person';
import { FEATURES } from '../services/features';
import { ToastService } from '../services/toast/toast.service';
import { ListArrangementService } from '../services/list-arrangement/list-arrangement.service';
import { TooltipDirective } from '../components/tooltip/tooltip.directive';

type ListFilter = 'all' | 'mine' | 'shared' | 'archived' | 'hidden';

const FILTERS: { value: ListFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'mine', label: 'Mine' },
  { value: 'shared', label: 'Shared with me' },
  { value: 'archived', label: 'Archived' },
  { value: 'hidden', label: 'Hidden' },
];

const ROLE_LABELS: Record<ProjectRole, string> = { owner: 'Owner', editor: 'Editor', viewer: 'Viewer' };

/**
 * `/projects` — the projects you own and the ones shared with you (shared-projects §6).
 *
 * Every card names your role, because it decides what you can do inside. The
 * filters run in the browser over one `GET /projects`; "All" hides archived
 * projects, which have their own filter.
 *
 * With `features.organizeLists`, a member can also **hide** a project from this
 * list (docs/specs/agent-project-organization.md D1): access, its conversations
 * in the sidebar and its notifications are untouched, and Hidden brings it back.
 * Hide is not Leave, which stays in the project's own menu.
 */
@Component({
  selector: 'app-projects',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, RouterLink, TooltipDirective],
  providers: [provideIcons({ heroArchiveBox, heroEye, heroEyeSlash, heroFolder, heroLockClosed, heroPencilSquare, heroPlus })],
  templateUrl: './projects.page.html',
})
export class ProjectsPage implements OnInit {
  private projectsService = inject(ProjectsService);
  private dialog = inject(Dialog);
  private router = inject(Router);
  private toast = inject(ToastService);
  private arrangements = inject(ListArrangementService);
  protected readonly features = inject(FEATURES);

  private readonly hiddenIds = this.arrangements.hiddenIds('projects');

  /** Hidden non-archived projects; archived ones stay under Archived. */
  protected readonly hiddenCount = computed(() => {
    const hidden = this.hiddenIds();
    return this.projects().filter(p => p.status !== 'archived' && hidden.has(p.projectId)).length;
  });

  /** Hidden is offered only with the flag, and only once something is hidden or it is chosen. */
  protected readonly filters = computed(() =>
    FILTERS.filter(
      f => f.value !== 'hidden' || (this.features.organizeLists && (this.hiddenCount() > 0 || this.filter() === 'hidden')),
    ),
  );

  /** The project being hidden or unhidden; disables its button meanwhile. */
  protected readonly hidingId = signal<string | null>(null);
  protected readonly roleLabels = ROLE_LABELS;
  protected readonly filter = signal<ListFilter>('all');

  protected readonly loading = this.projectsService.loading$;
  protected readonly error = this.projectsService.error$;
  protected readonly available = this.projectsService.available$;
  private readonly projects = this.projectsService.projects$;

  protected readonly visible = computed(() => {
    const f = this.filter();
    const hidden = this.features.organizeLists ? this.hiddenIds() : new Set<string>();
    return this.projects().filter(p => {
      if (f === 'archived') return p.status === 'archived';
      if (p.status === 'archived') return false;
      if (f === 'hidden') return hidden.has(p.projectId);
      if (hidden.has(p.projectId)) return false;
      if (f === 'mine') return p.role === 'owner';
      if (f === 'shared') return p.role !== 'owner';
      return true;
    });
  });

  protected readonly hasAny = computed(() => this.projects().length > 0);

  ngOnInit(): void {
    void this.projectsService.load();
    this.arrangements.open('projects');
  }

  protected setFilter(value: ListFilter): void {
    this.filter.set(value);
  }

  protected filterLabel(f: { value: ListFilter; label: string }): string {
    return f.value === 'hidden' ? `${f.label} (${this.hiddenCount()})` : f.label;
  }

  protected isHidden(project: Project): boolean {
    return this.hiddenIds().has(project.projectId);
  }

  /** Hide or unhide one project from this list; Undo puts a hidden one straight back. */
  protected async onToggleHidden(project: Project): Promise<void> {
    if (this.hidingId()) return;
    const hide = !this.isHidden(project);
    this.hidingId.set(project.projectId);
    try {
      await this.arrangements.setHidden('projects', project.projectId, hide, this.projectIds());
      if (hide) {
        this.toast.info(`Hid ${project.name}`, 'You are still a member. Find it under Hidden.', {
          action: { label: 'Undo', handler: () => void this.undoHide(project) },
        });
      }
    } catch {
      this.toast.error(hide ? 'Could not hide project' : 'Could not unhide project', 'Please try again.');
    } finally {
      this.hidingId.set(null);
    }
  }

  private async undoHide(project: Project): Promise<void> {
    try {
      await this.arrangements.setHidden('projects', project.projectId, false, this.projectIds());
    } catch {
      this.toast.error('Could not undo', 'Please try again.');
    }
  }

  private projectIds(): ReadonlySet<string> {
    return new Set(this.projects().map(p => p.projectId));
  }

  protected readonly personLabel = personLabel;

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
