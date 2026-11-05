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
  AbstractControl,
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
  heroMagnifyingGlass,
  heroChevronDown,
} from '@ng-icons/heroicons/outline';
import { ConnectorsService } from '../services/connectors.service';
import { RoleOptionsService } from '../../roles/services/role-options.service';
import {
  Connector,
  ConnectorCreateRequest,
  ConnectorUpdateRequest,
  ConnectorType,
  CONNECTOR_PRESETS,
  CONNECTOR_PRESET_GROUPS,
  getConnectorPreset,
  getConnectorTypeLabel,
  requiresDiscovery,
  requiresTenantEndpoints,
  TENANT_ISSUER_PLACEHOLDERS,
} from '../models/connector.model';
import { getBrandIconDataUrl } from '../models/connector-brand-icons';
import { ConnectorLogoComponent } from '../components/connector-logo.component';
import { TooltipDirective } from '../../../components/tooltip/tooltip.directive';
import { SpinnerComponent } from '../../../components/spinner/spinner.component';

interface ConnectorFormGroup {
  providerId: FormControl<string>;
  displayName: FormControl<string>;
  providerType: FormControl<ConnectorType>;
  clientId: FormControl<string>;
  clientSecret: FormControl<string>;
  oauthDiscoveryUrl: FormControl<string>;
  /** Microsoft only: Entra tenant ID or verified domain, `''` when unset. */
  tenantId: FormControl<string>;
  /** Tenant-hosted vendors only (Okta, Auth0, ...): sent as `authorizationServerMetadata`. */
  tenantIssuer: FormControl<string>;
  tenantAuthorizationEndpoint: FormControl<string>;
  tenantTokenEndpoint: FormControl<string>;
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
  /**
   * File-source adapter key, or `''` when the connector is not a file
   * source. On update, sending `''` clears an existing mapping.
   */
  fileSourceAdapterId: FormControl<string>;
  /**
   * Export-target adapter key, or `''` when the connector is not an export
   * target. On update, sending `''` clears an existing mapping.
   */
  exportTargetAdapterId: FormControl<string>;
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
  imports: [ReactiveFormsModule, NgIcon, TooltipDirective, SpinnerComponent, ConnectorLogoComponent],
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
      heroMagnifyingGlass,
      heroChevronDown,
    }),
  ],
  host: { class: 'block' },
  template: `
    <div class="min-h-dvh">
      <div class="mx-auto max-w-3xl px-4 py-8 sm:px-6 lg:px-8">
        <button
          type="button"
          (click)="goBack()"
          class="-ml-2 mb-4 inline-flex items-center gap-1.5 rounded-2xl px-2 py-1 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-white"
        >
          <ng-icon name="heroArrowLeft" class="size-4" aria-hidden="true" />
          Back to Connectors
        </button>

        <div class="mb-8 flex items-center gap-4">
          @if (isEditMode() && loadedConnector(); as loaded) {
            <app-connector-logo
              [type]="loaded.providerType"
              [iconData]="loaded.iconData"
              [iconName]="loaded.iconName"
              size="lg"
            />
          }
          <div class="min-w-0">
            <h1 class="text-2xl/8 font-bold text-gray-900 dark:text-white">{{ pageTitle() }}</h1>
            <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
              {{ isEditMode() ? 'Update connector settings and credentials.' : 'Register an OAuth provider that users can connect for tool authentication.' }}
            </p>
          </div>
        </div>

        @if (loading()) {
          <div class="flex h-64 items-center justify-center">
            <div class="flex flex-col items-center gap-4">
              <app-spinner size="xl" label="Loading connector" />
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">Loading connector…</p>
            </div>
          </div>
        } @else if (createdConnector(); as created) {
          <!-- After create: the callback URL has to be registered with the vendor -->
          <div class="space-y-8">
            <div class="rounded-2xl border border-state-success-200 bg-state-success-50 p-4 dark:border-state-success-800 dark:bg-state-success-900/20">
              <div class="flex items-start gap-3">
                <ng-icon name="heroCheckCircle" class="mt-0.5 size-5 shrink-0 text-state-success-600 dark:text-state-success-400" aria-hidden="true" />
                <div class="min-w-0 flex-1">
                  <h2 class="text-sm/6 font-semibold text-state-success-800 dark:text-state-success-200">Connector created</h2>
                  <p class="mt-1 text-sm/6 text-state-success-700 dark:text-state-success-300">
                    "{{ created.displayName }}" is registered with AgentCore Identity.
                  </p>
                </div>
                <app-connector-logo [type]="created.providerType" [iconData]="created.iconData" [iconName]="created.iconName" size="sm" />
              </div>
            </div>

            <section class="space-y-4">
              <div>
                <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">Next step: register this callback URL</h2>
                <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
                  Add it to {{ created.displayName }}'s list of authorized redirect URIs in the provider's developer console.
                  Until you do, users will see an error when they try to consent.
                </p>
              </div>
              <div class="flex items-stretch gap-2">
                <label for="createdCallbackUrl" class="sr-only">Callback URL</label>
                <input
                  type="text"
                  id="createdCallbackUrl"
                  [value]="created.callbackUrl || ''"
                  readonly
                  [class]="monoInputClass"
                />
                <button
                  type="button"
                  (click)="copyCallbackUrl(created.callbackUrl || '')"
                  aria-label="Copy callback URL"
                  [class]="secondaryButtonClass"
                  [appTooltip]="callbackCopied() ? 'Copied!' : 'Copy to clipboard'"
                >
                  <ng-icon [name]="callbackCopied() ? 'heroClipboardDocumentCheck' : 'heroClipboard'" class="size-5" aria-hidden="true" />
                </button>
              </div>
            </section>

            <div class="border-t border-gray-200 pt-6 dark:border-gray-700">
              <button type="button" (click)="goBack()" [class]="primaryButtonClass">Done</button>
            </div>
          </div>
        } @else {
          <form [formGroup]="connectorForm" (ngSubmit)="onSubmit()" class="space-y-8">

            @if (!isEditMode()) {
              <section class="space-y-4" aria-labelledby="provider-heading">
                <div>
                  <h2 id="provider-heading" class="text-base/7 font-semibold text-gray-900 dark:text-white">Choose a provider</h2>
                  <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
                    Pick a built-in provider to pre-fill its scopes and logo, or use Custom for any OpenID Connect provider.
                  </p>
                </div>

                <div class="relative">
                  <ng-icon
                    name="heroMagnifyingGlass"
                    class="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-gray-400 dark:text-gray-500"
                    aria-hidden="true"
                  />
                  <label for="presetSearch" class="sr-only">Search providers</label>
                  <input
                    type="text"
                    id="presetSearch"
                    [value]="presetQuery()"
                    (input)="presetQuery.set($any($event.target).value)"
                    placeholder="Search providers…"
                    autocomplete="off"
                    class="block w-full rounded-2xl border border-gray-300 bg-white py-2 pl-9 pr-3 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500"
                  />
                </div>

                <fieldset class="space-y-6">
                  <legend class="sr-only">Connector type</legend>
                  @for (group of presetGroups(); track group.category) {
                    <div>
                      <h3 class="text-sm/6 font-medium text-gray-900 dark:text-white">{{ group.label }}</h3>
                      <p class="text-xs/5 text-gray-600 dark:text-gray-400">{{ group.description }}</p>
                      <div class="mt-3 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
                        @for (preset of group.presets; track preset.type) {
                          <label
                            class="flex cursor-pointer items-center gap-3 rounded-2xl border border-gray-200 bg-white p-3 transition-colors hover:border-gray-300 hover:bg-gray-50 has-checked:border-primary-500 has-checked:ring-2 has-checked:ring-primary-500 has-focus-visible:outline-2 has-focus-visible:outline-offset-2 has-focus-visible:outline-primary-500 dark:border-gray-700 dark:bg-gray-800 dark:hover:border-gray-600 dark:hover:bg-gray-700/50"
                          >
                            <input
                              type="radio"
                              name="providerTypePreset"
                              class="sr-only"
                              [value]="preset.type"
                              [checked]="providerTypeSignal() === preset.type"
                              (change)="selectConnectorType(preset.type)"
                            />
                            <app-connector-logo [type]="preset.type" [iconName]="preset.iconName" size="md" />
                            <span class="min-w-0">
                              <span class="block truncate text-sm/6 font-medium text-gray-900 dark:text-white">{{ preset.displayName }}</span>
                              @if (preset.hint) {
                                <span class="block text-xs/5 text-gray-600 dark:text-gray-400">{{ preset.hint }}</span>
                              }
                            </span>
                          </label>
                        }
                      </div>
                    </div>
                  } @empty {
                    <p class="rounded-2xl border border-dashed border-gray-300 p-6 text-center text-sm/6 text-gray-600 dark:border-gray-700 dark:text-gray-400">
                      No providers match "{{ presetQuery() }}". Use Custom for any OpenID Connect provider.
                    </p>
                  }
                </fieldset>
              </section>
            }

            <section class="space-y-4" [class.border-t]="!isEditMode()" [class.border-gray-200]="!isEditMode()" [class.pt-8]="!isEditMode()" [class.dark:border-gray-700]="!isEditMode()">
              <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">Basic information</h2>

              <div>
                <label for="providerId" [class]="labelClass">
                  Connector ID <span class="text-state-danger-600 dark:text-state-danger-400">*</span>
                </label>
                <input
                  type="text"
                  id="providerId"
                  formControlName="providerId"
                  placeholder="e.g., google-workspace, github-enterprise"
                  [readonly]="isEditMode()"
                  [attr.aria-invalid]="isInvalid(connectorForm.controls.providerId)"
                  class="mt-1"
                  [class]="inputClass"
                />
                <p [class]="helpClass">Unique identifier. Lowercase letters, numbers, and hyphens only.</p>
                @if (isInvalid(connectorForm.controls.providerId)) {
                  <p class="mt-1 text-sm/6 text-state-danger-600 dark:text-state-danger-400" role="alert">
                    @if (connectorForm.controls.providerId.errors?.['required']) { Connector ID is required }
                    @else if (connectorForm.controls.providerId.errors?.['pattern']) { Must be lowercase letters, numbers, and hyphens only }
                    @else if (connectorForm.controls.providerId.errors?.['maxlength']) { Must be at most 64 characters }
                  </p>
                }
              </div>

              <div>
                <label for="displayName" [class]="labelClass">
                  Display name <span class="text-state-danger-600 dark:text-state-danger-400">*</span>
                </label>
                <input
                  type="text"
                  id="displayName"
                  formControlName="displayName"
                  placeholder="e.g., Google Workspace, GitHub Enterprise"
                  [attr.aria-invalid]="isInvalid(connectorForm.controls.displayName)"
                  class="mt-1"
                  [class]="inputClass"
                />
                @if (isInvalid(connectorForm.controls.displayName)) {
                  <p class="mt-1 text-sm/6 text-state-danger-600 dark:text-state-danger-400" role="alert">Display name is required</p>
                }
              </div>

              <div>
                <span id="icon-label" [class]="labelClass">
                  Icon <span class="font-normal text-gray-500 dark:text-gray-400">(optional)</span>
                </span>
                <div class="mt-2 flex items-center gap-4" role="group" aria-labelledby="icon-label">
                  <app-connector-logo
                    [iconData]="iconDataSignal()"
                    [iconName]="connectorForm.controls.iconName.value"
                    size="lg"
                  />
                  <div class="flex flex-wrap items-center gap-2">
                    <label [class]="secondaryButtonClass" class="cursor-pointer focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-primary-500">
                      {{ iconDataSignal() ? 'Replace' : 'Upload' }}
                      <input
                        type="file"
                        class="sr-only"
                        [accept]="acceptedIconTypes"
                        (change)="onIconFileSelected($event)"
                      />
                    </label>
                    @if (iconDataSignal()) {
                      <button type="button" (click)="removeUploadedIcon()" [class]="ghostButtonClass">Remove</button>
                    }
                  </div>
                </div>
                <p [class]="helpClass">
                  @if (usingBrandLogo()) {
                    Using the {{ providerLabel() }} logo. Upload your own to replace it.
                  } @else {
                    PNG, JPEG, GIF, WebP, or SVG. Max 100KB. Falls back to a generic icon when none is set.
                  }
                </p>
                @if (iconUploadError(); as iconErr) {
                  <p class="mt-1 text-sm/6 text-state-danger-600 dark:text-state-danger-400" role="alert">{{ iconErr }}</p>
                }
              </div>

              <div class="flex items-center gap-3">
                <input type="checkbox" id="enabled" formControlName="enabled" [class]="checkboxClass" />
                <label for="enabled" [class]="labelClass">Connector enabled</label>
              </div>
            </section>

            <!-- Edit mode: AgentCore-owned metadata, read-only -->
            @if (isEditMode() && loadedConnector(); as loaded) {
              <section class="space-y-4 border-t border-gray-200 pt-8 dark:border-gray-700">
                <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">AgentCore Identity</h2>
                <div>
                  <label for="callbackUrl" [class]="labelClass">Callback URL</label>
                  <div class="mt-1 flex items-stretch gap-2">
                    <input type="text" id="callbackUrl" [value]="loaded.callbackUrl || ''" readonly [class]="monoInputClass" />
                    <button
                      type="button"
                      (click)="copyCallbackUrl(loaded.callbackUrl || '')"
                      aria-label="Copy callback URL"
                      [class]="secondaryButtonClass"
                      [appTooltip]="callbackCopied() ? 'Copied!' : 'Copy to clipboard'"
                    >
                      <ng-icon [name]="callbackCopied() ? 'heroClipboardDocumentCheck' : 'heroClipboard'" class="size-5" aria-hidden="true" />
                    </button>
                  </div>
                </div>
                @if (loaded.credentialProviderArn) {
                  <div>
                    <label for="credentialProviderArn" [class]="labelClass">Credential provider ARN</label>
                    <input type="text" id="credentialProviderArn" [value]="loaded.credentialProviderArn" readonly class="mt-1 text-xs/5" [class]="monoInputClass" />
                  </div>
                }
              </section>
            }

            <section class="space-y-4 border-t border-gray-200 pt-8 dark:border-gray-700">
              <div>
                <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">OAuth credentials</h2>
                <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
                  @if (isEditMode()) {
                    Enter both fields to rotate credentials. Leave both blank to keep the existing ones.
                  } @else {
                    Credentials are stored by AWS Bedrock AgentCore Identity, never by this application.
                  }
                </p>
              </div>

              <div>
                <label for="clientId" [class]="labelClass">
                  Client ID
                  @if (!isEditMode()) { <span class="text-state-danger-600 dark:text-state-danger-400">*</span> }
                </label>
                <input
                  type="text"
                  id="clientId"
                  formControlName="clientId"
                  autocomplete="off"
                  placeholder="Your OAuth client ID"
                  [attr.aria-invalid]="isInvalid(connectorForm.controls.clientId)"
                  class="mt-1"
                  [class]="monoInputClass"
                />
              </div>

              <div>
                <label for="clientSecret" [class]="labelClass">
                  Client secret
                  @if (!isEditMode()) { <span class="text-state-danger-600 dark:text-state-danger-400">*</span> }
                </label>
                <div class="relative mt-1">
                  <input
                    [type]="showClientSecret() ? 'text' : 'password'"
                    id="clientSecret"
                    formControlName="clientSecret"
                    autocomplete="off"
                    [placeholder]="isEditMode() ? 'Leave blank to keep existing' : 'Your OAuth client secret'"
                    class="pr-10"
                    [class]="monoInputClass"
                  />
                  <button
                    type="button"
                    (click)="showClientSecret.set(!showClientSecret())"
                    [attr.aria-label]="showClientSecret() ? 'Hide client secret' : 'Show client secret'"
                    class="absolute right-2 top-1/2 flex size-7 -translate-y-1/2 items-center justify-center rounded-2xl text-gray-500 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
                    [appTooltip]="showClientSecret() ? 'Hide secret' : 'Show secret'"
                  >
                    <ng-icon [name]="showClientSecret() ? 'heroEyeSlash' : 'heroEye'" class="size-5" aria-hidden="true" />
                  </button>
                </div>
              </div>

              @if (credentialPairError()) {
                <p class="text-sm/6 text-state-danger-600 dark:text-state-danger-400" role="alert">{{ credentialPairError() }}</p>
              }

              @if (needsDiscovery()) {
                <div>
                  <label for="oauthDiscoveryUrl" [class]="labelClass">
                    OIDC discovery URL <span class="text-state-danger-600 dark:text-state-danger-400">*</span>
                  </label>
                  <input
                    type="url"
                    id="oauthDiscoveryUrl"
                    formControlName="oauthDiscoveryUrl"
                    placeholder="https://example.com/.well-known/openid-configuration"
                    [attr.aria-invalid]="isInvalid(connectorForm.controls.oauthDiscoveryUrl)"
                    class="mt-1"
                    [class]="monoInputClass"
                  />
                  <p [class]="helpClass">AgentCore fetches this URL to resolve the authorization and token endpoints.</p>
                </div>
              }

              @if (needsTenantEndpoints()) {
                <div class="space-y-4 border-l-2 border-gray-200 pl-4 dark:border-gray-700">
                  <div>
                    <h3 class="text-sm/6 font-medium text-gray-900 dark:text-white">Tenant endpoints</h3>
                    <p class="text-xs/5 text-gray-600 dark:text-gray-400">
                      {{ providerLabel() }} runs in your own tenant, so AgentCore needs its issuer and OAuth endpoints. All three must be HTTPS URLs.
                    </p>
                  </div>
                  <div>
                    <label for="tenantIssuer" [class]="labelClass">
                      Issuer <span class="text-state-danger-600 dark:text-state-danger-400">*</span>
                    </label>
                    <input
                      type="url"
                      id="tenantIssuer"
                      formControlName="tenantIssuer"
                      autocomplete="off"
                      [placeholder]="tenantIssuerPlaceholder()"
                      [attr.aria-invalid]="isInvalid(connectorForm.controls.tenantIssuer)"
                      class="mt-1"
                      [class]="monoInputClass"
                    />
                  </div>
                  <div>
                    <label for="tenantAuthorizationEndpoint" [class]="labelClass">
                      Authorization endpoint <span class="text-state-danger-600 dark:text-state-danger-400">*</span>
                    </label>
                    <input
                      type="url"
                      id="tenantAuthorizationEndpoint"
                      formControlName="tenantAuthorizationEndpoint"
                      autocomplete="off"
                      [placeholder]="tenantIssuerPlaceholder() + '/authorize'"
                      [attr.aria-invalid]="isInvalid(connectorForm.controls.tenantAuthorizationEndpoint)"
                      class="mt-1"
                      [class]="monoInputClass"
                    />
                  </div>
                  <div>
                    <label for="tenantTokenEndpoint" [class]="labelClass">
                      Token endpoint <span class="text-state-danger-600 dark:text-state-danger-400">*</span>
                    </label>
                    <input
                      type="url"
                      id="tenantTokenEndpoint"
                      formControlName="tenantTokenEndpoint"
                      autocomplete="off"
                      [placeholder]="tenantIssuerPlaceholder() + '/token'"
                      [attr.aria-invalid]="isInvalid(connectorForm.controls.tenantTokenEndpoint)"
                      class="mt-1"
                      [class]="monoInputClass"
                    />
                  </div>
                </div>
              }

              @if (isMicrosoft()) {
                <div>
                  <label for="tenantId" [class]="labelClass">Directory (tenant) ID</label>
                  <input
                    type="text"
                    id="tenantId"
                    formControlName="tenantId"
                    placeholder="00000000-0000-0000-0000-000000000000 or contoso.onmicrosoft.com"
                    [attr.aria-describedby]="isInvalid(connectorForm.controls.tenantId) ? 'tenantId-error tenantId-help' : 'tenantId-help'"
                    [attr.aria-invalid]="isInvalid(connectorForm.controls.tenantId)"
                    class="mt-1"
                    [class]="monoInputClass"
                  />
                  @if (isInvalid(connectorForm.controls.tenantId)) {
                    <p id="tenantId-error" class="mt-1 text-sm/6 text-state-danger-600 dark:text-state-danger-400" role="alert">
                      Enter a directory (tenant) ID or a verified domain.
                    </p>
                  }
                  <p id="tenantId-help" [class]="helpClass">
                    Required when the Entra app registration is single-tenant, the default for a new registration.
                    Shown on the app's Overview page.
                    @if (isEditMode()) { Changing it requires re-entering the client ID and secret. }
                  </p>
                </div>
              }

              <div>
                <label for="scopes" [class]="labelClass">Scopes</label>
                <input
                  type="text"
                  id="scopes"
                  formControlName="scopes"
                  [placeholder]="scopesPlaceholder()"
                  class="mt-1"
                  [class]="inputClass"
                />
                <p [class]="helpClass">Comma-separated list of OAuth scopes to request during authorization.</p>
              </div>

              <div>
                <label for="customParameters" [class]="labelClass">
                  Custom OAuth parameters
                  <span class="font-normal text-gray-500 dark:text-gray-400">(optional)</span>
                </label>
                <textarea
                  id="customParameters"
                  formControlName="customParameters"
                  rows="3"
                  [placeholder]="customParametersPlaceholder()"
                  spellcheck="false"
                  class="mt-1"
                  [class]="monoInputClass"
                ></textarea>
                <p [class]="helpClass">
                  One <code class="rounded bg-gray-100 px-1 py-0.5 dark:bg-gray-700">key=value</code> pair per line, forwarded to AgentCore Identity as <code class="rounded bg-gray-100 px-1 py-0.5 dark:bg-gray-700">customParameters</code>.
                  Required vendor params (e.g. Google's <code class="rounded bg-gray-100 px-1 py-0.5 dark:bg-gray-700">access_type=offline</code>) are sent automatically and override any conflicting entries here.
                </p>
              </div>
            </section>

            <section class="space-y-4 border-t border-gray-200 pt-8 dark:border-gray-700">
              <div>
                <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">Access control</h2>
                <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">Restrict which application roles can use this connector.</p>
              </div>

              <div class="flex items-center gap-3">
                <input
                  type="checkbox"
                  id="grantAllRoles"
                  formControlName="grantAllRoles"
                  (change)="onGrantAllRolesChange()"
                  [class]="checkboxClass"
                />
                <label for="grantAllRoles" [class]="labelClass">Allow all roles (unrestricted access)</label>
              </div>

              @if (!connectorForm.controls.grantAllRoles.value) {
                @if (rolesResource.isLoading() || rolesResource.value() === undefined) {
                  <div class="flex items-center gap-2">
                    <app-spinner size="sm" variant="brand" label="Loading roles" />
                    <p class="text-sm/6 text-gray-600 dark:text-gray-400">Loading roles…</p>
                  </div>
                } @else if (availableRoles().length > 0) {
                  <div class="flex flex-wrap gap-2" role="group" aria-label="Allowed roles">
                    @for (role of availableRoles(); track role.roleId) {
                      <button
                        type="button"
                        (click)="toggleRole(role.roleId)"
                        [attr.aria-pressed]="isRoleSelected(role.roleId)"
                        [class]="roleChipClass"
                        [appTooltip]="role.description || 'No description'"
                      >
                        {{ role.displayName }}
                      </button>
                    }
                  </div>
                } @else {
                  <p class="text-sm/6 text-gray-600 dark:text-gray-400">No roles available. Create roles in Role Management first.</p>
                }
              }
              <p [class]="helpClass">Only users with the selected roles will be able to use this connector.</p>
            </section>

            <section class="space-y-4 border-t border-gray-200 pt-8 dark:border-gray-700">
              <div>
                <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">File source</h2>
                <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
                  Optionally map this connector to a file-source adapter so it can import files into an assistant's knowledge base from the assistant editor.
                </p>
              </div>
              @if (compatibleAdapters().length > 0) {
                <div>
                  <label for="fileSourceAdapterId" [class]="labelClass">File-source adapter</label>
                  <div class="relative mt-1">
                    <select id="fileSourceAdapterId" formControlName="fileSourceAdapterId" class="pr-9" [class]="selectClass">
                      <option value="">Not a file source</option>
                      @for (adapter of compatibleAdapters(); track adapter.key) {
                        <option [value]="adapter.key">{{ adapter.displayName }}</option>
                      }
                    </select>
                    <ng-icon name="heroChevronDown" class="pointer-events-none absolute right-3 top-1/2 size-4 -translate-y-1/2 text-gray-400 dark:text-gray-500" aria-hidden="true" />
                  </div>
                  <p [class]="helpClass">When set, this connector appears as a file source in the assistant editor for any user allowed to use it.</p>
                  @if (scopeCoverageWarning(); as warning) {
                    <div class="mt-3 flex gap-2 rounded-2xl border border-state-warning-200 bg-state-warning-50 p-3 dark:border-state-warning-800 dark:bg-state-warning-900/20">
                      <ng-icon name="heroExclamationTriangle" class="size-5 shrink-0 text-state-warning-600 dark:text-state-warning-400" aria-hidden="true" />
                      <p class="text-sm/6 text-state-warning-700 dark:text-state-warning-300">{{ warning }}</p>
                    </div>
                  }
                </div>
              } @else {
                <p class="text-sm/6 text-gray-600 dark:text-gray-400">No file-source adapter is available for this connector type.</p>
              }
            </section>

            <section class="space-y-4 border-t border-gray-200 pt-8 dark:border-gray-700">
              <div>
                <h2 class="text-base/7 font-semibold text-gray-900 dark:text-white">Export target</h2>
                <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
                  Optionally map this connector to an export-target adapter so users can save a conversation to it from the chat "Save to…" dialog.
                </p>
              </div>
              @if (compatibleExportAdapters().length > 0) {
                <div>
                  <label for="exportTargetAdapterId" [class]="labelClass">Export-target adapter</label>
                  <div class="relative mt-1">
                    <select id="exportTargetAdapterId" formControlName="exportTargetAdapterId" class="pr-9" [class]="selectClass">
                      <option value="">Not an export target</option>
                      @for (adapter of compatibleExportAdapters(); track adapter.key) {
                        <option [value]="adapter.key">{{ adapter.displayName }}</option>
                      }
                    </select>
                    <ng-icon name="heroChevronDown" class="pointer-events-none absolute right-3 top-1/2 size-4 -translate-y-1/2 text-gray-400 dark:text-gray-500" aria-hidden="true" />
                  </div>
                  <p [class]="helpClass">When set, this connector appears as a destination in the "Save to…" dialog for any user allowed to use it.</p>
                  @if (exportScopeCoverageWarning(); as warning) {
                    <div class="mt-3 flex gap-2 rounded-2xl border border-state-warning-200 bg-state-warning-50 p-3 dark:border-state-warning-800 dark:bg-state-warning-900/20">
                      <ng-icon name="heroExclamationTriangle" class="size-5 shrink-0 text-state-warning-600 dark:text-state-warning-400" aria-hidden="true" />
                      <p class="text-sm/6 text-state-warning-700 dark:text-state-warning-300">{{ warning }}</p>
                    </div>
                  }
                </div>
              } @else {
                <p class="text-sm/6 text-gray-600 dark:text-gray-400">No export-target adapter is available for this connector type.</p>
              }
            </section>

            @if (isEditMode()) {
              <div class="flex items-start gap-3 rounded-2xl border border-state-warning-200 bg-state-warning-50 p-4 dark:border-state-warning-800 dark:bg-state-warning-900/20">
                <ng-icon name="heroExclamationTriangle" class="mt-0.5 size-5 shrink-0 text-state-warning-600 dark:text-state-warning-400" aria-hidden="true" />
                <div>
                  <h2 class="text-sm/6 font-semibold text-state-warning-800 dark:text-state-warning-200">Security notice</h2>
                  <p class="mt-1 text-sm/6 text-state-warning-700 dark:text-state-warning-300">
                    Changing scopes forces connected users to re-consent on their next tool call.
                    Rotating credentials requires re-entering both Client ID and Client Secret.
                  </p>
                </div>
              </div>
            }

            <div class="flex items-center gap-3 border-t border-gray-200 pt-6 dark:border-gray-700">
              <button
                type="submit"
                [disabled]="isSubmitting() || connectorForm.invalid || !!credentialPairError()"
                [class]="primaryButtonClass"
              >
                @if (isSubmitting()) {
                  <app-spinner size="sm" variant="on-solid" label="Saving" />
                  Saving…
                } @else {
                  {{ isEditMode() ? 'Update connector' : 'Create connector' }}
                }
              </button>
              <button type="button" (click)="goBack()" [disabled]="isSubmitting()" [class]="ghostButtonClass">Cancel</button>
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
  private roleOptionsService = inject(RoleOptionsService);

  readonly rolesResource = this.roleOptionsService.rolesResource;

  readonly presets = CONNECTOR_PRESETS;

  // Shared control styling. Kept as constants because the page repeats these
  // on a dozen controls; Tailwind still sees the full class names here.
  readonly labelClass = 'block text-sm/6 font-medium text-gray-700 dark:text-gray-300';
  readonly helpClass = 'mt-1 text-xs/5 text-gray-600 dark:text-gray-400';
  readonly inputClass =
    'block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 aria-invalid:border-state-danger-500 read-only:cursor-not-allowed read-only:bg-gray-50 dark:border-gray-600 dark:bg-gray-800 dark:text-white dark:placeholder:text-gray-500 dark:read-only:bg-gray-900';
  readonly monoInputClass = `${this.inputClass} font-mono`;
  readonly selectClass =
    'block w-full appearance-none rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-primary-500 focus:outline-none focus:ring-2 focus:ring-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-white';
  readonly checkboxClass =
    'size-4 rounded border-gray-300 text-primary-accessible focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800';
  readonly primaryButtonClass =
    'inline-flex items-center gap-2 rounded-2xl bg-primary-accessible px-4 py-2 text-sm/6 font-medium text-white hover:brightness-95 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:hover:brightness-110';
  readonly secondaryButtonClass =
    'inline-flex shrink-0 items-center justify-center gap-2 rounded-2xl border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700';
  readonly ghostButtonClass =
    'rounded-2xl px-4 py-2 text-sm/6 font-medium text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-gray-500 disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-400 dark:hover:bg-gray-800 dark:hover:text-white';
  readonly roleChipClass =
    'rounded-full border border-gray-300 bg-white px-3.5 py-1 text-sm/6 font-medium text-gray-700 transition-colors hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 aria-pressed:border-gray-900 aria-pressed:bg-gray-900 aria-pressed:text-white dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700 dark:aria-pressed:border-white dark:aria-pressed:bg-white dark:aria-pressed:text-gray-900';

  readonly presetQuery = signal('');

  /** Presets grouped for the picker, narrowed by the search box. */
  readonly presetGroups = computed(() => {
    const query = this.presetQuery().trim().toLowerCase();
    return CONNECTOR_PRESET_GROUPS.map(group => ({
      ...group,
      presets: this.presets.filter(
        preset =>
          preset.category === group.category &&
          (!query ||
            preset.displayName.toLowerCase().includes(query) ||
            preset.type.includes(query)),
      ),
    })).filter(group => group.presets.length > 0);
  });

  /** Mirrors the `iconData` control so the preview and hint can react to it. */
  readonly iconDataSignal = signal('');
  /** The logo this page last filled in itself, so an admin's own upload is never overwritten. */
  private readonly prefilledIcon = signal<string | null>(null);

  /** The preview is showing the vendor's logo that picking the preset filled in. */
  readonly usingBrandLogo = computed(() => {
    const current = this.iconDataSignal();
    return !!current && current === getBrandIconDataUrl(this.providerTypeSignal());
  });

  readonly providerLabel = computed(() => getConnectorTypeLabel(this.providerTypeSignal()));
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
    tenantId: this.fb.control('', {
      nonNullable: true,
      // Mirrors the backend's `normalize_tenant_id`: a GUID or a domain.
      validators: [Validators.pattern(/^\s*[A-Za-z0-9](?:[A-Za-z0-9.-]{0,126}[A-Za-z0-9])?\s*$/)],
    }),
    tenantIssuer: this.fb.control('', { nonNullable: true }),
    tenantAuthorizationEndpoint: this.fb.control('', { nonNullable: true }),
    tenantTokenEndpoint: this.fb.control('', { nonNullable: true }),
    scopes: this.fb.control('', { nonNullable: true }),
    allowedRoles: this.fb.control<string[]>(['*'], { nonNullable: true }),
    grantAllRoles: this.fb.control(true, { nonNullable: true }),
    enabled: this.fb.control(true, { nonNullable: true }),
    iconName: this.fb.control('heroLink', { nonNullable: true }),
    iconData: this.fb.control('', { nonNullable: true }),
    customParameters: this.fb.control('', { nonNullable: true }),
    fileSourceAdapterId: this.fb.control('', { nonNullable: true }),
    exportTargetAdapterId: this.fb.control('', { nonNullable: true }),
  });

  readonly pageTitle = computed(() => (this.isEditMode() ? 'Edit Connector' : 'Add Connector'));

  readonly availableRoles = computed(() => this.roleOptionsService.getEnabledRoles());

  readonly selectedRoles = signal<string[]>(['*']);

  // Form controls aren't observable signals, so mirror providerType into a
  // signal updated from valueChanges. This drives the template's @if for
  // discovery, the placeholder lookups, and the submit-time discovery
  // gating.
  readonly needsDiscovery = signal(
    requiresDiscovery(this.connectorForm.controls.providerType.value)
  );

  /** True for built-in vendors that need the tenant's issuer and endpoints. */
  readonly needsTenantEndpoints = signal(
    requiresTenantEndpoints(this.connectorForm.controls.providerType.value)
  );

  /** Mirrors `providerType` so computed placeholders react to changes. */
  protected readonly providerTypeSignal = signal<ConnectorType>(
    this.connectorForm.controls.providerType.value,
  );

  /** Only Microsoft connectors take an Entra tenant. */
  readonly isMicrosoft = computed(() => this.providerTypeSignal() === 'microsoft');

  /** Vendor-relevant scopes example shown when the field is empty. */
  readonly scopesPlaceholder = computed<string>(() => {
    const preset = getConnectorPreset(this.providerTypeSignal());
    return preset?.scopesPlaceholder ?? 'openid, email, profile';
  });

  /** Issuer example for the selected tenant vendor. */
  readonly tenantIssuerPlaceholder = computed<string>(
    () => TENANT_ISSUER_PLACEHOLDERS[this.providerTypeSignal()] ?? 'https://your-tenant.example.com',
  );

  /**
   * Vendor-relevant `key=value` example for the custom-parameters
   * textarea. Generic `key=value` fallback for vendors with no
   * commonly-used extras.
   */
  readonly customParametersPlaceholder = computed<string>(() => {
    const preset = getConnectorPreset(this.providerTypeSignal());
    return preset?.customParametersPlaceholder ?? 'key=value';
  });

  // Form controls aren't signals, so mirror the two values the file-source
  // section reacts to. Updated from valueChanges in ngOnInit.
  private readonly scopesSignal = signal<string>('');
  private readonly fileSourceAdapterIdSignal = signal<string>('');
  private readonly exportTargetAdapterIdSignal = signal<string>('');

  /**
   * The file-source adapter mapping loaded from the server, so update can
   * tell a real change (send it, possibly `''` to clear) from a no-op.
   */
  private readonly adapterLoadedFromServer = signal<string>('');

  /** Same tri-state tracking for the export-target mapping. */
  private readonly exportAdapterLoadedFromServer = signal<string>('');

  /**
   * The discovery URL loaded from the server. Update only sends the field
   * when it actually changed: the backend treats a discovery change as a
   * credential-rotation event, and the admin cannot rotate (the client
   * secret is never readable back), so resending an unchanged URL would
   * block every metadata-only edit on a discovery-URL connector.
   */
  private readonly discoveryLoadedFromServer = signal<string>('');
  /** Tenant endpoints as loaded, to send them only when actually changed. */
  private readonly tenantEndpointsLoadedFromServer = signal<string>('');

  /** The tenant loaded from the server, for the same reason as discovery. */
  private readonly tenantLoadedFromServer = signal<string>('');

  /** Every file-source adapter shipped in the backend registry. */
  readonly fileSourceAdapters = computed(() =>
    this.connectorsService.getFileSourceAdapters()
  );

  /** Adapters that may be mapped to the currently-selected provider type. */
  readonly compatibleAdapters = computed(() =>
    this.fileSourceAdapters().filter(a =>
      a.compatibleProviderTypes.includes(this.providerTypeSignal())
    )
  );

  /** The adapter currently chosen in the dropdown, if any. */
  readonly selectedAdapter = computed(() => {
    const key = this.fileSourceAdapterIdSignal();
    if (!key) return null;
    return this.fileSourceAdapters().find(a => a.key === key) ?? null;
  });

  /**
   * Warns when the connector's scopes don't cover what the selected adapter
   * needs — a silent empty browse at runtime, caught here at config time.
   */
  readonly scopeCoverageWarning = computed<string | null>(() => {
    const adapter = this.selectedAdapter();
    if (!adapter) return null;
    const granted = new Set(
      this.scopesSignal()
        .split(',')
        .map(s => s.trim())
        .filter(Boolean)
    );
    const missing = adapter.requiredScopes.filter(s => !granted.has(s));
    if (missing.length === 0) return null;
    return (
      `This connector's scopes are missing ${missing.join(', ')}, which ` +
      `${adapter.displayName} needs to read files. Add them above or ` +
      `browsing will return nothing.`
    );
  });

  /** Every export-target adapter shipped in the backend registry. */
  readonly exportTargetAdapters = computed(() =>
    this.connectorsService.getExportTargetAdapters()
  );

  /** Adapters that may be mapped to the currently-selected provider type. */
  readonly compatibleExportAdapters = computed(() =>
    this.exportTargetAdapters().filter(a =>
      a.compatibleProviderTypes.includes(this.providerTypeSignal())
    )
  );

  /** The export-target adapter currently chosen in the dropdown, if any. */
  readonly selectedExportAdapter = computed(() => {
    const key = this.exportTargetAdapterIdSignal();
    if (!key) return null;
    return this.exportTargetAdapters().find(a => a.key === key) ?? null;
  });

  /**
   * Warns when the connector's scopes don't cover the write scope the
   * selected export target needs — caught here at config time rather than as
   * a failed save later.
   */
  readonly exportScopeCoverageWarning = computed<string | null>(() => {
    const adapter = this.selectedExportAdapter();
    if (!adapter) return null;
    const granted = new Set(
      this.scopesSignal()
        .split(',')
        .map(s => s.trim())
        .filter(Boolean)
    );
    const missing = adapter.requiredScopes.filter(s => !granted.has(s));
    if (missing.length === 0) return null;
    return (
      `This connector's scopes are missing ${missing.join(', ')}, which ` +
      `${adapter.displayName} needs to save files. Add them above or ` +
      `saving will fail.`
    );
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
      if (value !== 'microsoft') {
        this.connectorForm.controls.tenantId.setValue('');
      }
      this.applyDiscoveryValidator();
      this.clearIncompatibleAdapter();
    });

    this.connectorForm.controls.iconData.valueChanges.subscribe((value) => {
      this.iconDataSignal.set(value);
    });

    this.connectorForm.controls.scopes.valueChanges.subscribe((value) => {
      this.scopesSignal.set(value);
    });
    this.connectorForm.controls.fileSourceAdapterId.valueChanges.subscribe((value) => {
      this.fileSourceAdapterIdSignal.set(value);
    });
    this.connectorForm.controls.exportTargetAdapterId.valueChanges.subscribe((value) => {
      this.exportTargetAdapterIdSignal.set(value);
    });
  }

  /**
   * Drop the file-source mapping when the provider type changes such that
   * the selected adapter is no longer compatible (e.g. switching a Google
   * connector to Slack). Keeps the form from submitting an invalid mapping.
   */
  private clearIncompatibleAdapter(): void {
    const providerType = this.connectorForm.controls.providerType.value;

    const currentFileSource = this.connectorForm.controls.fileSourceAdapterId.value;
    if (currentFileSource) {
      const adapter = this.connectorsService
        .getFileSourceAdapters()
        .find(a => a.key === currentFileSource);
      if (!adapter?.compatibleProviderTypes.includes(providerType)) {
        this.connectorForm.controls.fileSourceAdapterId.setValue('');
      }
    }

    const currentExport = this.connectorForm.controls.exportTargetAdapterId.value;
    if (currentExport) {
      const adapter = this.connectorsService
        .getExportTargetAdapters()
        .find(a => a.key === currentExport);
      if (!adapter?.compatibleProviderTypes.includes(providerType)) {
        this.connectorForm.controls.exportTargetAdapterId.setValue('');
      }
    }
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
    this.applyTenantValidator();
  }

  private applyTenantValidator(): void {
    const needs = requiresTenantEndpoints(this.connectorForm.controls.providerType.value);
    this.needsTenantEndpoints.set(needs);
    const controls = [
      this.connectorForm.controls.tenantIssuer,
      this.connectorForm.controls.tenantAuthorizationEndpoint,
      this.connectorForm.controls.tenantTokenEndpoint,
    ];
    for (const ctrl of controls) {
      if (needs) {
        ctrl.setValidators([Validators.required, Validators.pattern(/^https:\/\/.+/)]);
      } else {
        ctrl.clearValidators();
        ctrl.setValue('');
      }
      ctrl.updateValueAndValidity({ emitEvent: false });
    }
  }

  /** The three tenant fields as the API's `authorizationServerMetadata`. */
  private tenantMetadata(): Record<string, string> {
    const v = this.connectorForm.getRawValue();
    return {
      issuer: v.tenantIssuer.trim(),
      authorizationEndpoint: v.tenantAuthorizationEndpoint.trim(),
      tokenEndpoint: v.tenantTokenEndpoint.trim(),
    };
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
        tenantId: connector.tenantId ?? '',
        tenantIssuer: String(connector.authorizationServerMetadata?.['issuer'] ?? ''),
        tenantAuthorizationEndpoint: String(
          connector.authorizationServerMetadata?.['authorizationEndpoint'] ?? '',
        ),
        tenantTokenEndpoint: String(connector.authorizationServerMetadata?.['tokenEndpoint'] ?? ''),
        scopes: connector.scopes.join(', '),
        allowedRoles: connector.allowedRoles.length > 0 ? connector.allowedRoles : ['*'],
        grantAllRoles: connector.allowedRoles.length === 0,
        enabled: connector.enabled,
        iconName: connector.iconName || 'heroLink',
        iconData: connector.iconData ?? '',
        customParameters: this.serializeCustomParameters(connector.customParameters ?? null),
        fileSourceAdapterId: connector.fileSourceAdapterId ?? '',
        exportTargetAdapterId: connector.exportTargetAdapterId ?? '',
      });
      this.iconLoadedFromServer.set(connector.iconData ?? null);
      this.adapterLoadedFromServer.set(connector.fileSourceAdapterId ?? '');
      this.exportAdapterLoadedFromServer.set(connector.exportTargetAdapterId ?? '');
      this.discoveryLoadedFromServer.set(connector.oauthDiscoveryUrl ?? '');
      this.tenantLoadedFromServer.set(connector.tenantId ?? '');
      this.applyDiscoveryValidator();
      this.tenantEndpointsLoadedFromServer.set(
        requiresTenantEndpoints(connector.providerType) ? JSON.stringify(this.tenantMetadata()) : '',
      );
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
      this.prefillBrandIcon(type);
    }
    this.applyDiscoveryValidator();
  }

  /** `true` for `aria-invalid` once a touched control fails, else `null` so the attribute is omitted. */
  isInvalid(control: AbstractControl): true | null {
    return control.invalid && control.touched ? true : null;
  }

  /**
   * Fill the icon with the vendor's logo when picking a preset. Only touches
   * the field while it is empty or still holds the logo filled in earlier, so
   * switching presets swaps logos but never replaces an admin's own upload.
   */
  private prefillBrandIcon(type: ConnectorType): void {
    const control = this.connectorForm.controls.iconData;
    const current = control.value;
    if (current && current !== this.prefilledIcon()) return;

    const next = getBrandIconDataUrl(type) ?? '';
    control.setValue(next);
    this.prefilledIcon.set(next || null);
    this.iconUploadError.set(null);
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
        // Tri-state for the file-source mapping: send only on a real change.
        // `''` clears an existing mapping; a key sets it; unchanged → omit.
        const currentAdapter = formValue.fileSourceAdapterId || '';
        if (currentAdapter !== this.adapterLoadedFromServer()) {
          updates.fileSourceAdapterId = currentAdapter;
        }
        // Same tri-state for the export-target mapping.
        const currentExportAdapter = formValue.exportTargetAdapterId || '';
        if (currentExportAdapter !== this.exportAdapterLoadedFromServer()) {
          updates.exportTargetAdapterId = currentExportAdapter;
        }
        if (formValue.clientId && formValue.clientSecret) {
          updates.clientId = formValue.clientId;
          updates.clientSecret = formValue.clientSecret;
        }
        // Only send the discovery URL when the admin actually changed it —
        // the backend requires a credential rotation alongside a discovery
        // change, so an unchanged value would reject the whole save.
        const currentDiscovery = formValue.oauthDiscoveryUrl || '';
        if (
          this.needsDiscovery() &&
          currentDiscovery &&
          currentDiscovery !== this.discoveryLoadedFromServer()
        ) {
          updates.oauthDiscoveryUrl = currentDiscovery;
        }
        // Tenant: same rule, except `''` is a real value (it clears one).
        const currentTenant = formValue.tenantId.trim();
        if (this.isMicrosoft() && currentTenant !== this.tenantLoadedFromServer()) {
          updates.tenantId = currentTenant;
        }
        // Same rule for tenant endpoints: the backend only accepts a change
        // alongside a credential rotation, so omit them when untouched.
        if (
          this.needsTenantEndpoints() &&
          JSON.stringify(this.tenantMetadata()) !== this.tenantEndpointsLoadedFromServer()
        ) {
          updates.authorizationServerMetadata = this.tenantMetadata();
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
        if (this.isMicrosoft() && formValue.tenantId.trim()) {
          createData.tenantId = formValue.tenantId.trim();
        }
        if (this.needsTenantEndpoints()) {
          createData.authorizationServerMetadata = this.tenantMetadata();
        }
        if (Object.keys(customParameters).length > 0) {
          createData.customParameters = customParameters;
        }
        if (formValue.fileSourceAdapterId) {
          createData.fileSourceAdapterId = formValue.fileSourceAdapterId;
        }
        if (formValue.exportTargetAdapterId) {
          createData.exportTargetAdapterId = formValue.exportTargetAdapterId;
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
