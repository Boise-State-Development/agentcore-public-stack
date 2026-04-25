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
  heroCloud,
  heroCodeBracket,
  heroAcademicCap,
  heroLink,
  heroChatBubbleLeftRight,
  heroVideoCamera,
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
  /**
   * Optional uploaded icon as a base64 data URL. `''` means no upload (fall
   * back to `iconName`). On update, sending `''` to the backend clears any
   * previously uploaded icon.
   */
  iconData: FormControl<string>;
  /**
   * Free-form `key=value` lines (one per line) for vendor-specific OAuth
   * params. Parsed to `Record<string, string>` before submit. Blank lines
   * and lines without `=` are silently dropped.
   */
  customParameters: FormControl<string>;
}

const ICON_DATA_MAX_BYTES = 100 * 1024;
const ICON_ACCEPTED_MIME_TYPES = [
  'image/png',
  'image/jpeg',
  'image/gif',
  'image/webp',
  'image/svg+xml',
];

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
      heroCloud,
      heroCodeBracket,
      heroAcademicCap,
      heroLink,
      heroChatBubbleLeftRight,
      heroVideoCamera,
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

                <div>
                  <label class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Icon
                    <span class="font-normal text-gray-400 dark:text-gray-500">(optional)</span>
                  </label>
                  <div class="flex items-center gap-4">
                    <div class="flex size-14 shrink-0 items-center justify-center rounded-sm border border-gray-200 bg-gray-50 dark:border-gray-600 dark:bg-gray-900">
                      @if (connectorForm.controls.iconData.value) {
                        <img
                          [src]="connectorForm.controls.iconData.value"
                          alt="Connector icon preview"
                          class="size-10 object-contain"
                        />
                      } @else {
                        <ng-icon
                          [name]="connectorForm.controls.iconName.value || 'heroLink'"
                          class="size-6 text-gray-400 dark:text-gray-500"
                          aria-hidden="true"
                        />
                      }
                    </div>
                    <div class="flex flex-wrap items-center gap-2">
                      <label
                        class="inline-flex cursor-pointer items-center gap-2 rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 font-semibold text-gray-700 hover:bg-gray-50 focus-within:outline-hidden focus-within:ring-3 focus-within:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600"
                      >
                        {{ connectorForm.controls.iconData.value ? 'Replace' : 'Upload' }}
                        <input
                          type="file"
                          class="sr-only"
                          [accept]="acceptedIconTypes"
                          (change)="onIconFileSelected($event)"
                        />
                      </label>
                      @if (connectorForm.controls.iconData.value) {
                        <button
                          type="button"
                          (click)="removeUploadedIcon()"
                          class="inline-flex items-center gap-2 rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 font-semibold text-gray-700 hover:bg-gray-50 focus:outline-hidden focus:ring-3 focus:ring-gray-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-200 dark:hover:bg-gray-600"
                        >
                          Remove
                        </button>
                      }
                    </div>
                  </div>
                  <p class="mt-1.5 text-xs/5 text-gray-500 dark:text-gray-400">
                    PNG, JPEG, GIF, WebP, or SVG. Max 100KB. Falls back to the default icon when no image is uploaded.
                  </p>
                  @if (iconUploadError(); as iconErr) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">{{ iconErr }}</p>
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
                    [placeholder]="scopesPlaceholder()"
                    class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2.5 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                  <p class="mt-1.5 text-xs/5 text-gray-500 dark:text-gray-400">
                    Comma-separated list of OAuth scopes to request during authorization.
                  </p>
                </div>

                <div>
                  <label for="customParameters" class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Custom OAuth Parameters
                    <span class="font-normal text-gray-400 dark:text-gray-500">(optional)</span>
                  </label>
                  <textarea
                    id="customParameters"
                    formControlName="customParameters"
                    rows="3"
                    [placeholder]="customParametersPlaceholder()"
                    spellcheck="false"
                    class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2.5 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  ></textarea>
                  <p class="mt-1.5 text-xs/5 text-gray-500 dark:text-gray-400">
                    One <code class="rounded-xs bg-gray-100 px-1 py-0.5 dark:bg-gray-700">key=value</code> pair per line, forwarded to AgentCore Identity as <code class="rounded-xs bg-gray-100 px-1 py-0.5 dark:bg-gray-700">customParameters</code>.
                    Required vendor params (e.g. Google's <code class="rounded-xs bg-gray-100 px-1 py-0.5 dark:bg-gray-700">access_type=offline</code>) are sent automatically and override any conflicting entries here.
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
  readonly acceptedIconTypes = ICON_ACCEPTED_MIME_TYPES.join(',');

  readonly isEditMode = signal(false);
  readonly providerId = signal<string | null>(null);
  readonly isSubmitting = signal(false);
  readonly loading = signal(false);
  readonly showClientSecret = signal(false);
  readonly loadedConnector = signal<Connector | null>(null);
  readonly createdConnector = signal<Connector | null>(null);
  readonly callbackCopied = signal(false);
  /** Validation error from the most recent file pick (null when ok). */
  readonly iconUploadError = signal<string | null>(null);
  /**
   * Tracks the icon_data value that was loaded from the server, so we know
   * whether to send `iconData: ""` on update (clear) when the admin removes
   * the upload. `null` means no icon was loaded; a string means one was.
   */
  private readonly iconLoadedFromServer = signal<string | null>(null);

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
    iconData: this.fb.control('', { nonNullable: true }),
    customParameters: this.fb.control('', { nonNullable: true }),
  });

  readonly pageTitle = computed(() => (this.isEditMode() ? 'Edit Connector' : 'Add Connector'));

  readonly availableRoles = computed(() => this.appRolesService.getEnabledRoles());

  readonly selectedRoles = signal<string[]>(['*']);

  // Form controls aren't observable signals, so mirror providerType into a
  // signal updated from valueChanges. This drives the template's @if for
  // discovery, the placeholder lookups, and the submit-time discovery
  // gating.
  readonly needsDiscovery = signal(
    requiresDiscovery(this.connectorForm.controls.providerType.value)
  );

  /** Mirrors `providerType` so computed placeholders react to changes. */
  private readonly providerTypeSignal = signal<ConnectorType>(
    this.connectorForm.controls.providerType.value,
  );

  /** Vendor-relevant scopes example shown when the field is empty. */
  readonly scopesPlaceholder = computed<string>(() => {
    const preset = getConnectorPreset(this.providerTypeSignal());
    return preset?.scopesPlaceholder ?? 'openid, email, profile';
  });

  /**
   * Vendor-relevant `key=value` example for the custom-parameters
   * textarea. Generic `key=value` fallback for vendors with no
   * commonly-used extras.
   */
  readonly customParametersPlaceholder = computed<string>(() => {
    const preset = getConnectorPreset(this.providerTypeSignal());
    return preset?.customParametersPlaceholder ?? 'key=value';
  });

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

    this.connectorForm.controls.providerType.valueChanges.subscribe((value) => {
      this.providerTypeSignal.set(value);
      this.applyDiscoveryValidator();
    });
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
        iconData: connector.iconData ?? '',
        customParameters: this.serializeCustomParameters(connector.customParameters ?? null),
      });
      this.iconLoadedFromServer.set(connector.iconData ?? null);
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
        // Only seed customParameters from a preset if the preset declares
        // them — most don't, and we don't want to clobber whatever the
        // admin has already typed.
        ...(preset.defaultCustomParameters
          ? {
              customParameters: this.serializeCustomParameters(
                preset.defaultCustomParameters,
              ),
            }
          : {}),
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
      case 'slack':
        return `${base} bg-fuchsia-100 text-fuchsia-700 dark:bg-fuchsia-900/30 dark:text-fuchsia-300`;
      case 'salesforce':
        return `${base} bg-sky-100 text-sky-700 dark:bg-sky-900/30 dark:text-sky-300`;
      case 'zoom':
        return `${base} bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300`;
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

      // Parse the textarea into a key/value map. The empty case sends `{}`
      // on update (explicitly clears extras) and is omitted on create.
      const customParameters = this.parseCustomParameters(formValue.customParameters);

      if (this.isEditMode() && this.providerId()) {
        const updates: ConnectorUpdateRequest = {
          displayName: formValue.displayName,
          scopes,
          allowedRoles,
          enabled: formValue.enabled,
          iconName: formValue.iconName,
          customParameters,
        };
        // Tri-state for iconData: only send when the admin actually changed
        // it. Replaced upload → send the new data URL. Removed an existing
        // upload → send `""` so the backend clears it. No change → omit.
        const previousIcon = this.iconLoadedFromServer();
        const currentIcon = formValue.iconData || '';
        if (currentIcon !== (previousIcon ?? '')) {
          updates.iconData = currentIcon;
        }
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
        if (formValue.iconData) {
          createData.iconData = formValue.iconData;
        }
        if (this.needsDiscovery() && formValue.oauthDiscoveryUrl) {
          createData.oauthDiscoveryUrl = formValue.oauthDiscoveryUrl;
        }
        if (Object.keys(customParameters).length > 0) {
          createData.customParameters = customParameters;
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

  /**
   * Parse the textarea contents (one `key=value` per line) into a map.
   * Blank lines and lines without `=` are silently dropped — the admin
   * sees the cleaned-up version when they re-open the form for editing.
   */
  private parseCustomParameters(raw: string): Record<string, string> {
    const out: Record<string, string> = {};
    if (!raw) return out;
    for (const line of raw.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      const eq = trimmed.indexOf('=');
      if (eq <= 0) continue; // drop lines with no `=` or empty key
      const key = trimmed.slice(0, eq).trim();
      const value = trimmed.slice(eq + 1).trim();
      if (!key) continue;
      out[key] = value;
    }
    return out;
  }

  /**
   * Serialize a saved map back into the `key=value\nkey=value` textarea
   * format. Keys are sorted for deterministic display so admin diffs stay
   * stable across edits.
   */
  private serializeCustomParameters(
    map: Record<string, string> | null,
  ): string {
    if (!map) return '';
    return Object.keys(map)
      .sort()
      .map(key => `${key}=${map[key]}`)
      .join('\n');
  }

  async onIconFileSelected(event: Event): Promise<void> {
    const input = event.target as HTMLInputElement;
    const file = input.files?.[0];
    if (!file) return;

    if (!ICON_ACCEPTED_MIME_TYPES.includes(file.type)) {
      this.iconUploadError.set(
        'Unsupported file type. Use PNG, JPEG, GIF, WebP, or SVG.',
      );
      input.value = '';
      return;
    }
    if (file.size > ICON_DATA_MAX_BYTES) {
      this.iconUploadError.set(
        `Icon must be ${Math.floor(ICON_DATA_MAX_BYTES / 1024)}KB or smaller.`,
      );
      input.value = '';
      return;
    }

    try {
      const dataUrl = await this.readFileAsDataUrl(file);
      this.connectorForm.controls.iconData.setValue(dataUrl);
      this.connectorForm.controls.iconData.markAsDirty();
      this.iconUploadError.set(null);
    } catch (err) {
      console.error('Icon upload read failed', err);
      this.iconUploadError.set('Failed to read the file. Try again.');
    } finally {
      // Reset so picking the same file again still re-fires the change event.
      input.value = '';
    }
  }

  removeUploadedIcon(): void {
    this.connectorForm.controls.iconData.setValue('');
    this.connectorForm.controls.iconData.markAsDirty();
    this.iconUploadError.set(null);
  }

  private readFileAsDataUrl(file: File): Promise<string> {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result as string);
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
  }
}
