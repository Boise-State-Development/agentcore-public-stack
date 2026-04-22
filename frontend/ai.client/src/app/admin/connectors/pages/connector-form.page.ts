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
  heroClipboard,
  heroClipboardDocumentCheck,
} from '@ng-icons/heroicons/outline';
import { ConnectorsService } from '../services/connectors.service';
import { AppRolesService } from '../../roles/services/app-roles.service';
import {
  Connector,
  ConnectorCreateRequest,
  ConnectorUpdateRequest,
  ConnectorType,
  CONNECTOR_PRESETS,
  getConnectorPreset,
  requiresDiscovery,
} from '../models/connector.model';
import { TooltipDirective } from '../../../components/tooltip/tooltip.directive';

interface ConnectorFormGroup {
  providerId: FormControl<string>;
  displayName: FormControl<string>;
  providerType: FormControl<ConnectorType>;
  clientId: FormControl<string>;
  clientSecret: FormControl<string>;
  oauthDiscoveryUrl: FormControl<string>;
  scopes: FormControl<string>;
  allowedRoles: FormControl<string[]>;
  grantAllRoles: FormControl<boolean>;
  enabled: FormControl<boolean>;
  iconName: FormControl<string>;
}

@Component({
  selector: 'app-connector-form',
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
      heroClipboard,
      heroClipboardDocumentCheck,
    }),
  ],
  host: { class: 'block' },
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-3xl px-4 py-8 sm:px-6 lg:px-8">
        <button
          type="button"
          (click)="goBack()"
          class="mb-6 inline-flex items-center gap-2 text-sm/6 font-medium text-gray-600 hover:text-gray-900 dark:text-gray-400 dark:hover:text-white"
        >
          <ng-icon name="heroArrowLeft" class="size-4" />
          Back to Connectors
        </button>

        <div class="mb-8">
          <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">
            {{ pageTitle() }}
          </h1>
          <p class="mt-2 text-base/7 text-gray-600 dark:text-gray-400">
            {{ isEditMode() ? 'Update connector settings and credentials' : 'Register a new OAuth connector' }}
          </p>
        </div>

        @if (loading()) {
          <div class="flex h-64 items-center justify-center">
            <div class="flex flex-col items-center gap-4">
              <div class="size-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-gray-600"></div>
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">Loading connector...</p>
            </div>
          </div>
        } @else if (createdConnector(); as created) {
          <!-- Success screen after Create: show callback URL for vendor console -->
          <div class="space-y-6">
            <div class="rounded-sm border border-emerald-200 bg-emerald-50 p-4 dark:border-emerald-800 dark:bg-emerald-900/20">
              <div class="flex gap-3">
                <ng-icon name="heroCheckCircle" class="size-5 shrink-0 text-emerald-600 dark:text-emerald-400" />
                <div>
                  <h3 class="text-sm/6 font-medium text-emerald-800 dark:text-emerald-200">
                    Connector created
                  </h3>
                  <p class="mt-1 text-sm/6 text-emerald-700 dark:text-emerald-300">
                    "{{ created.displayName }}" is registered with AgentCore Identity.
                  </p>
                </div>
              </div>
            </div>

            <div class="rounded-sm border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800">
              <h2 class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">
                Next step: register this callback URL
              </h2>
              <p class="mb-4 text-sm/6 text-gray-600 dark:text-gray-400">
                Add the following URL to your OAuth provider's list of authorized redirect URIs
                (in Google Cloud Console, Microsoft Entra, GitHub OAuth App settings, etc.).
                Until this is done, users will see an error when they try to consent.
              </p>
              <div class="flex items-stretch gap-2">
                <input
                  type="text"
                  [value]="created.callbackUrl || ''"
                  readonly
                  class="block w-full rounded-sm border border-gray-300 bg-gray-50 px-3 py-2.5 font-mono text-sm/6 text-gray-900 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
                />
                <button
                  type="button"
                  (click)="copyCallbackUrl(created.callbackUrl || '')"
                  class="inline-flex items-center gap-2 rounded-sm border border-gray-300 bg-white px-3 py-2.5 text-sm/6 font-semibold text-gray-700 hover:bg-gray-50 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600"
                  [appTooltip]="callbackCopied() ? 'Copied!' : 'Copy to clipboard'"
                >
                  <ng-icon [name]="callbackCopied() ? 'heroClipboardDocumentCheck' : 'heroClipboard'" class="size-5" />
                </button>
              </div>
            </div>

            <div class="flex gap-3">
              <button
                type="button"
                (click)="goBack()"
                class="inline-flex items-center gap-2 rounded-sm bg-blue-600 px-6 py-2.5 text-sm/6 font-semibold text-white shadow-xs hover:bg-blue-700 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:bg-blue-500 dark:hover:bg-blue-600"
              >
                Done
              </button>
            </div>
          </div>
        } @else {
          <form [formGroup]="connectorForm" (ngSubmit)="onSubmit()" class="space-y-8">

            @if (!isEditMode()) {
              <div class="rounded-sm border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800">
                <h2 class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">Connector Type</h2>
                <p class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                  Choose a preset or use Custom for any OIDC-compliant provider.
                </p>
                <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
                  @for (preset of presets; track preset.type) {
                    <button
                      type="button"
                      (click)="selectConnectorType(preset.type)"
                      [class.ring-3]="connectorForm.controls.providerType.value === preset.type"
                      [class.ring-blue-500]="connectorForm.controls.providerType.value === preset.type"
                      [class.border-blue-500]="connectorForm.controls.providerType.value === preset.type"
                      class="flex flex-col items-center gap-2 rounded-sm border border-gray-200 bg-white p-4 text-center transition-all hover:border-gray-300 hover:shadow-xs focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:hover:border-gray-500"
                    >
                      <div [class]="getPresetIconClasses(preset.type)">
                        <ng-icon [name]="preset.iconName" class="size-5" />
                      </div>
                      <span class="text-sm/6 font-medium text-gray-900 dark:text-white">{{ preset.displayName }}</span>
                      @if (preset.hint) {
                        <span class="text-xs/5 text-gray-500 dark:text-gray-400">{{ preset.hint }}</span>
                      }
                    </button>
                  }
                </div>
              </div>
            }

            <div class="rounded-sm border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800">
              <h2 class="mb-6 text-xl/8 font-semibold text-gray-900 dark:text-white">Basic Information</h2>
              <div class="space-y-5">
                <div>
                  <label for="providerId" class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Connector ID <span class="text-red-600">*</span>
                  </label>
                  <input
                    type="text"
                    id="providerId"
                    formControlName="providerId"
                    placeholder="e.g., google-workspace, github-enterprise"
                    [readonly]="isEditMode()"
                    class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2.5 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 read-only:cursor-not-allowed read-only:bg-gray-50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500 dark:read-only:bg-gray-600"
                    [class.border-red-500]="connectorForm.controls.providerId.invalid && connectorForm.controls.providerId.touched"
                  />
                  <p class="mt-1.5 text-xs/5 text-gray-500 dark:text-gray-400">
                    Unique identifier. Lowercase letters, numbers, and hyphens only.
                  </p>
                  @if (connectorForm.controls.providerId.invalid && connectorForm.controls.providerId.touched) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">
                      @if (connectorForm.controls.providerId.errors?.['required']) { Connector ID is required }
                      @else if (connectorForm.controls.providerId.errors?.['pattern']) { Must be lowercase letters, numbers, and hyphens only }
                      @else if (connectorForm.controls.providerId.errors?.['maxlength']) { Must be at most 64 characters }
                    </p>
                  }
                </div>

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
                    [class.border-red-500]="connectorForm.controls.displayName.invalid && connectorForm.controls.displayName.touched"
                  />
                  @if (connectorForm.controls.displayName.invalid && connectorForm.controls.displayName.touched) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">Display name is required</p>
                  }
                </div>

                <div class="flex items-center gap-3">
                  <input
                    type="checkbox"
                    id="enabled"
                    formControlName="enabled"
                    class="size-4 rounded-xs border-gray-300 text-blue-600 focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700"
                  />
                  <label for="enabled" class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">Connector Enabled</label>
                </div>
              </div>
            </div>

            <!-- Edit mode: existing AgentCore metadata read-only -->
            @if (isEditMode() && loadedConnector(); as loaded) {
              <div class="rounded-sm border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800">
                <h2 class="mb-6 text-xl/8 font-semibold text-gray-900 dark:text-white">AgentCore Identity</h2>
                <div class="space-y-4">
                  <div>
                    <label class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Callback URL</label>
                    <div class="flex items-stretch gap-2">
                      <input
                        type="text"
                        [value]="loaded.callbackUrl || ''"
                        readonly
                        class="block w-full rounded-sm border border-gray-300 bg-gray-50 px-3 py-2.5 font-mono text-sm/6 text-gray-900 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
                      />
                      <button
                        type="button"
                        (click)="copyCallbackUrl(loaded.callbackUrl || '')"
                        class="inline-flex items-center gap-2 rounded-sm border border-gray-300 bg-white px-3 py-2.5 text-sm/6 font-semibold text-gray-700 hover:bg-gray-50 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600"
                        [appTooltip]="callbackCopied() ? 'Copied!' : 'Copy to clipboard'"
                      >
                        <ng-icon [name]="callbackCopied() ? 'heroClipboardDocumentCheck' : 'heroClipboard'" class="size-5" />
                      </button>
                    </div>
                  </div>
                  @if (loaded.credentialProviderArn) {
                    <div>
                      <label class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Credential Provider ARN</label>
                      <input
                        type="text"
                        [value]="loaded.credentialProviderArn"
                        readonly
                        class="block w-full rounded-sm border border-gray-300 bg-gray-50 px-3 py-2.5 font-mono text-xs/5 text-gray-900 dark:border-gray-600 dark:bg-gray-900 dark:text-white"
                      />
                    </div>
                  }
                </div>
              </div>
            }

            <div class="rounded-sm border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800">
              <h2 class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">OAuth Credentials</h2>
              <p class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                @if (isEditMode()) {
                  Enter both fields to rotate credentials. Leave both blank to keep existing.
                } @else {
                  Credentials are stored by AWS Bedrock AgentCore Identity — never by this application.
                }
              </p>

              <div class="space-y-5">
                <div>
                  <label for="clientId" class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Client ID
                    @if (!isEditMode()) { <span class="text-red-600">*</span> }
                  </label>
                  <input
                    type="text"
                    id="clientId"
                    formControlName="clientId"
                    autocomplete="off"
                    placeholder="Your OAuth client ID"
                    class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2.5 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                    [class.border-red-500]="connectorForm.controls.clientId.invalid && connectorForm.controls.clientId.touched"
                  />
                </div>

                <div>
                  <label for="clientSecret" class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Client Secret
                    @if (!isEditMode()) { <span class="text-red-600">*</span> }
                  </label>
                  <div class="relative">
                    <input
                      [type]="showClientSecret() ? 'text' : 'password'"
                      id="clientSecret"
                      formControlName="clientSecret"
                      autocomplete="off"
                      [placeholder]="isEditMode() ? 'Leave blank to keep existing' : 'Your OAuth client secret'"
                      class="block w-full rounded-sm border border-gray-300 bg-white py-2.5 pl-3 pr-10 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                    />
                    <button
                      type="button"
                      (click)="showClientSecret.set(!showClientSecret())"
                      class="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-600 dark:hover:text-gray-300"
                      [appTooltip]="showClientSecret() ? 'Hide secret' : 'Show secret'"
                    >
                      <ng-icon [name]="showClientSecret() ? 'heroEyeSlash' : 'heroEye'" class="size-5" />
                    </button>
                  </div>
                </div>

                @if (credentialPairError()) {
                  <p class="text-sm/6 text-red-600 dark:text-red-400">{{ credentialPairError() }}</p>
                }

                @if (needsDiscovery()) {
                  <div>
                    <label for="oauthDiscoveryUrl" class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                      OIDC Discovery URL <span class="text-red-600">*</span>
                    </label>
                    <input
                      type="url"
                      id="oauthDiscoveryUrl"
                      formControlName="oauthDiscoveryUrl"
                      placeholder="https://example.com/.well-known/openid-configuration"
                      class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2.5 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                      [class.border-red-500]="connectorForm.controls.oauthDiscoveryUrl.invalid && connectorForm.controls.oauthDiscoveryUrl.touched"
                    />
                    <p class="mt-1.5 text-xs/5 text-gray-500 dark:text-gray-400">
                      AgentCore fetches this URL to resolve authorization and token endpoints.
                    </p>
                  </div>
                }

                <div>
                  <label for="scopes" class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Scopes</label>
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

            <div class="rounded-sm border border-gray-200 bg-white p-6 dark:border-gray-700 dark:bg-gray-800">
              <h2 class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">Access Control</h2>
              <p class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                Restrict which application roles can use this connector.
              </p>
              <div>
                <label class="mb-2 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Allowed Roles</label>
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

                @if (!connectorForm.controls.grantAllRoles.value) {
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
                  Only users with selected roles will be able to use this connector.
                </p>
              </div>
            </div>

            @if (isEditMode()) {
              <div class="rounded-sm border border-amber-200 bg-amber-50 p-4 dark:border-amber-800 dark:bg-amber-900/20">
                <div class="flex gap-3">
                  <ng-icon name="heroExclamationTriangle" class="size-5 shrink-0 text-amber-600 dark:text-amber-400" />
                  <div>
                    <h3 class="text-sm/6 font-medium text-amber-800 dark:text-amber-200">Security Notice</h3>
                    <p class="mt-1 text-sm/6 text-amber-700 dark:text-amber-300">
                      Changing scopes forces connected users to re-consent on their next tool call.
                      Rotating credentials requires re-entering both Client ID and Client Secret.
                    </p>
                  </div>
                </div>
              </div>
            }

            <div class="flex gap-3 border-t border-gray-200 pt-6 dark:border-gray-700">
              <button
                type="submit"
                [disabled]="isSubmitting() || connectorForm.invalid || !!credentialPairError()"
                class="inline-flex items-center gap-2 rounded-sm bg-blue-600 px-6 py-2.5 text-sm/6 font-semibold text-white shadow-xs hover:bg-blue-700 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
              >
                @if (isSubmitting()) {
                  <div class="size-4 animate-spin rounded-full border-2 border-white/30 border-t-white"></div>
                  Saving...
                } @else {
                  <ng-icon name="heroCheckCircle" class="size-5" />
                  {{ isEditMode() ? 'Update Connector' : 'Create Connector' }}
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
export class ConnectorFormPage implements OnInit {
  private fb = inject(FormBuilder);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private connectorsService = inject(ConnectorsService);
  private appRolesService = inject(AppRolesService);

  readonly rolesResource = this.appRolesService.rolesResource;

  readonly presets = CONNECTOR_PRESETS;

  readonly isEditMode = signal(false);
  readonly providerId = signal<string | null>(null);
  readonly isSubmitting = signal(false);
  readonly loading = signal(false);
  readonly showClientSecret = signal(false);
  readonly loadedConnector = signal<Connector | null>(null);
  readonly createdConnector = signal<Connector | null>(null);
  readonly callbackCopied = signal(false);

  readonly connectorForm: FormGroup<ConnectorFormGroup> = this.fb.group({
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
    providerType: this.fb.control<ConnectorType>('custom', { nonNullable: true }),
    clientId: this.fb.control('', { nonNullable: true }),
    clientSecret: this.fb.control('', { nonNullable: true }),
    oauthDiscoveryUrl: this.fb.control('', { nonNullable: true }),
    scopes: this.fb.control('', { nonNullable: true }),
    allowedRoles: this.fb.control<string[]>(['*'], { nonNullable: true }),
    grantAllRoles: this.fb.control(true, { nonNullable: true }),
    enabled: this.fb.control(true, { nonNullable: true }),
    iconName: this.fb.control('heroLink', { nonNullable: true }),
  });

  readonly pageTitle = computed(() => (this.isEditMode() ? 'Edit Connector' : 'Add Connector'));

  readonly availableRoles = computed(() => this.appRolesService.getEnabledRoles());

  readonly selectedRoles = signal<string[]>(['*']);

  // Form controls aren't observable signals, so mirror providerType into a
  // signal updated from valueChanges. This drives both the template's @if
  // and the submit-time discovery gating.
  readonly needsDiscovery = signal(
    requiresDiscovery(this.connectorForm.controls.providerType.value)
  );

  /**
   * Returns a user-facing error string when clientId and clientSecret are
   * inconsistent. Rotation requires both or neither.
   */
  readonly credentialPairError = computed(() => {
    const id = this.connectorForm.controls.clientId.value.trim();
    const secret = this.connectorForm.controls.clientSecret.value.trim();
    if (!this.isEditMode()) return null; // create mode requires both, enforced elsewhere
    if (!!id === !!secret) return null;
    return 'Client ID and Client Secret must be provided together to rotate credentials.';
  });

  ngOnInit(): void {
    const id = this.route.snapshot.paramMap.get('providerId');
    if (id && id !== 'new') {
      this.isEditMode.set(true);
      this.providerId.set(id);
      this.loadConnectorData(id);
    } else {
      this.connectorForm.controls.clientId.setValidators([Validators.required]);
      this.connectorForm.controls.clientSecret.setValidators([Validators.required]);
      this.connectorForm.controls.clientId.updateValueAndValidity();
      this.connectorForm.controls.clientSecret.updateValueAndValidity();
      this.applyDiscoveryValidator();
    }

    this.connectorForm.controls.providerType.valueChanges.subscribe(() =>
      this.applyDiscoveryValidator()
    );
  }

  private applyDiscoveryValidator(): void {
    const ctrl = this.connectorForm.controls.oauthDiscoveryUrl;
    const needs = requiresDiscovery(this.connectorForm.controls.providerType.value);
    this.needsDiscovery.set(needs);
    if (needs) {
      ctrl.setValidators([Validators.required, Validators.pattern(/^https?:\/\/.+/)]);
    } else {
      ctrl.clearValidators();
      ctrl.setValue('');
    }
    ctrl.updateValueAndValidity({ emitEvent: false });
  }

  private async loadConnectorData(id: string): Promise<void> {
    this.loading.set(true);
    try {
      const connector = await this.connectorsService.fetchConnector(id);
      this.loadedConnector.set(connector);

      this.connectorForm.patchValue({
        providerId: connector.providerId,
        displayName: connector.displayName,
        providerType: connector.providerType,
        clientId: '',
        clientSecret: '',
        oauthDiscoveryUrl: connector.oauthDiscoveryUrl ?? '',
        scopes: connector.scopes.join(', '),
        allowedRoles: connector.allowedRoles.length > 0 ? connector.allowedRoles : ['*'],
        grantAllRoles: connector.allowedRoles.length === 0,
        enabled: connector.enabled,
        iconName: connector.iconName || 'heroLink',
      });
      this.selectedRoles.set(connector.allowedRoles.length > 0 ? connector.allowedRoles : ['*']);
      this.applyDiscoveryValidator();
    } catch (error) {
      console.error('Error loading connector:', error);
      alert('Failed to load connector. Returning to list.');
      this.router.navigate(['/admin/connectors']);
    } finally {
      this.loading.set(false);
    }
  }

  selectConnectorType(type: ConnectorType): void {
    const preset = getConnectorPreset(type);
    if (preset) {
      this.connectorForm.patchValue({
        providerType: type,
        displayName: preset.displayName,
        scopes: preset.defaultScopes.join(', '),
        iconName: preset.iconName,
      });
    }
    this.applyDiscoveryValidator();
  }

  getPresetIconClasses(type: ConnectorType): string {
    const base = 'flex size-10 items-center justify-center rounded-sm';
    switch (type) {
      case 'google':
        return `${base} bg-red-100 text-red-600 dark:bg-red-900/30 dark:text-red-400`;
      case 'microsoft':
        return `${base} bg-blue-100 text-blue-600 dark:bg-blue-900/30 dark:text-blue-400`;
      case 'github':
        return `${base} bg-gray-800 text-white dark:bg-gray-600`;
      case 'canvas':
        return `${base} bg-orange-100 text-orange-600 dark:bg-orange-900/30 dark:text-orange-400`;
      default:
        return `${base} bg-purple-100 text-purple-600 dark:bg-purple-900/30 dark:text-purple-400`;
    }
  }

  onGrantAllRolesChange(): void {
    const checked = this.connectorForm.controls.grantAllRoles.value;
    if (checked) {
      this.connectorForm.controls.allowedRoles.setValue(['*']);
      this.selectedRoles.set(['*']);
    } else {
      this.connectorForm.controls.allowedRoles.setValue([]);
      this.selectedRoles.set([]);
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
    const newRoles = currentRoles.includes(roleId)
      ? currentRoles.filter(r => r !== roleId)
      : [...currentRoles, roleId];
    this.connectorForm.controls.allowedRoles.setValue(newRoles);
    this.selectedRoles.set(newRoles);
  }

  async copyCallbackUrl(url: string): Promise<void> {
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      this.callbackCopied.set(true);
      setTimeout(() => this.callbackCopied.set(false), 2000);
    } catch (err) {
      console.error('Clipboard write failed', err);
    }
  }

  async onSubmit(): Promise<void> {
    if (this.connectorForm.invalid || this.credentialPairError()) {
      this.connectorForm.markAllAsTouched();
      return;
    }

    this.isSubmitting.set(true);
    try {
      const formValue = this.connectorForm.getRawValue();

      const scopes = formValue.scopes
        ? formValue.scopes.split(',').map((s: string) => s.trim()).filter(Boolean)
        : [];

      const allowedRoles = formValue.grantAllRoles
        ? []
        : (formValue.allowedRoles || []).filter((r: string) => r !== '*');

      if (this.isEditMode() && this.providerId()) {
        const updates: ConnectorUpdateRequest = {
          displayName: formValue.displayName,
          scopes,
          allowedRoles,
          enabled: formValue.enabled,
          iconName: formValue.iconName,
        };
        if (formValue.clientId && formValue.clientSecret) {
          updates.clientId = formValue.clientId;
          updates.clientSecret = formValue.clientSecret;
        }
        if (this.needsDiscovery() && formValue.oauthDiscoveryUrl) {
          updates.oauthDiscoveryUrl = formValue.oauthDiscoveryUrl;
        }
        await this.connectorsService.updateConnector(this.providerId()!, updates);
        this.router.navigate(['/admin/connectors']);
      } else {
        const createData: ConnectorCreateRequest = {
          providerId: formValue.providerId,
          displayName: formValue.displayName,
          providerType: formValue.providerType,
          clientId: formValue.clientId,
          clientSecret: formValue.clientSecret,
          scopes,
          allowedRoles,
          enabled: formValue.enabled,
          iconName: formValue.iconName,
        };
        if (this.needsDiscovery() && formValue.oauthDiscoveryUrl) {
          createData.oauthDiscoveryUrl = formValue.oauthDiscoveryUrl;
        }
        const created = await this.connectorsService.createConnector(createData);
        this.createdConnector.set(created);
      }
    } catch (error: unknown) {
      console.error('Error saving connector:', error);
      alert(this.formatErrorMessage(error));
    } finally {
      this.isSubmitting.set(false);
    }
  }

  goBack(): void {
    this.router.navigate(['/admin/connectors']);
  }

  /**
   * FastAPI returns validation errors as `detail: [{loc, msg, type, ...}]`
   * and business errors as `detail: "string"`. Collapse both shapes into a
   * single human-readable string for the alert.
   */
  private formatErrorMessage(error: unknown): string {
    const body = (error as { error?: { detail?: unknown } } | null)?.error;
    const detail = body?.detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      return detail
        .map(d => (d as { msg?: string })?.msg)
        .filter((m): m is string => typeof m === 'string')
        .join('\n') || 'Failed to save connector.';
    }
    const message = (error as { message?: string } | null)?.message;
    return message ?? 'Failed to save connector.';
  }
}
