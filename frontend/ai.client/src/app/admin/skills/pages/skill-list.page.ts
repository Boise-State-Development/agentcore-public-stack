import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
} from '@angular/core';
import { RouterLink } from '@angular/router';
import { FormsModule } from '@angular/forms';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroPlus,
  heroMagnifyingGlass,
  heroChevronDown,
  heroPencilSquare,
  heroTrash,
  heroUserGroup,
  heroWrenchScrewdriver,
  heroDocumentText,
} from '@ng-icons/heroicons/outline';
import { AdminSkillService } from '../services/admin-skill.service';
import { AdminSkill, SKILL_STATUSES } from '../models/admin-skill.model';
import { AppRolesService } from '../../roles/services/app-roles.service';
import { parseScopedToolId } from '../../../shared/utils/scoped-tool-id';
import { AdminToolService } from '../../tools/services/admin-tool.service';
import {
  SkillRoleDialogComponent,
  SkillRoleDialogData,
  SkillRoleDialogResult,
} from '../components/skill-role-dialog.component';

@Component({
  selector: 'app-skill-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, FormsModule, NgIcon],
  providers: [
    provideIcons({
      heroPlus,
      heroMagnifyingGlass,
      heroChevronDown,
      heroPencilSquare,
      heroTrash,
      heroUserGroup,
      heroWrenchScrewdriver,
      heroDocumentText,
    }),
  ],
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-5xl px-4 py-8 sm:px-6 lg:px-8">
        <!-- Page Header -->
        <div class="mb-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
          <div>
            <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">Skill Catalog</h1>
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              Author skills that bundle instructions, reference files and bound tools, then grant them to roles.
            </p>
          </div>
          <a
            routerLink="/admin/skills/new"
            class="inline-flex shrink-0 items-center gap-2 rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:bg-blue-500 dark:hover:bg-blue-600"
          >
            <ng-icon name="heroPlus" class="size-5" aria-hidden="true" />
            Add Skill
          </a>
        </div>

        <!-- Toolbar: search + status filter -->
        <div class="mb-3 flex flex-col gap-2 sm:flex-row sm:items-center">
          <div class="relative flex-1">
            <ng-icon
              name="heroMagnifyingGlass"
              class="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-gray-400 dark:text-gray-500"
              aria-hidden="true"
            />
            <label for="search" class="sr-only">Search skills</label>
            <input
              type="text"
              id="search"
              [ngModel]="searchQuery()"
              (ngModelChange)="searchQuery.set($event)"
              placeholder="Search by name, ID, or description…"
              class="block w-full rounded-2xl border border-gray-300 bg-white py-2 pl-9 pr-3 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
            />
          </div>

          <label for="status" class="sr-only">Filter by status</label>
          <select
            id="status"
            [ngModel]="statusFilter()"
            (ngModelChange)="statusFilter.set($event)"
            class="rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-none focus:ring-2 focus:ring-blue-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white"
          >
            <option value="">All statuses</option>
            @for (status of statuses; track status.value) {
              <option [value]="status.value">{{ status.label }}</option>
            }
          </select>

          @if (hasActiveFilters()) {
            <button
              (click)="resetFilters()"
              class="rounded-2xl px-3 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-white"
            >
              Reset
            </button>
          }
        </div>

        <!-- Count -->
        <div class="mb-3 text-xs/5 text-gray-500 dark:text-gray-400">
          {{ filteredSkills().length }} skill{{ filteredSkills().length !== 1 ? 's' : '' }}
        </div>

        <!-- Loading State -->
        @if (skillsResource.isLoading() && skills().length === 0) {
          <div class="flex h-64 items-center justify-center">
            <div class="flex flex-col items-center gap-4">
              <div class="size-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-600 dark:border-t-blue-400"></div>
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">Loading skills…</p>
            </div>
          </div>
        }

        <!-- Error State -->
        @if (skillsResource.error()) {
          <div class="mb-6 rounded-2xl border border-red-200 bg-red-50 p-4 text-red-800 dark:border-red-800 dark:bg-red-900/20 dark:text-red-200">
            <p class="text-sm/6">Failed to load skills. Please try again.</p>
            <button
              (click)="adminSkillService.reload()"
              class="mt-2 text-sm/6 font-medium underline hover:no-underline"
            >
              Retry
            </button>
          </div>
        }

        <!-- Skills List -->
        @if (!skillsResource.isLoading() || skills().length > 0) {
          @if (filteredSkills().length === 0) {
            <div class="rounded-2xl border border-dashed border-gray-300 bg-white p-12 text-center dark:border-gray-700 dark:bg-gray-800">
              @if (hasActiveFilters()) {
                <p class="text-sm/6 text-gray-500 dark:text-gray-400">No skills match the current filters.</p>
              } @else {
                <p class="text-sm/6 text-gray-500 dark:text-gray-400">No skills in catalog yet.</p>
                <a
                  routerLink="/admin/skills/new"
                  class="mt-4 inline-flex items-center gap-2 rounded-2xl bg-blue-600 px-4 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 dark:bg-blue-500 dark:hover:bg-blue-600"
                >
                  <ng-icon name="heroPlus" class="size-5" aria-hidden="true" />
                  Add Skill
                </a>
              }
            </div>
          } @else {
            <ul class="divide-y divide-gray-200 overflow-hidden rounded-2xl border border-gray-200 bg-white dark:divide-gray-700 dark:border-gray-700 dark:bg-gray-800">
              @for (skill of filteredSkills(); track skill.skillId) {
                <li>
                  <!-- Row -->
                  <div class="flex items-center gap-3 px-3 py-2.5 sm:px-4">
                    <button
                      type="button"
                      (click)="toggleExpand(skill.skillId)"
                      [attr.aria-expanded]="isExpanded(skill.skillId)"
                      [attr.aria-controls]="'skill-detail-' + skill.skillId"
                      [attr.aria-label]="(isExpanded(skill.skillId) ? 'Hide' : 'Show') + ' details for ' + skill.displayName"
                      class="flex size-7 shrink-0 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                    >
                      <ng-icon
                        name="heroChevronDown"
                        class="size-4 transition-transform duration-150"
                        [class.rotate-180]="isExpanded(skill.skillId)"
                        aria-hidden="true"
                      />
                    </button>

                    <!-- Name + skill id -->
                    <div class="min-w-0 flex-1">
                      <span class="block truncate text-sm/6 font-medium text-gray-900 dark:text-white">
                        {{ skill.displayName }}
                      </span>
                      <p class="truncate font-mono text-xs/5 text-gray-500 dark:text-gray-400">
                        {{ skill.skillId }}
                      </p>
                    </div>

                    <!-- Bound tools count -->
                    <span class="hidden shrink-0 items-center gap-1 text-xs/5 text-gray-500 sm:inline-flex dark:text-gray-400" [title]="skill.boundToolIds.length + ' bound tool(s)'">
                      <ng-icon name="heroWrenchScrewdriver" class="size-4" aria-hidden="true" />
                      {{ skill.boundToolIds.length }}
                    </span>

                    <!-- Reference files count -->
                    <span class="hidden shrink-0 items-center gap-1 text-xs/5 text-gray-500 sm:inline-flex dark:text-gray-400" [title]="skill.resources.length + ' reference file(s)'">
                      <ng-icon name="heroDocumentText" class="size-4" aria-hidden="true" />
                      {{ skill.resources.length }}
                    </span>

                    <!-- Roles count -->
                    <span class="hidden w-16 shrink-0 justify-end text-right text-xs/5 text-gray-500 sm:flex dark:text-gray-400">
                      {{ skill.allowedAppRoles.length }} role{{ skill.allowedAppRoles.length !== 1 ? 's' : '' }}
                    </span>

                    <!-- Status -->
                    <span [class]="getStatusClass(skill.status)">{{ skill.status }}</span>

                    <!-- Actions -->
                    <div class="flex shrink-0 items-center gap-1">
                      <button
                        type="button"
                        (click)="openRoleDialog(skill)"
                        [attr.aria-label]="'Manage role access for ' + skill.displayName"
                        [title]="'Manage role access for ' + skill.displayName"
                        class="flex size-8 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                      >
                        <ng-icon name="heroUserGroup" class="size-4" aria-hidden="true" />
                      </button>
                      <a
                        [routerLink]="['/admin/skills/edit', skill.skillId]"
                        [attr.aria-label]="'Edit ' + skill.displayName"
                        [title]="'Edit ' + skill.displayName"
                        class="flex size-8 items-center justify-center rounded-2xl text-gray-400 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                      >
                        <ng-icon name="heroPencilSquare" class="size-4" aria-hidden="true" />
                      </a>
                      <button
                        type="button"
                        (click)="deleteSkill(skill)"
                        [attr.aria-label]="'Delete ' + skill.displayName"
                        [title]="'Delete ' + skill.displayName"
                        class="flex size-8 items-center justify-center rounded-2xl text-gray-400 hover:bg-red-50 hover:text-red-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-red-500 dark:text-gray-500 dark:hover:bg-red-900/20 dark:hover:text-red-400"
                      >
                        <ng-icon name="heroTrash" class="size-4" aria-hidden="true" />
                      </button>
                    </div>
                  </div>

                  <!-- Expanded detail -->
                  @if (isExpanded(skill.skillId)) {
                    <div
                      [id]="'skill-detail-' + skill.skillId"
                      class="border-t border-gray-100 bg-gray-50 px-4 py-3 sm:pl-14 dark:border-gray-700/60 dark:bg-gray-900/40"
                    >
                      <dl class="grid grid-cols-1 gap-x-8 gap-y-3 sm:grid-cols-2">
                        <div class="sm:col-span-2">
                          <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Description</dt>
                          <dd class="mt-0.5 text-sm/6 text-gray-700 dark:text-gray-300">
                            {{ skill.description || 'No description provided.' }}
                          </dd>
                        </div>

                        <div>
                          <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Bound tools</dt>
                          <dd class="mt-1 flex flex-wrap gap-1.5">
                            @if (skill.boundToolIds.length > 0) {
                              @for (toolId of skill.boundToolIds; track toolId) {
                                <span class="inline-flex items-center rounded-2xl bg-blue-100 px-2 py-0.5 font-mono text-xs/5 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300" [title]="toolId">
                                  {{ getToolDisplayName(toolId) }}
                                </span>
                              }
                            } @else {
                              <span class="text-xs/5 italic text-gray-500 dark:text-gray-400">No tools bound</span>
                            }
                          </dd>
                        </div>

                        <div>
                          <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Reference files</dt>
                          <dd class="mt-1 flex flex-wrap gap-1.5">
                            @if (skill.resources.length > 0) {
                              @for (res of skill.resources; track res.filename) {
                                <span class="inline-flex items-center rounded-2xl bg-gray-100 px-2 py-0.5 font-mono text-xs/5 text-gray-700 dark:bg-gray-700 dark:text-gray-300" [title]="res.size + ' bytes'">
                                  {{ res.filename }}
                                </span>
                              }
                            } @else {
                              <span class="text-xs/5 italic text-gray-500 dark:text-gray-400">No reference files</span>
                            }
                          </dd>
                        </div>

                        <div class="sm:col-span-2">
                          <dt class="text-xs/5 font-medium uppercase tracking-wide text-gray-500 dark:text-gray-400">Access</dt>
                          <dd class="mt-1 flex flex-wrap gap-1.5">
                            @if (skill.allowedAppRoles.length > 0) {
                              @for (roleId of skill.allowedAppRoles; track roleId) {
                                <span class="inline-flex items-center rounded-2xl bg-purple-100 px-2 py-0.5 text-xs/5 text-purple-700 dark:bg-purple-900/50 dark:text-purple-300" [title]="roleId">
                                  {{ getRoleDisplayName(roleId) }}
                                </span>
                              }
                            } @else {
                              <span class="text-xs/5 italic text-gray-500 dark:text-gray-400">No roles assigned</span>
                            }
                          </dd>
                        </div>
                      </dl>
                    </div>
                  }
                </li>
              }
            </ul>
          }
        }
      </div>
    </div>
  `,
})
export class SkillListPage {
  adminSkillService = inject(AdminSkillService);
  private dialog = inject(Dialog);
  private appRolesService = inject(AppRolesService);
  private adminToolService = inject(AdminToolService);

  readonly skillsResource = this.adminSkillService.skillsResource;
  readonly statuses = SKILL_STATUSES;

  searchQuery = signal('');
  statusFilter = signal('');

  private expandedIds = signal<ReadonlySet<string>>(new Set());

  readonly skills = computed(() => this.adminSkillService.getSkills());

  readonly filteredSkills = computed(() => {
    let skills = this.skills();
    const query = this.searchQuery().toLowerCase();
    const status = this.statusFilter();

    if (query) {
      skills = skills.filter(
        (s) =>
          s.displayName.toLowerCase().includes(query) ||
          s.skillId.toLowerCase().includes(query) ||
          s.description.toLowerCase().includes(query)
      );
    }

    if (status) {
      skills = skills.filter((s) => s.status === status);
    }

    return [...skills].sort((a, b) => {
      const catCompare = (a.category ?? '').localeCompare(b.category ?? '');
      if (catCompare !== 0) return catCompare;
      return a.displayName.localeCompare(b.displayName);
    });
  });

  readonly hasActiveFilters = computed(() => !!(this.searchQuery() || this.statusFilter()));

  resetFilters(): void {
    this.searchQuery.set('');
    this.statusFilter.set('');
  }

  isExpanded(skillId: string): boolean {
    return this.expandedIds().has(skillId);
  }

  toggleExpand(skillId: string): void {
    this.expandedIds.update((current) => {
      const next = new Set(current);
      if (next.has(skillId)) {
        next.delete(skillId);
      } else {
        next.add(skillId);
      }
      return next;
    });
  }

  getRoleDisplayName(roleId: string): string {
    return this.appRolesService.getRoleById(roleId)?.displayName ?? roleId;
  }

  getToolDisplayName(toolId: string): string {
    // A scoped id (`base::mcpToolName`) binds one tool of a server.
    const { base, name } = parseScopedToolId(toolId);
    const serverName = this.adminToolService.getToolById(base)?.displayName ?? base;
    return name ? `${serverName} · ${name}` : serverName;
  }

  getStatusClass(status: string): string {
    const base = 'shrink-0 rounded-2xl px-2.5 py-0.5 text-xs/5 font-medium';
    switch (status) {
      case 'active':
        return `${base} bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-300`;
      case 'draft':
        return `${base} bg-yellow-100 text-yellow-800 dark:bg-yellow-900/30 dark:text-yellow-300`;
      case 'disabled':
        return `${base} bg-red-100 text-red-800 dark:bg-red-900/30 dark:text-red-300`;
      default:
        return `${base} bg-gray-100 text-gray-800 dark:bg-gray-700 dark:text-gray-300`;
    }
  }

  async openRoleDialog(skill: AdminSkill): Promise<void> {
    const dialogRef = this.dialog.open<SkillRoleDialogResult>(SkillRoleDialogComponent, {
      data: { skill } as SkillRoleDialogData,
    });

    const result = await firstValueFrom(dialogRef.closed);
    if (result !== undefined) {
      try {
        await this.adminSkillService.setSkillRoles(skill.skillId, result);
      } catch (error: unknown) {
        console.error('Error saving roles:', error);
        alert(error instanceof Error ? error.message : 'Failed to save roles.');
      }
    }
  }

  async deleteSkill(skill: AdminSkill): Promise<void> {
    const confirmed = confirm(
      `Delete skill "${skill.displayName}"? This disables it; you can re-enable it later from the edit page.`
    );
    if (!confirmed) {
      return;
    }
    try {
      // Soft delete (status → disabled) keeps the row + reference-file manifest.
      await this.adminSkillService.deleteSkill(skill.skillId, false);
    } catch (error: unknown) {
      console.error('Error deleting skill:', error);
      alert(error instanceof Error ? error.message : 'Failed to delete skill.');
    }
  }
}
