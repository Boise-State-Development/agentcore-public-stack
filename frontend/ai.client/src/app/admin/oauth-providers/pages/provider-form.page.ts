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
  heroEye,
  heroEyeSlash,
  heroExclamationTriangle,
  heroCheckCircle,
} from '@ng-icons/heroicons/outline';
import { OAuthProvidersService } from '../services/oauth-providers.service';
import { AppRolesService } from '../../roles/services/app-roles.service';
import {
  OAuthProviderCreateRequest,
  OAuthProviderUpdateRequest,
  OAuthProviderType,
  OAUTH_PROVIDER_PRESETS,
  getProviderPreset,
} from '../models/oauth-provider.model';
import { TooltipDirective } from '../../../components/tooltip/tooltip.directive';

interface ProviderFormGroup {
  providerId: FormControl<string>;
  displayName: FormControl<string>;
  providerType: FormControl<OAuthProviderType>;
  authorizationEndpoint: FormControl<string>;
  tokenEndpoint: FormControl<string>;
  clientId: FormControl<string>;
  clientSecret: FormControl<string>;
  scopes: FormControl<string>;
  allowedRoles: FormControl<string[]>;
  grantAllRoles: FormControl<boolean>;
  enabled: FormControl<boolean>;
  iconName: FormControl<string>;
}

@Component({
  selector: 'app-provider-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReactiveFormsModule, NgIcon, TooltipDirective],
  providers: [
    provideIcons({
      heroArrowLeft,
      heroInformationCircle,
      heroEye,
      heroEyeSlash,
      heroExclamationTriangle,
      heroCheckCircle,
    }),
  ],
  host: {
    class: 'block',
  },
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-3xl px-4 py-8 sm:px-6 lg:px-8">
        <!-- Back Button -->
        <button
          type="button"
          (click)="goBack()"
          class="mb-6 inline-flex items-center gap-2 text-sm/6 font-medium text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
        >
          <ng-icon name="heroArrowLeft" class="size-4" />
          Back to Providers
        </button>

        <!-- Page Header -->
        <div class="mb-8">
          <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">
            {{ pageTitle() }}
          </h1>
          <p class="mt-2 text-base/7 text-gray-600 dark:text-gray-400">
            {{ isEditMode() ? 'Update OAuth provider settings and credentials' : 'Configure a new OAuth provider for tool authentication' }}
          </p>
        </div>

        <!-- Loading State -->
        @if (loading()) {
          <div class="flex h-64 items-center justify-center">
            <div class="flex flex-col items-center gap-4">
              <div
                class="size-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-600"
              ></div>
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                Loading provider...
              </p>
            </div>
          </div>
        } @else {
          <!-- Form -->
          <form [formGroup]="providerForm" (ngSubmit)="onSubmit()" class="space-y-8">

            <!-- Provider Type Selection (Create only) -->
            @if (!isEditMode()) {
              <div class="rounded-sm border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800">
                <h2 class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">
                  Provider Type
                </h2>
                <p class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                  Select a preset or configure a custom OAuth 2.0 provider.
                </p>

                <div class="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
                  @for (preset of presets; track preset.type) {
                    <button
                      type="button"
                      (click)="selectProviderType(preset.type)"
                      [class.ring-3]="providerForm.controls.providerType.value === preset.type"
                      [class.ring-blue-500]="providerForm.controls.providerType.value === preset.type"
                      [class.border-blue-500]="providerForm.controls.providerType.value === preset.type"
                      class="flex flex-col items-center gap-2 rounded-sm border border-gray-200 bg-white p-4 text-center transition-all hover:border-gray-300 hover:shadow-xs focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:hover:border-gray-500"
                    >
                      <div [class]="getPresetIconClasses(preset.type)">
                        <ng-icon [name]="preset.iconName" class="size-5" />
                      </div>
                      <span class="text-sm/6 font-medium text-gray-900 dark:text-white">
                        {{ preset.displayName }}
                      </span>
                    </button>
                  }
                </div>
              </div>
            }

            <!-- Basic Information Section -->
            <div class="rounded-sm border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800">
              <h2 class="mb-6 text-xl/8 font-semibold text-gray-900 dark:text-white">
                Basic Information
              </h2>

              <div class="space-y-5">
                <!-- Provider ID -->
                <div>
                  <label for="providerId" class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Provider ID <span class="text-red-600">*</span>
                  </label>
                  <input
                    type="text"
                    id="providerId"
                    formControlName="providerId"
                    placeholder="e.g., google-workspace, github-enterprise"
                    [readonly]="isEditMode()"
                    class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2.5 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 read-only:cursor-not-allowed read-only:bg-gray-50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500 dark:read-only:bg-gray-600"
                    [class.border-red-500]="providerForm.controls.providerId.invalid && providerForm.controls.providerId.touched"
                  />
                  <p class="mt-1.5 text-xs/5 text-gray-500 dark:text-gray-400">
                    Unique identifier. Lowercase letters, numbers, and hyphens only.
                  </p>
                  @if (providerForm.controls.providerId.invalid && providerForm.controls.providerId.touched) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">
                      @if (providerForm.controls.providerId.errors?.['required']) {
                        Provider ID is required
                      } @else if (providerForm.controls.providerId.errors?.['pattern']) {
                        Must be lowercase letters, numbers, and hyphens only
                      } @else if (providerForm.controls.providerId.errors?.['maxlength']) {
                        Must be at most 64 characters
                      }
                    </p>
                  }
                </div>

                <!-- Display Name -->
                <div>
                  <label for="displayName" class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Display Name <span class="text-red-600">*</span>
                  </label>
                  <input
                    type="text"
                    id="displayName"
                    formControlName="displayName"
                    placeholder="e.g., Google Workspace, GitHub Enterprise"
                    class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2.5 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                    [class.border-red-500]="providerForm.controls.displayName.invalid && providerForm.controls.displayName.touched"
                  />
                  @if (providerForm.controls.displayName.invalid && providerForm.controls.displayName.touched) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">Display name is required</p>
                  }
                </div>

                <!-- Enabled Toggle -->
                <div class="flex items-center gap-3">
                  <input
                    type="checkbox"
                    id="enabled"
                    formControlName="enabled"
                    class="size-4 rounded-xs border-gray-300 text-blue-600 focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700"
                  />
                  <label for="enabled" class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Provider Enabled
                  </label>
                </div>
              </div>
            </div>

            <!-- OAuth Configuration Section -->
            <div class="rounded-sm border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800">
              <h2 class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">
                OAuth Configuration
              </h2>
              <p class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                Configure the OAuth 2.0 endpoints and credentials.
              </p>

              <div class="space-y-5">
                <!-- Authorization Endpoint -->
                <div>
                  <label for="authorizationEndpoint" class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Authorization Endpoint <span class="text-red-600">*</span>
                  </label>
                  <input
                    type="url"
                    id="authorizationEndpoint"
                    formControlName="authorizationEndpoint"
                    placeholder="https://provider.com/oauth/authorize"
                    class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2.5 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                    [class.border-red-500]="providerForm.controls.authorizationEndpoint.invalid && providerForm.controls.authorizationEndpoint.touched"
                  />
                  @if (providerForm.controls.authorizationEndpoint.invalid && providerForm.controls.authorizationEndpoint.touched) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">
                      @if (providerForm.controls.authorizationEndpoint.errors?.['required']) {
                        Authorization endpoint is required
                      } @else {
                        Must be a valid URL
                      }
                    </p>
                  }
                </div>

                <!-- Token Endpoint -->
                <div>
                  <label for="tokenEndpoint" class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Token Endpoint <span class="text-red-600">*</span>
                  </label>
                  <input
                    type="url"
                    id="tokenEndpoint"
                    formControlName="tokenEndpoint"
                    placeholder="https://provider.com/oauth/token"
                    class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2.5 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                    [class.border-red-500]="providerForm.controls.tokenEndpoint.invalid && providerForm.controls.tokenEndpoint.touched"
                  />
                  @if (providerForm.controls.tokenEndpoint.invalid && providerForm.controls.tokenEndpoint.touched) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">
                      @if (providerForm.controls.tokenEndpoint.errors?.['required']) {
                        Token endpoint is required
                      } @else {
                        Must be a valid URL
                      }
                    </p>
                  }
                </div>

                <!-- Client ID -->
                <div>
                  <label for="clientId" class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Client ID <span class="text-red-600">*</span>
                  </label>
                  <input
                    type="text"
                    id="clientId"
                    formControlName="clientId"
                    placeholder="Your OAuth client ID"
                    class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2.5 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                    [class.border-red-500]="providerForm.controls.clientId.invalid && providerForm.controls.clientId.touched"
                  />
                  @if (providerForm.controls.clientId.invalid && providerForm.controls.clientId.touched) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">Client ID is required</p>
                  }
                </div>

                <!-- Client Secret -->
                <div>
                  <label for="clientSecret" class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Client Secret
                    @if (!isEditMode()) {
                      <span class="text-red-600">*</span>
                    }
                  </label>
                  <div class="relative">
                    <input
                      [type]="showClientSecret() ? 'text' : 'password'"
                      id="clientSecret"
                      formControlName="clientSecret"
                      autocomplete="off"
                      [placeholder]="isEditMode() ? 'Leave blank to keep existing secret' : 'Your OAuth client secret'"
                      class="block w-full rounded-sm border border-gray-300 bg-white py-2.5 pl-3 pr-10 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                      [class.border-red-500]="providerForm.controls.clientSecret.invalid && providerForm.controls.clientSecret.touched"
                    />
                    <button
                      type="button"
                      (click)="showClientSecret.set(!showClientSecret())"
                      class="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                      [appTooltip]="showClientSecret() ? 'Hide secret' : 'Show secret'"
                      appTooltipPosition="top"
                    >
                      <ng-icon [name]="showClientSecret() ? 'heroEyeSlash' : 'heroEye'" class="size-5" />
                    </button>
                  </div>
                  @if (isEditMode()) {
                    <p class="mt-1.5 text-xs/5 text-gray-500 dark:text-gray-400">
                      Leave blank to keep the existing secret. Enter a new value to update it.
                    </p>
                  }
                  @if (!isEditMode() && providerForm.controls.clientSecret.invalid && providerForm.controls.clientSecret.touched) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">Client secret is required</p>
                  }
                </div>

                <!-- Scopes -->
                <div>
                  <label for="scopes" class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Scopes
                  </label>
                  <input
                    type="text"
                    id="scopes"
                    formControlName="scopes"
                    placeholder="openid, email, profile"
                    class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2.5 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                  <p class="mt-1.5 text-xs/5 text-gray-500 dark:text-gray-400">
                    Comma-separated list of OAuth scopes to request during authorization.
                  </p>
                </div>
              </div>
            </div>

            <!-- Access Control Section -->
            <div class="rounded-sm border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800">
              <h2 class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">
                Access Control
              </h2>
              <p class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                Restrict which application roles can use this provider.
              </p>

              <div>
                <label class="mb-2 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                  Allowed Roles
                </label>

                <!-- Grant All Roles Option -->
                <div class="mb-4 flex items-center gap-3">
                  <input
                    type="checkbox"
                    id="grantAllRoles"
                    formControlName="grantAllRoles"
                    (change)="onGrantAllRolesChange()"
                    class="size-4 rounded-xs border-gray-300 text-purple-600 focus:ring-3 focus:ring-purple-500/50 dark:border-gray-600 dark:bg-gray-700"
                  />
                  <label for="grantAllRoles" class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Allow all roles (unrestricted access)
                  </label>
                </div>

                @if (!providerForm.controls.grantAllRoles.value) {
                  @if (rolesResource.isLoading() || rolesResource.value() === undefined) {
                    <div class="flex items-center gap-2">
                      <div class="size-4 animate-spin rounded-full border-2 border-gray-300 border-t-purple-600"></div>
                      <p class="text-sm/6 text-gray-500 dark:text-gray-400">Loading roles...</p>
                    </div>
                  } @else if (availableRoles().length > 0) {
                    <div class="flex flex-wrap gap-2">
                      @for (role of availableRoles(); track role.roleId) {
                        <button
                          type="button"
                          (click)="toggleRole(role.roleId)"
                          [class.bg-purple-600]="isRoleSelected(role.roleId)"
                          [class.text-white]="isRoleSelected(role.roleId)"
                          [class.bg-gray-100]="!isRoleSelected(role.roleId)"
                          [class.text-gray-700]="!isRoleSelected(role.roleId)"
                          [class.dark:bg-purple-500]="isRoleSelected(role.roleId)"
                          [class.dark:bg-gray-700]="!isRoleSelected(role.roleId)"
                          [class.dark:text-gray-300]="!isRoleSelected(role.roleId)"
                          class="rounded-sm px-3 py-1.5 text-sm/6 font-medium transition-colors hover:opacity-80 focus:outline-hidden focus:ring-3 focus:ring-purple-500/50"
                          [appTooltip]="role.description || 'No description'"
                          appTooltipPosition="top"
                        >
                          {{ role.displayName }}
                        </button>
                      }
                    </div>
                  } @else {
                    <p class="text-sm/6 text-gray-500 dark:text-gray-400">
                      No roles available. Create roles in Role Management first.
                    </p>
                  }
                }
                <p class="mt-2 text-xs/5 text-gray-500 dark:text-gray-400">
                  Only users with selected roles will be able to connect to this provider.
                </p>
              </div>
            </div>

            <!-- Security Warning for Edit -->
            @if (isEditMode()) {
              <div class="rounded-sm border border-amber-200 bg-amber-50 p-4 dark:border-amber-800 dark:bg-amber-900/20">
                <div class="flex gap-3">
                  <ng-icon name="heroExclamationTriangle" class="size-5 shrink-0 text-amber-600 dark:text-amber-400" />
                  <div>
                    <h3 class="text-sm/6 font-medium text-amber-800 dark:text-amber-200">
                      Security Notice
                    </h3>
                    <p class="mt-1 text-sm/6 text-amber-700 dark:text-amber-300">
                      Changing scopes may invalidate existing user tokens. Users may need to re-authenticate after scope changes.
                    </p>
                  </div>
                </div>
              </div>
            }

            <!-- Form Actions -->
            <div class="flex gap-3 border-t border-gray-200 pt-6 dark:border-gray-700">
              <button
                type="submit"
                [disabled]="isSubmitting() || providerForm.invalid"
                class="inline-flex items-center gap-2 rounded-sm bg-blue-600 px-6 py-2.5 text-sm/6 font-semibold text-white shadow-xs hover:bg-blue-700 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
              >
                @if (isSubmitting()) {
                  <div class="size-4 animate-spin rounded-full border-2 border-white/30 border-t-white"></div>
                  Saving...
                } @else {
                  <ng-icon name="heroCheckCircle" class="size-5" />
                  {{ isEditMode() ? 'Update Provider' : 'Create Provider' }}
                }
              </button>
              <button
                type="button"
                (click)="goBack()"
                [disabled]="isSubmitting()"
                class="rounded-sm border border-gray-300 bg-white px-6 py-2.5 text-sm/6 font-semibold text-gray-700 hover:bg-gray-50 focus:outline-hidden focus:ring-3 focus:ring-gray-500/50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
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
export class ProviderFormPage implements OnInit {
  private fb = inject(FormBuilder);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private oauthProvidersService = inject(OAuthProvidersService);
  private appRolesService = inject(AppRolesService);

  // Resources
  readonly rolesResource = this.appRolesService.rolesResource;

  // Presets
  readonly presets = OAUTH_PROVIDER_PRESETS;

  // State
  readonly isEditMode = signal(false);
  readonly providerId = signal<string | null>(null);
  readonly isSubmitting = signal(false);
  readonly loading = signal(false);
  readonly showClientSecret = signal(false);

  // Form
  readonly providerForm: FormGroup<ProviderFormGroup> = this.fb.group({
    providerId: this.fb.control('', {
      nonNullable: true,
      validators: [
        Validators.required,
        Validators.minLength(1),
        Validators.maxLength(64),
        Validators.pattern(/^[a-z0-9-]+$/),
      ],
    }),
    displayName: this.fb.control('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(100)],
    }),
    providerType: this.fb.control<OAuthProviderType>('custom', { nonNullable: true }),
    authorizationEndpoint: this.fb.control('', {
      nonNullable: true,
      validators: [Validators.required, Validators.pattern(/^https?:\/\/.+/)],
    }),
    tokenEndpoint: this.fb.control('', {
      nonNullable: true,
      validators: [Validators.required, Validators.pattern(/^https?:\/\/.+/)],
    }),
    clientId: this.fb.control('', {
      nonNullable: true,
      validators: [Validators.required],
    }),
    clientSecret: this.fb.control('', { nonNullable: true }),
    scopes: this.fb.control('', { nonNullable: true }),
    allowedRoles: this.fb.control<string[]>(['*'], { nonNullable: true }),
    grantAllRoles: this.fb.control(true, { nonNullable: true }),
    enabled: this.fb.control(true, { nonNullable: true }),
    iconName: this.fb.control('heroLink', { nonNullable: true }),
  });

  readonly pageTitle = computed(() =>
    this.isEditMode() ? 'Edit OAuth Provider' : 'Add OAuth Provider'
  );

  readonly availableRoles = computed(() =>
    this.appRolesService.getEnabledRoles()
  );

  // Track selected roles as a signal for change detection with OnPush
  readonly selectedRoles = signal<string[]>(['*']);

  ngOnInit(): void {
    const id = this.route.snapshot.paramMap.get('providerId');
    if (id && id !== 'new') {
      this.isEditMode.set(true);
      this.providerId.set(id);
      this.loadProviderData(id);
    } else {
      // Set client secret as required for new providers
      this.providerForm.controls.clientSecret.setValidators([Validators.required]);
      this.providerForm.controls.clientSecret.updateValueAndValidity();
    }
  }

  private async loadProviderData(id: string): Promise<void> {
    this.loading.set(true);
    try {
      const provider = await this.oauthProvidersService.fetchProvider(id);
      this.providerForm.patchValue({
        providerId: provider.providerId,
        displayName: provider.displayName,
        providerType: provider.providerType,
        authorizationEndpoint: provider.authorizationEndpoint,
        tokenEndpoint: provider.tokenEndpoint,
        clientId: provider.clientId,
        clientSecret: '', // Never returned from API
        scopes: provider.scopes.join(', '),
        allowedRoles: provider.allowedRoles.length > 0 ? provider.allowedRoles : ['*'],
        grantAllRoles: provider.allowedRoles.length === 0,
        enabled: provider.enabled,
        iconName: provider.iconName || 'heroLink',
      });
      // Sync selectedRoles signal with loaded data
      this.selectedRoles.set(provider.allowedRoles.length > 0 ? provider.allowedRoles : ['*']);
    } catch (error) {
      console.error('Error loading provider:', error);
      alert('Failed to load provider. Returning to list.');
      this.router.navigate(['/admin/oauth-providers']);
    } finally {
      this.loading.set(false);
    }
  }

  selectProviderType(type: OAuthProviderType): void {
    const preset = getProviderPreset(type);
    if (preset) {
      this.providerForm.patchValue({
        providerType: type,
        displayName: preset.displayName,
        authorizationEndpoint: preset.authorizationEndpoint,
        tokenEndpoint: preset.tokenEndpoint,
        scopes: preset.defaultScopes.join(', '),
        iconName: preset.iconName,
      });
    }
  }

  getPresetIconClasses(type: OAuthProviderType): string {
    const baseClasses = 'flex size-10 items-center justify-center rounded-sm';
    switch (type) {
      case 'google':
        return `${baseClasses} bg-red-100 text-red-600 dark:bg-red-900/30 dark:text-red-400`;
      case 'microsoft':
        return `${baseClasses} bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400`;
      case 'github':
        return `${baseClasses} bg-gray-800 text-white dark:bg-gray-600`;
      case 'canvas':
        return `${baseClasses} bg-orange-100 text-orange-600 dark:bg-orange-900/30 dark:text-orange-400`;
      default:
        return `${baseClasses} bg-purple-100 text-purple-600 dark:bg-purple-900/30 dark:text-purple-400`;
    }
  }

  onGrantAllRolesChange(): void {
    const checked = this.providerForm.controls.grantAllRoles.value;
    if (checked) {
      this.providerForm.controls.allowedRoles.setValue(['*']);
      this.selectedRoles.set(['*']);
    } else {
      this.providerForm.controls.allowedRoles.setValue([]);
      this.selectedRoles.set([]);
      // Trigger roles load if not already loaded
      if (this.rolesResource.value() === undefined) {
        this.rolesResource.reload();
      }
    }
  }

  isRoleSelected(roleId: string): boolean {
    return this.selectedRoles().includes(roleId);
  }

  toggleRole(roleId: string): void {
    const currentRoles = this.selectedRoles().filter(r => r !== '*');
    let newRoles: string[];
    if (currentRoles.includes(roleId)) {
      newRoles = currentRoles.filter(r => r !== roleId);
    } else {
      newRoles = [...currentRoles, roleId];
    }
    this.providerForm.controls.allowedRoles.setValue(newRoles);
    this.selectedRoles.set(newRoles);
  }

  async onSubmit(): Promise<void> {
    if (this.providerForm.invalid) {
      this.providerForm.markAllAsTouched();
      return;
    }

    this.isSubmitting.set(true);

    try {
      const formValue = this.providerForm.value;

      // Parse scopes from comma-separated string
      const scopes = formValue.scopes
        ? formValue.scopes
            .split(',')
            .map((s: string) => s.trim())
            .filter((s: string) => s.length > 0)
        : [];

      // Normalize allowed roles
      const allowedRoles = formValue.grantAllRoles
        ? []
        : (formValue.allowedRoles || []).filter((r: string) => r !== '*');

      if (this.isEditMode() && this.providerId()) {
        const updates: OAuthProviderUpdateRequest = {
          displayName: formValue.displayName,
          authorizationEndpoint: formValue.authorizationEndpoint,
          tokenEndpoint: formValue.tokenEndpoint,
          clientId: formValue.clientId,
          scopes,
          allowedRoles,
          enabled: formValue.enabled,
          iconName: formValue.iconName,
        };
        // Only include client secret if provided
        if (formValue.clientSecret) {
          updates.clientSecret = formValue.clientSecret;
        }
        await this.oauthProvidersService.updateProvider(this.providerId()!, updates);
      } else {
        const createData: OAuthProviderCreateRequest = {
          providerId: formValue.providerId!,
          displayName: formValue.displayName!,
          providerType: formValue.providerType!,
          authorizationEndpoint: formValue.authorizationEndpoint!,
          tokenEndpoint: formValue.tokenEndpoint!,
          clientId: formValue.clientId!,
          clientSecret: formValue.clientSecret!,
          scopes,
          allowedRoles,
          enabled: formValue.enabled,
          iconName: formValue.iconName,
        };
        await this.oauthProvidersService.createProvider(createData);
      }

      this.router.navigate(['/admin/oauth-providers']);
    } catch (error: any) {
      console.error('Error saving provider:', error);
      const message =
        error?.error?.detail || error?.message || 'Failed to save provider.';
      alert(message);
    } finally {
      this.isSubmitting.set(false);
    }
  }

  goBack(): void {
    this.router.navigate(['/admin/oauth-providers']);
  }
}
