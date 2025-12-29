import {
  Component,
  ChangeDetectionStrategy,
  inject,
  signal,
  computed,
  OnInit,
} from '@angular/core';
import { Router, ActivatedRoute } from '@angular/router';
import {
  FormBuilder,
  FormGroup,
  FormControl,
  Validators,
  ReactiveFormsModule,
} from '@angular/forms';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowLeft,
  heroInformationCircle,
} from '@ng-icons/heroicons/outline';
import { AppRolesService } from '../services/app-roles.service';
import { ToolsService } from '../../tools/services/tools.service';
import { ManagedModelsService } from '../../manage-models/services/managed-models.service';
import { AppRoleCreateRequest, AppRoleUpdateRequest } from '../models/app-role.model';

interface RoleFormGroup {
  roleId: FormControl<string>;
  displayName: FormControl<string>;
  description: FormControl<string>;
  jwtRoleMappings: FormControl<string>;
  inheritsFrom: FormControl<string[]>;
  grantedTools: FormControl<string[]>;
  grantedModels: FormControl<string[]>;
  priority: FormControl<number>;
  enabled: FormControl<boolean>;
}

@Component({
  selector: 'app-role-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReactiveFormsModule, NgIcon],
  providers: [
    provideIcons({ heroArrowLeft, heroInformationCircle }),
  ],
  host: {
    class: 'block',
  },
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-4xl px-4 py-8 sm:px-6 lg:px-8">
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
              <div
                class="animate-spin rounded-full size-12 border-4 border-gray-300 dark:border-gray-600 border-t-blue-600"
              ></div>
              <p class="text-sm text-gray-500 dark:text-gray-400">
                Loading role...
              </p>
            </div>
          </div>
        } @else {
          <!-- Form -->
          <form [formGroup]="roleForm" (ngSubmit)="onSubmit()" class="space-y-8">
            <!-- Basic Information Section -->
            <div class="rounded-sm border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800">
              <h2 class="mb-6 text-xl/8 font-semibold text-gray-900 dark:text-white">
                Basic Information
              </h2>

              <div class="space-y-4">
                <!-- Role ID -->
                <div>
                  <label for="roleId" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Role ID <span class="text-red-600">*</span>
                  </label>
                  <input
                    type="text"
                    id="roleId"
                    formControlName="roleId"
                    placeholder="e.g., basic_user, power_user, developer"
                    [readonly]="isEditMode()"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500 read-only:bg-gray-100 read-only:dark:bg-gray-600"
                    [class.border-red-500]="roleForm.controls.roleId.invalid && roleForm.controls.roleId.touched"
                  />
                  <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                    Lowercase letters, numbers, and underscores only. 3-50 characters.
                  </p>
                  @if (roleForm.controls.roleId.invalid && roleForm.controls.roleId.touched) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">
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
                    Display Name <span class="text-red-600">*</span>
                  </label>
                  <input
                    type="text"
                    id="displayName"
                    formControlName="displayName"
                    placeholder="e.g., Basic User, Power User, Developer"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                    [class.border-red-500]="roleForm.controls.displayName.invalid && roleForm.controls.displayName.touched"
                  />
                  @if (roleForm.controls.displayName.invalid && roleForm.controls.displayName.touched) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">Display name is required</p>
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
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
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
                    class="mt-1 block w-32 rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white"
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
                    class="size-4 rounded-xs border-gray-300 text-blue-600 focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700"
                  />
                  <label for="enabled" class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Role Enabled
                  </label>
                </div>
              </div>
            </div>

            <!-- JWT Mappings Section -->
            <div class="rounded-sm border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800">
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
                  placeholder="e.g., User, Admin, DotNetDevelopers"
                  class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                />
                <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                  Enter JWT role names separated by commas. These are the roles from your identity provider.
                </p>
              </div>
            </div>

            <!-- Inheritance Section -->
            <div class="rounded-sm border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800">
              <h2 class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">
                Role Inheritance
              </h2>
              <p class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                This role will inherit permissions from selected parent roles.
              </p>

              <div>
                <label class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300 mb-2">
                  Inherit From
                </label>
                @if (availableParentRoles().length > 0) {
                  <div class="flex flex-wrap gap-2">
                    @for (role of availableParentRoles(); track role.roleId) {
                      <button
                        type="button"
                        (click)="toggleArrayValue('inheritsFrom', role.roleId)"
                        [class.bg-purple-600]="isSelected('inheritsFrom', role.roleId)"
                        [class.text-white]="isSelected('inheritsFrom', role.roleId)"
                        [class.bg-gray-100]="!isSelected('inheritsFrom', role.roleId)"
                        [class.text-gray-700]="!isSelected('inheritsFrom', role.roleId)"
                        [class.dark:bg-purple-500]="isSelected('inheritsFrom', role.roleId)"
                        [class.dark:bg-gray-700]="!isSelected('inheritsFrom', role.roleId)"
                        [class.dark:text-gray-300]="!isSelected('inheritsFrom', role.roleId)"
                        class="rounded-sm px-3 py-1.5 text-sm/6 font-medium hover:opacity-80 focus:outline-hidden focus:ring-3 focus:ring-purple-500/50"
                        [title]="role.description"
                      >
                        {{ role.displayName }}
                      </button>
                    }
                  </div>
                } @else {
                  <p class="text-sm text-gray-500 dark:text-gray-400">
                    No other roles available for inheritance.
                  </p>
                }
                <p class="mt-2 text-xs/5 text-gray-500 dark:text-gray-400">
                  Inherited tools and models will be merged with directly granted permissions.
                </p>
              </div>
            </div>

            <!-- Tool Permissions Section -->
            <div class="rounded-sm border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800">
              <h2 class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">
                Tool Permissions
              </h2>
              <p class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                Select which tools users with this role can access.
              </p>

              <div>
                <!-- Grant All Tools Option -->
                <div class="mb-4 flex items-center gap-3">
                  <input
                    type="checkbox"
                    id="grantAllTools"
                    [checked]="isSelected('grantedTools', '*')"
                    (change)="toggleWildcard('grantedTools', $event)"
                    class="size-4 rounded-xs border-gray-300 text-green-600 focus:ring-3 focus:ring-green-500/50 dark:border-gray-600 dark:bg-gray-700"
                  />
                  <label for="grantAllTools" class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Grant access to all tools
                  </label>
                </div>

                @if (!isSelected('grantedTools', '*')) {
                  @if (toolsResource.isLoading()) {
                    <p class="text-sm text-gray-500 dark:text-gray-400">Loading tools...</p>
                  } @else if (availableTools().length > 0) {
                    <div class="flex flex-wrap gap-2">
                      @for (tool of availableTools(); track tool.toolId) {
                        <button
                          type="button"
                          (click)="toggleArrayValue('grantedTools', tool.toolId)"
                          [class.bg-green-600]="isSelected('grantedTools', tool.toolId)"
                          [class.text-white]="isSelected('grantedTools', tool.toolId)"
                          [class.bg-gray-100]="!isSelected('grantedTools', tool.toolId)"
                          [class.text-gray-700]="!isSelected('grantedTools', tool.toolId)"
                          [class.dark:bg-green-500]="isSelected('grantedTools', tool.toolId)"
                          [class.dark:bg-gray-700]="!isSelected('grantedTools', tool.toolId)"
                          [class.dark:text-gray-300]="!isSelected('grantedTools', tool.toolId)"
                          class="rounded-sm px-3 py-1.5 text-sm/6 font-medium hover:opacity-80 focus:outline-hidden focus:ring-3 focus:ring-green-500/50"
                          [title]="tool.description"
                        >
                          {{ tool.name }}
                        </button>
                      }
                    </div>
                  } @else {
                    <p class="text-sm text-gray-500 dark:text-gray-400">
                      No tools available. Configure tools in the tool catalog first.
                    </p>
                  }
                }
              </div>
            </div>

            <!-- Model Permissions Section -->
            <div class="rounded-sm border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800">
              <h2 class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">
                Model Permissions
              </h2>
              <p class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                Select which AI models users with this role can access.
              </p>

              <div>
                <!-- Grant All Models Option -->
                <div class="mb-4 flex items-center gap-3">
                  <input
                    type="checkbox"
                    id="grantAllModels"
                    [checked]="isSelected('grantedModels', '*')"
                    (change)="toggleWildcard('grantedModels', $event)"
                    class="size-4 rounded-xs border-gray-300 text-amber-600 focus:ring-3 focus:ring-amber-500/50 dark:border-gray-600 dark:bg-gray-700"
                  />
                  <label for="grantAllModels" class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Grant access to all models
                  </label>
                </div>

                @if (!isSelected('grantedModels', '*')) {
                  @if (modelsResource.isLoading()) {
                    <p class="text-sm text-gray-500 dark:text-gray-400">Loading models...</p>
                  } @else if (availableModels().length > 0) {
                    <div class="flex flex-wrap gap-2">
                      @for (model of availableModels(); track model.id) {
                        <button
                          type="button"
                          (click)="toggleArrayValue('grantedModels', model.modelId)"
                          [class.bg-amber-600]="isSelected('grantedModels', model.modelId)"
                          [class.text-white]="isSelected('grantedModels', model.modelId)"
                          [class.bg-gray-100]="!isSelected('grantedModels', model.modelId)"
                          [class.text-gray-700]="!isSelected('grantedModels', model.modelId)"
                          [class.dark:bg-amber-500]="isSelected('grantedModels', model.modelId)"
                          [class.dark:bg-gray-700]="!isSelected('grantedModels', model.modelId)"
                          [class.dark:text-gray-300]="!isSelected('grantedModels', model.modelId)"
                          class="rounded-sm px-3 py-1.5 text-sm/6 font-medium hover:opacity-80 focus:outline-hidden focus:ring-3 focus:ring-amber-500/50"
                          [title]="model.modelId"
                        >
                          {{ model.modelName }}
                        </button>
                      }
                    </div>
                  } @else {
                    <p class="text-sm text-gray-500 dark:text-gray-400">
                      No models available. Add models in Manage Models first.
                    </p>
                  }
                }
              </div>
            </div>

            <!-- Form Actions -->
            <div class="flex gap-3 border-t border-gray-200 pt-6 dark:border-gray-700">
              <button
                type="submit"
                [disabled]="isSubmitting() || roleForm.invalid"
                class="rounded-sm bg-blue-600 px-6 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 disabled:opacity-50 disabled:cursor-not-allowed dark:bg-blue-500 dark:hover:bg-blue-600"
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
                class="rounded-sm border border-gray-300 bg-white px-6 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus:outline-hidden focus:ring-3 focus:ring-gray-500/50 disabled:opacity-50 disabled:cursor-not-allowed dark:border-gray-600 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
              >
                Cancel
              </button>
            </div>
          </form>
        }
      </div>
    </div>
  `,
})
export class RoleFormPage implements OnInit {
  private fb = inject(FormBuilder);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private appRolesService = inject(AppRolesService);
  private toolsService = inject(ToolsService);
  private managedModelsService = inject(ManagedModelsService);

  // Resources
  readonly toolsResource = this.toolsService.catalogResource;
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
    priority: this.fb.control(0, {
      nonNullable: true,
      validators: [Validators.min(0), Validators.max(999)],
    }),
    enabled: this.fb.control(true, { nonNullable: true }),
  });

  readonly pageTitle = computed(() =>
    this.isEditMode() ? 'Edit Role' : 'Create Role'
  );

  readonly availableTools = computed(() => this.toolsService.getTools());

  readonly availableModels = computed(() =>
    this.managedModelsService.getManagedModels()
  );

  readonly availableParentRoles = computed(() => {
    const currentRoleId = this.roleId();
    return this.appRolesService
      .getEnabledRoles()
      .filter(r => r.roleId !== currentRoleId);
  });

  ngOnInit(): void {
    const id = this.route.snapshot.paramMap.get('id');
    if (id && id !== 'new') {
      this.isEditMode.set(true);
      this.roleId.set(id);
      this.loadRoleData(id);
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

  toggleArrayValue(
    controlName: 'inheritsFrom' | 'grantedTools' | 'grantedModels',
    value: string
  ): void {
    const control = this.roleForm.get(controlName) as FormControl<string[]>;
    const currentValue = control.value || [];

    if (currentValue.includes(value)) {
      control.setValue(currentValue.filter(v => v !== value));
    } else {
      control.setValue([...currentValue, value]);
    }
  }

  isSelected(
    controlName: 'inheritsFrom' | 'grantedTools' | 'grantedModels',
    value: string
  ): boolean {
    const control = this.roleForm.get(controlName) as FormControl<string[]>;
    return control.value?.includes(value) ?? false;
  }

  toggleWildcard(
    controlName: 'grantedTools' | 'grantedModels',
    event: Event
  ): void {
    const checked = (event.target as HTMLInputElement).checked;
    const control = this.roleForm.get(controlName) as FormControl<string[]>;

    if (checked) {
      control.setValue(['*']);
    } else {
      control.setValue([]);
    }
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
