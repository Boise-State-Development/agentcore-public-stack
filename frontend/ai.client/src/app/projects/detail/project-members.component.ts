import { ChangeDetectionStrategy, Component, computed, effect, inject, input, output, signal, untracked, viewChild } from '@angular/core';
import { Router } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroChevronDown, heroTrash } from '@ng-icons/heroicons/outline';
import { UserService } from '../../auth/user.service';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../../components/confirmation-dialog/confirmation-dialog.component';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';
import { ToastService } from '../../services/toast/toast.service';
import { MemberRole, Project, ProjectMember, ProjectRole } from '../models/project.model';
import { PeoplePickerComponent, PeoplePickerSubmit } from '../components/people-picker.component';
import { ProjectApiService } from '../services/project-api.service';
import { ProjectsService, projectErrorMessage } from '../services/projects.service';

const ROLE_LABELS: Record<ProjectRole, string> = { owner: 'Owner', editor: 'Editor', viewer: 'Viewer' };

/**
 * A project's Members tab (shared-projects §5, PR-1.2 and 1.3).
 *
 * The server decides who may manage people (`canManage`), so this page never
 * re-derives the editors-manage-members rule. Nobody can change or remove the owner;
 * ownership moves only to an editor who has signed in, and the old owner becomes an
 * editor.
 */
@Component({
  selector: 'app-project-members',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, PeoplePickerComponent, TooltipDirective],
  providers: [provideIcons({ heroChevronDown, heroTrash })],
  template: `
    <div class="max-w-3xl space-y-8">
      @if (canManage()) {
        <section aria-labelledby="add-people-heading">
          <h2 id="add-people-heading" class="sr-only">Add people</h2>
          <app-people-picker [projectId]="project().projectId" [busy]="adding()" (submitted)="add($event)" />
        </section>
      }

      <section aria-labelledby="members-heading">
        <div class="flex items-baseline justify-between gap-3">
          <h2 id="members-heading" class="text-base/7 font-semibold text-gray-900 dark:text-white">
            Members
            @if (members().length) {
              <span class="font-normal text-gray-600 dark:text-gray-400">· {{ members().length }}</span>
            }
          </h2>
          @if (canLeave()) {
            <button
              type="button"
              (click)="leave()"
              class="rounded-2xl px-3 py-1.5 text-sm/6 font-medium text-state-danger-600 hover:bg-state-danger-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-600 dark:text-state-danger-400 dark:hover:bg-state-danger-900/20"
            >
              Leave project
            </button>
          }
        </div>

        @if (error()) {
          <p role="alert" class="mt-3 text-sm/6 text-state-danger-600 dark:text-state-danger-400">{{ error() }}</p>
        }

        @if (loading() && members().length === 0) {
          <div class="mt-3 h-32 animate-pulse rounded-2xl bg-gray-100 dark:bg-gray-800" aria-busy="true"></div>
        } @else {
          <ul class="mt-3 divide-y divide-gray-200 overflow-hidden rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800">
            @for (member of members(); track member.email) {
              <li class="flex flex-wrap items-center gap-3 px-4 py-3">
                <div class="min-w-0 flex-1">
                  <p class="truncate text-sm/6 font-medium text-gray-900 dark:text-white">
                    {{ member.email }}
                    @if (member.email === me()) {
                      <span class="font-normal text-gray-600 dark:text-gray-400">(you)</span>
                    }
                  </p>
                  @if (!member.hasSignedIn) {
                    <p class="text-xs/5 text-gray-600 dark:text-gray-400">Hasn’t opened the project yet</p>
                  }
                </div>

                @if (canChange(member)) {
                  <div class="relative inline-flex">
                    <label [for]="'role-' + member.email" class="sr-only">Role for {{ member.email }}</label>
                    <select
                      [id]="'role-' + member.email"
                      (change)="changeRole(member, $any($event.target).value)"
                      class="appearance-none rounded-2xl border border-gray-300 bg-white py-1 pr-8 pl-3 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
                    >
                      <option value="editor" [selected]="member.role === 'editor'">Editor</option>
                      <option value="viewer" [selected]="member.role === 'viewer'">Viewer</option>
                    </select>
                    <ng-icon name="heroChevronDown" class="pointer-events-none absolute top-1/2 right-2.5 size-3.5 -translate-y-1/2 text-gray-500 dark:text-gray-400" aria-hidden="true" />
                  </div>
                  <button
                    type="button"
                    (click)="remove(member)"
                    [appTooltip]="'Remove ' + member.email"
                    appTooltipPosition="top"
                    [attr.aria-label]="'Remove ' + member.email"
                    class="flex size-8 items-center justify-center rounded-2xl text-gray-500 hover:bg-state-danger-50 hover:text-state-danger-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-state-danger-600 dark:text-gray-400 dark:hover:bg-state-danger-900/20 dark:hover:text-state-danger-400"
                  >
                    <ng-icon name="heroTrash" class="size-4" aria-hidden="true" />
                  </button>
                } @else {
                  <span class="text-sm/6 text-gray-600 dark:text-gray-400">{{ roleLabels[member.role] }}</span>
                }

                @if (canTransferTo(member)) {
                  <button
                    type="button"
                    (click)="transfer(member)"
                    class="rounded-2xl border border-gray-300 bg-white px-3 py-1 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
                  >
                    Make owner
                  </button>
                }
              </li>
            }
          </ul>
          @if (isOwner() && !hasTransferTarget()) {
            <p class="mt-2 text-xs/5 text-gray-600 dark:text-gray-400">
              To hand this project to someone else, make them an editor. Once they’ve opened the project, you can make them the owner.
            </p>
          }
        }
      </section>
    </div>
  `,
})
export class ProjectMembersComponent {
  private api = inject(ProjectApiService);
  private projects = inject(ProjectsService);
  private user = inject(UserService);
  private toast = inject(ToastService);
  private dialog = inject(Dialog);
  private router = inject(Router);

  readonly project = input.required<Project>();
  readonly projectChange = output<Project>();

  private readonly picker = viewChild(PeoplePickerComponent);

  protected readonly roleLabels = ROLE_LABELS;
  protected readonly members = signal<ProjectMember[]>([]);
  protected readonly serverCanManage = signal(false);
  protected readonly loading = signal(true);
  protected readonly adding = signal(false);
  protected readonly error = signal<string | null>(null);

  protected readonly me = computed(() => (this.user.currentUser()?.email ?? '').toLowerCase());
  protected readonly isOwner = computed(() => this.project().role === 'owner');
  protected readonly active = computed(() => this.project().status === 'active');
  protected readonly canManage = computed(() => this.serverCanManage() && this.active());
  protected readonly canLeave = computed(() => !this.isOwner() && !!this.me());
  protected readonly hasTransferTarget = computed(() => this.members().some(m => this.canTransferTo(m)));

  // Keyed on the id, not the object: a tab hands back an updated project after most
  // changes, and that must not reload what it just saved.
  private readonly projectId = computed(() => this.project().projectId);

  constructor() {
    effect(() => {
      const id = this.projectId();
      untracked(() => void this.load(id));
    });
  }

  private async load(projectId: string): Promise<void> {
    this.loading.set(true);
    try {
      const response = await firstValueFrom(this.api.members(projectId));
      this.members.set(response.members);
      this.serverCanManage.set(response.canManage);
      this.error.set(null);
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'Members could not be loaded.'));
    } finally {
      this.loading.set(false);
    }
  }

  protected canChange(member: ProjectMember): boolean {
    return this.canManage() && member.role !== 'owner';
  }

  protected canTransferTo(member: ProjectMember): boolean {
    return this.isOwner() && this.active() && member.role === 'editor' && member.hasSignedIn;
  }

  protected async add(request: PeoplePickerSubmit): Promise<void> {
    this.adding.set(true);
    try {
      const result = await firstValueFrom(this.api.addMembers(this.project().projectId, request.emails, request.role));
      this.picker()?.reset();
      const parts = [
        result.added.length && `${result.added.length} added`,
        result.alreadyMembers.length && `${result.alreadyMembers.length} already in the project`,
        result.invalid.length && `${result.invalid.length} not valid emails`,
        result.overCapacity.length && `${result.overCapacity.length} over the member limit`,
      ].filter(Boolean);
      const summary = parts.join(', ');
      if (result.invalid.length || result.overCapacity.length) this.toast.warning('Some people weren’t added', summary);
      else this.toast.success('People added', summary);
      await this.refresh();
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'Those people could not be added.'));
    } finally {
      this.adding.set(false);
    }
  }

  protected async changeRole(member: ProjectMember, role: MemberRole): Promise<void> {
    if (member.role === role) return;
    try {
      await firstValueFrom(this.api.updateMember(this.project().projectId, member.email, role));
      this.members.update(list => list.map(m => (m.email === member.email ? { ...m, role } : m)));
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'That role could not be changed.'));
      await this.load(this.project().projectId);
    }
  }

  protected async remove(member: ProjectMember): Promise<void> {
    const ok = await this.confirm({
      title: `Remove ${member.email}?`,
      message: 'They lose access to this project and everything shared in it. Their own tasks stay theirs.',
      confirmText: 'Remove',
      destructive: true,
    });
    if (!ok) return;
    try {
      await firstValueFrom(this.api.removeMember(this.project().projectId, member.email));
      await this.refresh();
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'They could not be removed.'));
    }
  }

  protected async leave(): Promise<void> {
    const ok = await this.confirm({
      title: 'Leave this project?',
      message: 'You lose access to it and to the tasks shared in it. Someone would have to add you again.',
      confirmText: 'Leave project',
      destructive: true,
    });
    if (!ok) return;
    try {
      await firstValueFrom(this.api.leave(this.project().projectId));
      this.projects.remove(this.project().projectId);
      await this.router.navigate(['/projects']);
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'You could not leave the project.'));
    }
  }

  protected async transfer(member: ProjectMember): Promise<void> {
    const ok = await this.confirm({
      title: `Make ${member.email} the owner?`,
      message: 'They take over settings, archiving and deletion. You stay on as an editor.',
      confirmText: 'Make owner',
    });
    if (!ok) return;
    try {
      const project = await firstValueFrom(this.api.transfer(this.project().projectId, member.email));
      this.projectChange.emit(project);
      await this.load(project.projectId);
    } catch (err) {
      this.error.set(projectErrorMessage(err, 'Ownership could not be transferred.'));
    }
  }

  /** Reload members and the project (its member count moved). */
  private async refresh(): Promise<void> {
    const id = this.project().projectId;
    await this.load(id);
    try {
      this.projectChange.emit(await firstValueFrom(this.api.get(id)));
    } catch {
      // The members list is already current; the count catches up on the next load.
    }
  }

  private async confirm(data: ConfirmationDialogData): Promise<boolean> {
    const ref = this.dialog.open<boolean>(ConfirmationDialogComponent, { data });
    return (await firstValueFrom(ref.closed)) === true;
  }
}
