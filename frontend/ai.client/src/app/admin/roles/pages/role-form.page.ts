import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
  OnInit,
  Signal,
} from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { Router, ActivatedRoute } from '@angular/router';
import {
  FormBuilder,
  FormGroup,
  FormControl,
  Validators,
  ReactiveFormsModule,
} from '@angular/forms';
import { map } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroInformationCircle,
} from '@ng-icons/heroicons/outline';
import { AppRolesService } from '../services/app-roles.service';
import { AdminToolService } from '../../tools/services/admin-tool.service';
import { ManagedModelsService } from '../../manage-models/services/managed-models.service';
import { AppRoleCreateRequest, AppRoleUpdateRequest } from '../models/app-role.model';
import { AdminScope } from '../../admin-scope.model';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';
import { ToolSelectorComponent } from '../../../components/tool-selector/tool-selector.component';
import { ToolSelectorItem } from '../../../components/tool-selector/tool-selector.model';
import { isRetiring } from '../../../shared/utils/retirement';

interface RoleFormGroup {
  roleId: FormControl<string>;
  displayName: FormControl<string>;
  description: FormControl<string>;
  jwtRoleMappings: FormControl<string>;
  inheritsFrom: FormControl<string[]>;
  grantedTools: FormControl<string[]>;
  grantedModels: FormControl<string[]>;
  grantedAdminScopes: FormControl<string[]>;
  priority: FormControl<number>;
  enabled: FormControl<boolean>;
}

/** The form's list-valued grants, each drawn by one `<app-tool-selector>`. */
type GrantControl = 'inheritsFrom' | 'grantedTools' | 'grantedModels' | 'grantedAdminScopes';

/** The wildcard grant: every tool, or every model. */
const WILDCARD = '*';

@Component({
  selector: 'app-role-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReactiveFormsModule, NgIcon, SpinnerComponent, ToolSelectorComponent],
  providers: [
    provideIcons({ heroArrowLeft, heroInformationCircle }),
  ],
  host: {
    class: 'block',
  },
  template: `
    <div class="max-w-4xl">
        <!-- Back Button -->
        <button
          (click)="goBack()"
          class="mb-6 inline-flex items-center gap-2 text-sm text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-gray-200"
        >
          <ng-icon name="heroArrowLeft" class="size-4" />
          Back to Roles
        </button>

        <!-- Page Header -->
        <div class="mb-8">
          <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">
            {{ pageTitle() }}
          </h1>
          <p class="mt-2 text-base/7 text-gray-600 dark:text-gray-400">
            {{ isEditMode() ? 'Update role settings and permissions' : 'Create a new application role with permissions' }}
          </p>
        </div>

        <!-- Loading State -->
        @if (loading()) {
          <div class="flex items-center justify-center h-64">
            <div class="flex flex-col items-center gap-4">
              <app-spinner size="xl" label="Loading role" />
              <p class="text-sm text-gray-500 dark:text-gray-400">
                Loading role...
              </p>
            </div>
          </div>
        } @else {
          <!-- Form -->
          <form [formGroup]="roleForm" (ngSubmit)="onSubmit()" class="space-y-8">
            <!-- Basic Information Section -->
            <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
              <h2 class="mb-6 text-xl/8 font-semibold text-gray-900 dark:text-white">
                Basic Information
              </h2>

              <div class="space-y-4">
                <!-- Role ID -->
                <div>
                  <label for="roleId" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Role ID <span class="text-state-danger-600">*</span>
                  </label>
                  <input
                    type="text"
                    id="roleId"
                    formControlName="roleId"
                    placeholder="e.g., basic_user, power_user, developer"
                    [readonly]="isEditMode()"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-hidden focus:ring-3 focus:ring-primary-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500 read-only:bg-gray-100 read-only:dark:bg-gray-600"
                    [class.border-state-danger-500]="roleForm.controls.roleId.invalid && roleForm.controls.roleId.touched"
                  />
                  <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                    Lowercase letters, numbers, and underscores only. 3-50 characters.
                  </p>
                  @if (roleForm.controls.roleId.invalid && roleForm.controls.roleId.touched) {
                    <p class="mt-1 text-sm/6 text-state-danger-600 dark:text-state-danger-400">
                      @if (roleForm.controls.roleId.errors?.['required']) {
                        Role ID is required
                      } @else if (roleForm.controls.roleId.errors?.['pattern']) {
                        Role ID must be lowercase letters, numbers, and underscores only
                      } @else if (roleForm.controls.roleId.errors?.['minlength']) {
                        Role ID must be at least 3 characters
                      } @else if (roleForm.controls.roleId.errors?.['maxlength']) {
                        Role ID must be at most 50 characters
                      }
                    </p>
                  }
                </div>

                <!-- Display Name -->
                <div>
                  <label for="displayName" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Display Name <span class="text-state-danger-600">*</span>
                  </label>
                  <input
                    type="text"
                    id="displayName"
                    formControlName="displayName"
                    placeholder="e.g., Basic User, Power User, Developer"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-hidden focus:ring-3 focus:ring-primary-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                    [class.border-state-danger-500]="roleForm.controls.displayName.invalid && roleForm.controls.displayName.touched"
                  />
                  @if (roleForm.controls.displayName.invalid && roleForm.controls.displayName.touched) {
                    <p class="mt-1 text-sm/6 text-state-danger-600 dark:text-state-danger-400">Display name is required</p>
                  }
                </div>

                <!-- Description -->
                <div>
                  <label for="description" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Description
                  </label>
                  <textarea
                    id="description"
                    formControlName="description"
                    rows="3"
                    placeholder="Describe what this role is for..."
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-hidden focus:ring-3 focus:ring-primary-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  ></textarea>
                  <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                    Optional description for administrators. Max 500 characters.
                  </p>
                </div>

                <!-- Priority -->
                <div>
                  <label for="priority" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Priority
                  </label>
                  <input
                    type="number"
                    id="priority"
                    formControlName="priority"
                    min="0"
                    max="999"
                    class="mt-1 block w-32 rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-hidden focus:ring-3 focus:ring-primary-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white"
                  />
                  <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                    Higher priority roles take precedence for quota tier selection (0-999).
                  </p>
                </div>

                <!-- Enabled -->
                <div class="flex items-center gap-3">
                  <input
                    type="checkbox"
                    id="enabled"
                    formControlName="enabled"
                    class="size-4 rounded-xs border-gray-300 text-primary-600 focus:ring-3 focus:ring-primary-500/50 dark:border-gray-600 dark:bg-gray-700"
                  />
                  <label for="enabled" class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Role Enabled
                  </label>
                </div>
              </div>
            </div>

            <!-- JWT Mappings Section -->
            <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
              <h2 class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">
                JWT Role Mappings
              </h2>
              <p class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                Users with these JWT roles will automatically receive this AppRole.
              </p>

              <div>
                <label for="jwtRoleMappings" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                  JWT Roles (comma-separated)
                </label>
                <input
                  type="text"
                  id="jwtRoleMappings"
                  formControlName="jwtRoleMappings"
                  placeholder="e.g., User, Admin, PSEmeriti Entra Sync"
                  class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-hidden focus:ring-3 focus:ring-primary-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                />
                <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                  Enter the group names exactly as your identity provider sends them,
                  separated by commas. Group names may contain spaces
                  (<span class="font-medium">PSEmeriti Entra Sync</span>) &mdash; only the
                  comma separates one name from the next, so a name cannot itself contain
                  one. Surrounding spaces are trimmed; a name that still fails to save is
                  usually carrying an invisible character from a copy&#8209;paste, and the
                  error will point at it.
                </p>
              </div>
            </div>

            <!-- Inheritance Section -->
            <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
              <h2 id="role-inherits-heading" class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">
                Role Inheritance
              </h2>
              <p id="role-inherits-blurb" class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                This role will inherit permissions from selected parent roles.
                Inherited tools and models are merged with directly granted permissions.
              </p>

              <app-tool-selector
                [items]="parentRoleItems()"
                [selected]="inheritsFromSelection()"
                (selectedChange)="setSelection('inheritsFrom', $event)"
                labelledBy="role-inherits-heading"
                describedBy="role-inherits-blurb"
                noun="role"
                nounPlural="roles"
                emptyText="No other roles available for inheritance."
                [bulkActions]="false"
                maxHeight="sm"
              />
            </div>

            <!-- Tool Permissions Section -->
            <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
              <h2 id="role-tools-heading" class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">
                Tool Permissions
              </h2>
              <p id="role-tools-blurb" class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                Select which tools users with this role can access.
              </p>

              <div class="mb-4 flex items-center gap-3">
                <input
                  type="checkbox"
                  id="grantAllTools"
                  [checked]="grantsAllTools()"
                  (change)="toggleWildcard('grantedTools', $event)"
                  class="size-4 cursor-pointer rounded accent-primary-accessible focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:accent-primary-500"
                />
                <label for="grantAllTools" class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                  Grant access to all tools
                </label>
              </div>

              @if (!grantsAllTools()) {
                @if (toolsResource.isLoading()) {
                  <p class="text-sm/6 text-gray-600 dark:text-gray-400">Loading tools...</p>
                } @else {
                  <app-tool-selector
                    [items]="toolItems()"
                    [selected]="grantedToolsSelection()"
                    (selectedChange)="setSelection('grantedTools', $event)"
                    labelledBy="role-tools-heading"
                    describedBy="role-tools-blurb"
                    emptyText="No tools available. Configure tools in the tool catalog first."
                    maxHeight="lg"
                  />
                }
              }
            </div>

            <!-- Admin Access Section -->
            <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
              <h2 id="role-admin-heading" class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">
                Admin Access
              </h2>
              <p id="role-admin-blurb" class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                Grant this role access to specific areas of the admin console. Members
                get only the areas selected here — everything else stays hidden.
                Managing roles and auth providers cannot be delegated, because either
                one would let the holder grant themselves full admin.
              </p>

              @if (adminScopesLoading()) {
                <p class="text-sm/6 text-gray-600 dark:text-gray-400">Loading admin areas...</p>
              } @else {
                <app-tool-selector
                  [items]="adminScopeItems()"
                  [selected]="grantedAdminScopesSelection()"
                  (selectedChange)="setSelection('grantedAdminScopes', $event)"
                  labelledBy="role-admin-heading"
                  describedBy="role-admin-blurb"
                  noun="admin area"
                  nounPlural="admin areas"
                  emptyText="Could not load admin areas."
                  maxHeight="lg"
                />
              }
            </div>

            <!-- Model Permissions Section -->
            <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
              <h2 id="role-models-heading" class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">
                Model Permissions
              </h2>
              <p id="role-models-blurb" class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                Select which AI models users with this role can access.
              </p>

              <div class="mb-4 flex items-center gap-3">
                <input
                  type="checkbox"
                  id="grantAllModels"
                  [checked]="grantsAllModels()"
                  (change)="toggleWildcard('grantedModels', $event)"
                  class="size-4 cursor-pointer rounded accent-primary-accessible focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:accent-primary-500"
                />
                <label for="grantAllModels" class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                  Grant access to all models
                </label>
              </div>

              @if (!grantsAllModels()) {
                @if (modelsResource.isLoading()) {
                  <p class="text-sm/6 text-gray-600 dark:text-gray-400">Loading models...</p>
                } @else {
                  <app-tool-selector
                    [items]="modelItems()"
                    [selected]="grantedModelsSelection()"
                    (selectedChange)="setSelection('grantedModels', $event)"
                    labelledBy="role-models-heading"
                    describedBy="role-models-blurb"
                    noun="model"
                    nounPlural="models"
                    emptyText="No models available. Add models in Manage Models first."
                  />
                }
              }
            </div>

            <!-- Form Actions -->
            <div class="flex gap-3 border-t border-gray-200 pt-6 dark:border-gray-700">
              <button
                type="submit"
                [disabled]="isSubmitting() || roleForm.invalid"
                class="rounded-2xl bg-primary-accessible px-6 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus:outline-hidden focus:ring-3 focus:ring-primary-500/50 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                @if (isSubmitting()) {
                  Saving...
                } @else {
                  {{ isEditMode() ? 'Update Role' : 'Create Role' }}
                }
              </button>
              <button
                type="button"
                (click)="goBack()"
                [disabled]="isSubmitting()"
                class="rounded-2xl border border-gray-300 bg-white px-6 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus:outline-hidden focus:ring-3 focus:ring-gray-500/50 disabled:opacity-50 disabled:cursor-not-allowed dark:border-gray-600 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
              >
                Cancel
              </button>
            </div>
          </form>
        }
    </div>
  `,
})
export class RoleFormPage implements OnInit {
  private fb = inject(FormBuilder);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private appRolesService = inject(AppRolesService);
  private adminToolService = inject(AdminToolService);
  private managedModelsService = inject(ManagedModelsService);

  // Resources
  readonly toolsResource = this.adminToolService.toolsResource;
  readonly modelsResource = this.managedModelsService.modelsResource;

  // State
  readonly isEditMode = signal(false);
  readonly roleId = signal<string | null>(null);
  readonly isSubmitting = signal(false);
  readonly loading = signal(false);

  // Form
  readonly roleForm: FormGroup<RoleFormGroup> = this.fb.group({
    roleId: this.fb.control('', {
      nonNullable: true,
      validators: [
        Validators.required,
        Validators.minLength(3),
        Validators.maxLength(50),
        Validators.pattern(/^[a-z][a-z0-9_]*$/),
      ],
    }),
    displayName: this.fb.control('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(100)],
    }),
    description: this.fb.control('', {
      nonNullable: true,
      validators: [Validators.maxLength(500)],
    }),
    jwtRoleMappings: this.fb.control('', { nonNullable: true }),
    inheritsFrom: this.fb.control<string[]>([], { nonNullable: true }),
    grantedTools: this.fb.control<string[]>([], { nonNullable: true }),
    grantedModels: this.fb.control<string[]>([], { nonNullable: true }),
    grantedAdminScopes: this.fb.control<string[]>([], { nonNullable: true }),
    priority: this.fb.control(0, {
      nonNullable: true,
      validators: [Validators.min(0), Validators.max(1000)],
    }),
    enabled: this.fb.control(true, { nonNullable: true }),
  });

  readonly pageTitle = computed(() =>
    this.isEditMode() ? 'Edit Role' : 'Create Role'
  );

  /**
   * The role record's own `granted*` lists are what access checks read, so each
   * picker edits its form control directly: the selection is a view of the
   * control's array, and a change writes the array back. Ids the catalog no
   * longer lists (a retired tool, a disabled parent role) are not rows, so they
   * stay in the array untouched rather than being dropped on save.
   */
  readonly inheritsFromSelection = this.selectionOf('inheritsFrom');
  readonly grantedToolsSelection = this.selectionOf('grantedTools');
  readonly grantedModelsSelection = this.selectionOf('grantedModels');
  readonly grantedAdminScopesSelection = this.selectionOf('grantedAdminScopes');

  readonly grantsAllTools = computed(() => this.grantedToolsSelection().has(WILDCARD));
  readonly grantsAllModels = computed(() => this.grantedModelsSelection().has(WILDCARD));

  readonly toolItems = computed<ToolSelectorItem[]>(() =>
    this.adminToolService.getTools().map((tool) => ({
      id: tool.toolId,
      name: tool.displayName,
      description: tool.description,
      group: tool.category,
      badge: isRetiring(tool) ? 'retiring' : undefined,
    })),
  );

  readonly modelItems = computed<ToolSelectorItem[]>(() =>
    this.managedModelsService.getManagedModels().map((model) => ({
      id: model.modelId,
      name: model.modelName,
      // The Bedrock id is what an admin cross-checks against a card or a log,
      // and putting it here makes it searchable.
      description: model.modelId,
      group: model.providerName,
      badge: model.enabled ? undefined : 'disabled',
    })),
  );

  /**
   * The admin scope registry, as rows. The selector groups in first-seen order
   * from the server's own `group` field, which mirrors the admin nav headings —
   * so the picker reads in the same order as the sidebar the grantee will end
   * up looking at.
   */
  readonly adminScopes = signal<AdminScope[]>([]);
  readonly adminScopesLoading = signal(false);

  readonly adminScopeItems = computed<ToolSelectorItem[]>(() =>
    this.adminScopes().map((scope) => ({
      id: scope.id,
      name: scope.label,
      description: scope.description,
      group: scope.group,
      // The server rejects these regardless (`validate_admin_scopes`); locking
      // the row says so up front instead of on a 400 after Save.
      locked: !scope.delegable,
      note: scope.delegable ? undefined : 'System admins only',
      noteTone: scope.delegable ? undefined : ('muted' as const),
    })),
  );

  readonly parentRoleItems = computed<ToolSelectorItem[]>(() => {
    const currentRoleId = this.roleId();
    return this.appRolesService
      .getEnabledRoles()
      .filter((r) => r.roleId !== currentRoleId)
      .map((role) => ({ id: role.roleId, name: role.displayName, description: role.description }));
  });

  ngOnInit(): void {
    void this.loadAdminScopes();

    const id = this.route.snapshot.paramMap.get('id');
    if (id && id !== 'new') {
      this.isEditMode.set(true);
      this.roleId.set(id);
      this.loadRoleData(id);
    }
  }

  private async loadAdminScopes(): Promise<void> {
    this.adminScopesLoading.set(true);
    try {
      const response = await this.appRolesService.fetchAdminScopes();
      this.adminScopes.set(response.scopes);
    } catch (error) {
      // Non-fatal: the rest of the form still works, and the section renders
      // its empty state. Blocking role editing because one optional picker
      // could not load would be a worse trade.
      console.error('Failed to load admin scopes:', error);
      this.adminScopes.set([]);
    } finally {
      this.adminScopesLoading.set(false);
    }
  }

  private async loadRoleData(id: string): Promise<void> {
    this.loading.set(true);
    try {
      const role = await this.appRolesService.fetchRole(id);
      this.roleForm.patchValue({
        roleId: role.roleId,
        displayName: role.displayName,
        description: role.description,
        jwtRoleMappings: role.jwtRoleMappings.join(', '),
        inheritsFrom: role.inheritsFrom,
        grantedTools: role.grantedTools,
        grantedModels: role.grantedModels,
        // `?? []` — a role written before admin scopes existed has no such
        // field, and patchValue(undefined) would leave the control untouched
        // rather than clearing it.
        grantedAdminScopes: role.grantedAdminScopes ?? [],
        priority: role.priority,
        enabled: role.enabled,
      });
    } catch (error) {
      console.error('Error loading role:', error);
      alert('Failed to load role. Returning to list.');
      this.router.navigate(['/admin/roles']);
    } finally {
      this.loading.set(false);
    }
  }

  /** Writes a picker's selection back to its control, keeping the array's existing order. */
  setSelection(controlName: GrantControl, next: ReadonlySet<string>): void {
    const control = this.roleForm.controls[controlName];
    control.setValue([...next]);
    control.markAsDirty();
  }

  toggleWildcard(
    controlName: 'grantedTools' | 'grantedModels',
    event: Event
  ): void {
    const checked = (event.target as HTMLInputElement).checked;
    const control = this.roleForm.controls[controlName];
    control.setValue(checked ? [WILDCARD] : []);
    control.markAsDirty();
  }

  private selectionOf(controlName: GrantControl): Signal<ReadonlySet<string>> {
    const control = this.roleForm.controls[controlName];
    return toSignal(control.valueChanges.pipe(map((value) => new Set(value ?? []))), {
      initialValue: new Set(control.value),
    });
  }

  async onSubmit(): Promise<void> {
    if (this.roleForm.invalid) {
      this.roleForm.markAllAsTouched();
      return;
    }

    this.isSubmitting.set(true);

    try {
      const formValue = this.roleForm.value;

      // Parse JWT mappings from comma-separated string
      const jwtMappings = formValue.jwtRoleMappings
        ? formValue.jwtRoleMappings
            .split(',')
            .map((s: string) => s.trim())
            .filter((s: string) => s.length > 0)
        : [];

      if (this.isEditMode() && this.roleId()) {
        const updates: AppRoleUpdateRequest = {
          displayName: formValue.displayName,
          description: formValue.description,
          jwtRoleMappings: jwtMappings,
          inheritsFrom: formValue.inheritsFrom,
          grantedTools: formValue.grantedTools,
          grantedModels: formValue.grantedModels,
          grantedAdminScopes: formValue.grantedAdminScopes,
          priority: formValue.priority,
          enabled: formValue.enabled,
        };
        await this.appRolesService.updateRole(this.roleId()!, updates);
      } else {
        const createData: AppRoleCreateRequest = {
          roleId: formValue.roleId!,
          displayName: formValue.displayName!,
          description: formValue.description,
          jwtRoleMappings: jwtMappings,
          inheritsFrom: formValue.inheritsFrom,
          grantedTools: formValue.grantedTools,
          grantedModels: formValue.grantedModels,
          grantedAdminScopes: formValue.grantedAdminScopes,
          priority: formValue.priority,
          enabled: formValue.enabled,
        };
        await this.appRolesService.createRole(createData);
      }

      this.router.navigate(['/admin/roles']);
    } catch (error: any) {
      console.error('Error saving role:', error);
      const message =
        error?.error?.detail || error?.message || 'Failed to save role.';
      alert(message);
    } finally {
      this.isSubmitting.set(false);
    }
  }

  goBack(): void {
    this.router.navigate(['/admin/roles']);
  }
}
