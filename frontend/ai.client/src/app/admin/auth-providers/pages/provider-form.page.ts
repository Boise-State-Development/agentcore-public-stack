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
  heroArrowPath,
  heroInformationCircle,
} from '@ng-icons/heroicons/outline';
import { AuthProvidersService } from '../services/auth-providers.service';
import {
  AuthProviderCreateRequest,
  AuthProviderUpdateRequest,
  OIDCDiscoveryResponse,
} from '../models/auth-provider.model';

interface ProviderFormGroup {
  providerId: FormControl<string>;
  displayName: FormControl<string>;
  providerType: FormControl<string>;
  enabled: FormControl<boolean>;
  issuerUrl: FormControl<string>;
  clientId: FormControl<string>;
  clientSecret: FormControl<string>;
  authorizationEndpoint: FormControl<string>;
  tokenEndpoint: FormControl<string>;
  jwksUri: FormControl<string>;
  userinfoEndpoint: FormControl<string>;
  endSessionEndpoint: FormControl<string>;
  scopes: FormControl<string>;
  responseType: FormControl<string>;
  pkceEnabled: FormControl<boolean>;
  redirectUri: FormControl<string>;
  userIdClaim: FormControl<string>;
  emailClaim: FormControl<string>;
  nameClaim: FormControl<string>;
  rolesClaim: FormControl<string>;
  pictureClaim: FormControl<string>;
  firstNameClaim: FormControl<string>;
  lastNameClaim: FormControl<string>;
  userIdPattern: FormControl<string>;
  requiredScopes: FormControl<string>;
  allowedAudiences: FormControl<string>;
  logoUrl: FormControl<string>;
  buttonColor: FormControl<string>;
}

@Component({
  selector: 'app-auth-provider-form',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReactiveFormsModule, NgIcon],
  providers: [
    provideIcons({ heroArrowLeft, heroArrowPath, heroInformationCircle }),
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
          Back to Auth Providers
        </button>

        <!-- Page Header -->
        <div class="mb-8">
          <h1 class="text-3xl/9 font-bold text-gray-900 dark:text-white">
            {{ pageTitle() }}
          </h1>
          <p class="mt-2 text-base/7 text-gray-600 dark:text-gray-400">
            {{ isEditMode() ? 'Update authentication provider settings' : 'Configure a new OIDC authentication provider' }}
          </p>
        </div>

        <!-- Loading State -->
        @if (loading()) {
          <div class="flex h-64 items-center justify-center">
            <div class="flex flex-col items-center gap-4">
              <div
                class="size-12 animate-spin rounded-full border-4 border-gray-300 border-t-blue-600 dark:border-t-blue-400 dark:border-gray-600"
              ></div>
              <p class="text-sm text-gray-500 dark:text-gray-400">
                Loading provider...
              </p>
            </div>
          </div>
        } @else {
          <!-- Form -->
          <form [formGroup]="providerForm" (ngSubmit)="onSubmit()" class="space-y-8">

            <!-- Basic Information -->
            <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
              <h2 class="mb-6 text-xl/8 font-semibold text-gray-900 dark:text-white">
                Basic Information
              </h2>

              <div class="space-y-4">
                <!-- Provider ID -->
                <div>
                  <label for="providerId" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Provider ID <span class="text-red-600">*</span>
                  </label>
                  <input
                    type="text"
                    id="providerId"
                    formControlName="providerId"
                    placeholder="e.g., entra-id, okta-prod, google"
                    [readonly]="isEditMode()"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500 read-only:bg-gray-100 read-only:dark:bg-gray-600"
                    [class.border-red-500]="providerForm.controls.providerId.invalid && providerForm.controls.providerId.touched"
                  />
                  <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                    Lowercase letters, numbers, and hyphens only.
                  </p>
                  @if (providerForm.controls.providerId.invalid && providerForm.controls.providerId.touched) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">
                      @if (providerForm.controls.providerId.errors?.['required']) {
                        Provider ID is required
                      } @else if (providerForm.controls.providerId.errors?.['pattern']) {
                        Must be lowercase letters, numbers, and hyphens only
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
                    placeholder="e.g., Microsoft Entra ID, Okta, Google"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                    [class.border-red-500]="providerForm.controls.displayName.invalid && providerForm.controls.displayName.touched"
                  />
                  @if (providerForm.controls.displayName.invalid && providerForm.controls.displayName.touched) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">Display name is required</p>
                  }
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
                    Provider Enabled
                  </label>
                </div>
              </div>
            </div>

            <!-- OIDC Configuration -->
            <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
              <h2 class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">
                OIDC Configuration
              </h2>
              <p class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                Core OIDC settings. Enter the issuer URL and click Discover to auto-fill endpoints.
              </p>

              <!-- Cognito Redirect URI Helper -->
              @if (cognitoRedirectUri()) {
                <div class="mb-6 rounded-sm border border-blue-200 bg-blue-50 p-4 dark:border-blue-800 dark:bg-blue-900/20">
                  <div class="flex items-start gap-3">
                    <ng-icon name="heroInformationCircle" class="mt-0.5 size-5 shrink-0 text-blue-600 dark:text-blue-400" />
                    <div class="min-w-0 flex-1">
                      <p class="text-sm/6 font-medium text-blue-800 dark:text-blue-200">
                        Required: Add this Redirect URI to your identity provider
                      </p>
                      <p class="mt-1 text-xs/5 text-blue-700 dark:text-blue-300">
                        In your IdP's app registration (e.g., Azure Portal, Okta Admin), add the following as an allowed redirect URI:
                      </p>
                      <div class="mt-2 flex items-center gap-2">
                        <code class="block flex-1 truncate rounded-xs bg-blue-100 px-3 py-1.5 font-mono text-sm text-blue-900 dark:bg-blue-800/40 dark:text-blue-100">
                          {{ cognitoRedirectUri() }}
                        </code>
                        <button
                          type="button"
                          (click)="copyRedirectUri()"
                          class="shrink-0 rounded-sm border border-blue-300 bg-white px-3 py-1.5 text-xs font-medium text-blue-700 hover:bg-blue-50 dark:border-blue-600 dark:bg-blue-800/40 dark:text-blue-200 dark:hover:bg-blue-800/60"
                        >
                          {{ copiedRedirectUri() ? 'Copied!' : 'Copy' }}
                        </button>
                      </div>
                    </div>
                  </div>
                </div>
              }

              <div class="space-y-4">
                <!-- Issuer URL with Discover -->
                <div>
                  <label for="issuerUrl" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Issuer URL <span class="text-red-600">*</span>
                  </label>
                  <div class="mt-1 flex gap-2">
                    <input
                      type="url"
                      id="issuerUrl"
                      formControlName="issuerUrl"
                      placeholder="https://login.microsoftonline.com/{tenant}/v2.0"
                      class="block flex-1 rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                      [class.border-red-500]="providerForm.controls.issuerUrl.invalid && providerForm.controls.issuerUrl.touched"
                    />
                    <button
                      type="button"
                      (click)="discoverEndpoints()"
                      [disabled]="discovering() || !providerForm.controls.issuerUrl.value"
                      class="inline-flex items-center gap-2 rounded-sm border border-gray-300 bg-white px-4 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
                    >
                      <ng-icon
                        name="heroArrowPath"
                        class="size-4"
                        [class.animate-spin]="discovering()"
                      />
                      {{ discovering() ? 'Discovering...' : 'Discover' }}
                    </button>
                  </div>
                  @if (providerForm.controls.issuerUrl.invalid && providerForm.controls.issuerUrl.touched) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">Issuer URL is required</p>
                  }
                  @if (discoveryResult()) {
                    <p class="mt-1 text-sm/6 text-green-600 dark:text-green-400">
                      Endpoints discovered successfully
                    </p>
                  }
                  @if (discoveryError()) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">
                      {{ discoveryError() }}
                    </p>
                  }
                </div>

                <!-- Client ID -->
                <div>
                  <label for="clientId" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Client ID <span class="text-red-600">*</span>
                  </label>
                  <input
                    type="text"
                    id="clientId"
                    formControlName="clientId"
                    placeholder="Application/Client ID from your identity provider"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                    [class.border-red-500]="providerForm.controls.clientId.invalid && providerForm.controls.clientId.touched"
                  />
                  @if (providerForm.controls.clientId.invalid && providerForm.controls.clientId.touched) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">Client ID is required</p>
                  }
                </div>

                <!-- Client Secret -->
                <div>
                  <label for="clientSecret" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Client Secret
                    @if (!isEditMode()) {
                      <span class="text-red-600">*</span>
                    }
                  </label>
                  <input
                    type="password"
                    id="clientSecret"
                    formControlName="clientSecret"
                    [placeholder]="isEditMode() ? 'Leave empty to keep current secret' : 'Client secret from your identity provider'"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                  @if (isEditMode()) {
                    <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                      Only fill this if you want to rotate the client secret.
                    </p>
                  }
                </div>

                <!-- Scopes -->
                <div>
                  <label for="scopes" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Scopes
                  </label>
                  <input
                    type="text"
                    id="scopes"
                    formControlName="scopes"
                    placeholder="openid profile email"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                  <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                    Space-separated OAuth scopes.
                  </p>
                </div>

                <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
                  <!-- PKCE Enabled -->
                  <div class="flex items-center gap-3">
                    <input
                      type="checkbox"
                      id="pkceEnabled"
                      formControlName="pkceEnabled"
                      class="size-4 rounded-xs border-gray-300 text-blue-600 focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700"
                    />
                    <label for="pkceEnabled" class="text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                      PKCE Enabled (recommended)
                    </label>
                  </div>

                  <!-- Redirect URI -->
                  <div>
                    <label for="redirectUri" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                      Redirect URI Override
                    </label>
                    <input
                      type="url"
                      id="redirectUri"
                      formControlName="redirectUri"
                      placeholder="Leave empty for default"
                      class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                    />
                  </div>
                </div>
              </div>
            </div>

            <!-- Endpoints (auto-discovered or manual override) -->
            <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
              <h2 class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">
                Endpoints
              </h2>
              <p class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                These are auto-populated from OIDC Discovery. Override only if needed.
              </p>

              <div class="space-y-4">
                <div>
                  <label for="authorizationEndpoint" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Authorization Endpoint
                  </label>
                  <input
                    type="url"
                    id="authorizationEndpoint"
                    formControlName="authorizationEndpoint"
                    placeholder="Auto-discovered from issuer"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                </div>

                <div>
                  <label for="tokenEndpoint" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Token Endpoint
                  </label>
                  <input
                    type="url"
                    id="tokenEndpoint"
                    formControlName="tokenEndpoint"
                    placeholder="Auto-discovered from issuer"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                </div>

                <div>
                  <label for="jwksUri" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    JWKS URI
                  </label>
                  <input
                    type="url"
                    id="jwksUri"
                    formControlName="jwksUri"
                    placeholder="Auto-discovered from issuer"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                </div>

                <div>
                  <label for="userinfoEndpoint" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    UserInfo Endpoint
                  </label>
                  <input
                    type="url"
                    id="userinfoEndpoint"
                    formControlName="userinfoEndpoint"
                    placeholder="Auto-discovered from issuer"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                </div>

                <div>
                  <label for="endSessionEndpoint" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    End Session Endpoint
                  </label>
                  <input
                    type="url"
                    id="endSessionEndpoint"
                    formControlName="endSessionEndpoint"
                    placeholder="Auto-discovered from issuer"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                </div>
              </div>
            </div>

            <!-- Claim Mappings -->
            <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
              <h2 class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">
                JWT Claim Mappings
              </h2>
              <p class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                Map JWT token claims to user fields. These determine how user identity is extracted from tokens.
              </p>

              <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div>
                  <label for="userIdClaim" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    User ID Claim
                  </label>
                  <input
                    type="text"
                    id="userIdClaim"
                    formControlName="userIdClaim"
                    placeholder="sub"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                  <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                    Supports URI-style claims (e.g., http://schemas.example.com/claims/id)
                  </p>
                </div>

                <div>
                  <label for="emailClaim" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Email Claim
                  </label>
                  <input
                    type="text"
                    id="emailClaim"
                    formControlName="emailClaim"
                    placeholder="email"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                </div>

                <div>
                  <label for="nameClaim" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Full Name Claim
                  </label>
                  <input
                    type="text"
                    id="nameClaim"
                    formControlName="nameClaim"
                    placeholder="name"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                </div>

                <div>
                  <label for="rolesClaim" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Roles Claim
                  </label>
                  <input
                    type="text"
                    id="rolesClaim"
                    formControlName="rolesClaim"
                    placeholder="roles"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                </div>

                <div>
                  <label for="pictureClaim" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Picture Claim
                  </label>
                  <input
                    type="text"
                    id="pictureClaim"
                    formControlName="pictureClaim"
                    placeholder="picture"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                </div>

                <div>
                  <label for="firstNameClaim" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    First Name Claim
                  </label>
                  <input
                    type="text"
                    id="firstNameClaim"
                    formControlName="firstNameClaim"
                    placeholder="given_name"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                </div>

                <div>
                  <label for="lastNameClaim" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Last Name Claim
                  </label>
                  <input
                    type="text"
                    id="lastNameClaim"
                    formControlName="lastNameClaim"
                    placeholder="family_name"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                  <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                    Used as fallback when the full name claim is empty.
                  </p>
                </div>
              </div>
            </div>

            <!-- Validation Rules -->
            <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
              <h2 class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">
                Validation Rules
              </h2>
              <p class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                Optional validation constraints for tokens from this provider.
              </p>

              <div class="space-y-4">
                <div>
                  <label for="userIdPattern" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    User ID Pattern (regex)
                  </label>
                  <input
                    type="text"
                    id="userIdPattern"
                    formControlName="userIdPattern"
                    placeholder="e.g., ^\\d{9}$ for 9-digit IDs"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 font-mono text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                  <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                    If set, user IDs must match this regex pattern during JWT validation.
                  </p>
                </div>

                <div>
                  <label for="allowedAudiences" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Allowed Audiences
                  </label>
                  <input
                    type="text"
                    id="allowedAudiences"
                    formControlName="allowedAudiences"
                    placeholder="Comma-separated audience values"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                  <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                    If set, JWT audience must match one of these values.
                  </p>
                </div>

                <div>
                  <label for="requiredScopes" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Required Scopes
                  </label>
                  <input
                    type="text"
                    id="requiredScopes"
                    formControlName="requiredScopes"
                    placeholder="Comma-separated required scopes"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                </div>
              </div>
            </div>

            <!-- Appearance -->
            <div class="rounded-sm border border-gray-300 bg-white p-6 dark:border-gray-600 dark:bg-gray-800">
              <h2 class="mb-2 text-xl/8 font-semibold text-gray-900 dark:text-white">
                Appearance
              </h2>
              <p class="mb-6 text-sm/6 text-gray-600 dark:text-gray-400">
                Customize how this provider appears on the login page.
              </p>

              <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
                <div>
                  <label for="logoUrl" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Logo URL
                  </label>
                  <input
                    type="url"
                    id="logoUrl"
                    formControlName="logoUrl"
                    placeholder="https://example.com/logo.svg"
                    class="mt-1 block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                  />
                </div>

                <div>
                  <label for="buttonColor" class="block text-sm/6 font-medium text-gray-700 dark:text-gray-300">
                    Button Color
                  </label>
                  <div class="mt-1 flex items-center gap-3">
                    <input
                      type="text"
                      id="buttonColor"
                      formControlName="buttonColor"
                      placeholder="#0078D4"
                      maxlength="7"
                      class="block w-full rounded-sm border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 placeholder:text-gray-400 focus:border-blue-500 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 dark:border-gray-600 dark:bg-gray-700 dark:text-white dark:placeholder:text-gray-500"
                      [class.border-red-500]="providerForm.controls.buttonColor.invalid && providerForm.controls.buttonColor.touched"
                    />
                    @if (providerForm.controls.buttonColor.value) {
                      <div
                        class="size-8 shrink-0 rounded-xs border border-gray-300 dark:border-gray-600"
                        [style.background-color]="providerForm.controls.buttonColor.value"
                      ></div>
                    }
                  </div>
                  <p class="mt-1 text-xs/5 text-gray-500 dark:text-gray-400">
                    Hex color code (e.g., #0078D4 for Microsoft blue).
                  </p>
                  @if (providerForm.controls.buttonColor.invalid && providerForm.controls.buttonColor.touched) {
                    <p class="mt-1 text-sm/6 text-red-600 dark:text-red-400">
                      Must be a valid hex color (e.g., #0078D4)
                    </p>
                  }
                </div>
              </div>
            </div>

            <!-- Form Actions -->
            <div class="flex gap-3 border-t border-gray-200 pt-6 dark:border-gray-700">
              <button
                type="submit"
                [disabled]="isSubmitting() || providerForm.invalid"
                class="rounded-sm bg-blue-600 px-6 py-2 text-sm/6 font-medium text-white hover:bg-blue-700 focus:outline-hidden focus:ring-3 focus:ring-blue-500/50 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
              >
                @if (isSubmitting()) {
                  Saving...
                } @else {
                  {{ isEditMode() ? 'Update Provider' : 'Create Provider' }}
                }
              </button>
              <button
                type="button"
                (click)="goBack()"
                [disabled]="isSubmitting()"
                class="rounded-sm border border-gray-300 bg-white px-6 py-2 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus:outline-hidden focus:ring-3 focus:ring-gray-500/50 disabled:cursor-not-allowed disabled:opacity-50 dark:border-gray-600 dark:bg-gray-700 dark:text-gray-300 dark:hover:bg-gray-600"
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
export class AuthProviderFormPage implements OnInit {
  private fb = inject(FormBuilder);
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private authProvidersService = inject(AuthProvidersService);

  readonly isEditMode = signal(false);
  readonly providerId = signal<string | null>(null);
  readonly isSubmitting = signal(false);
  readonly loading = signal(false);
  readonly discovering = signal(false);
  readonly discoveryResult = signal<OIDCDiscoveryResponse | null>(null);
  readonly discoveryError = signal<string | null>(null);
  readonly copiedRedirectUri = signal(false);

  /** The Cognito redirect URI that must be registered in the external IdP. Fetched in ngOnInit; empty until then or if app-api lacks COGNITO_DOMAIN_URL. */
  readonly cognitoRedirectUri = signal('');

  readonly providerForm: FormGroup<ProviderFormGroup> = this.fb.group({
    providerId: this.fb.control('', {
      nonNullable: true,
      validators: [
        Validators.required,
        Validators.pattern(/^[a-z0-9][a-z0-9-]*$/),
      ],
    }),
    displayName: this.fb.control('', {
      nonNullable: true,
      validators: [Validators.required, Validators.maxLength(128)],
    }),
    providerType: this.fb.control('oidc', { nonNullable: true }),
    enabled: this.fb.control(true, { nonNullable: true }),
    issuerUrl: this.fb.control('', {
      nonNullable: true,
      validators: [Validators.required],
    }),
    clientId: this.fb.control('', {
      nonNullable: true,
      validators: [Validators.required],
    }),
    clientSecret: this.fb.control('', { nonNullable: true }),
    authorizationEndpoint: this.fb.control('', { nonNullable: true }),
    tokenEndpoint: this.fb.control('', { nonNullable: true }),
    jwksUri: this.fb.control('', { nonNullable: true }),
    userinfoEndpoint: this.fb.control('', { nonNullable: true }),
    endSessionEndpoint: this.fb.control('', { nonNullable: true }),
    scopes: this.fb.control('openid profile email', { nonNullable: true }),
    responseType: this.fb.control('code', { nonNullable: true }),
    pkceEnabled: this.fb.control(true, { nonNullable: true }),
    redirectUri: this.fb.control('', { nonNullable: true }),
    userIdClaim: this.fb.control('sub', { nonNullable: true }),
    emailClaim: this.fb.control('email', { nonNullable: true }),
    nameClaim: this.fb.control('name', { nonNullable: true }),
    rolesClaim: this.fb.control('roles', { nonNullable: true }),
    pictureClaim: this.fb.control('picture', { nonNullable: true }),
    firstNameClaim: this.fb.control('given_name', { nonNullable: true }),
    lastNameClaim: this.fb.control('family_name', { nonNullable: true }),
    userIdPattern: this.fb.control('', { nonNullable: true }),
    requiredScopes: this.fb.control('', { nonNullable: true }),
    allowedAudiences: this.fb.control('', { nonNullable: true }),
    logoUrl: this.fb.control('', { nonNullable: true }),
    buttonColor: this.fb.control('', {
      nonNullable: true,
      validators: [Validators.pattern(/^(#[0-9a-fA-F]{6})?$/)],
    }),
  });

  readonly pageTitle = computed(() =>
    this.isEditMode() ? 'Edit Auth Provider' : 'Add Auth Provider'
  );

  ngOnInit(): void {
    this.loadCognitoRedirectUri();

    const id = this.route.snapshot.paramMap.get('providerId');
    if (id && id !== 'new') {
      this.isEditMode.set(true);
      this.providerId.set(id);
      // Client secret not required in edit mode
      this.providerForm.controls.clientSecret.clearValidators();
      this.providerForm.controls.clientSecret.updateValueAndValidity();
      this.loadProviderData(id);
    } else {
      // Client secret required in create mode
      this.providerForm.controls.clientSecret.setValidators([Validators.required]);
      this.providerForm.controls.clientSecret.updateValueAndValidity();
    }
  }

  private async loadCognitoRedirectUri(): Promise<void> {
    try {
      const uri = await this.authProvidersService.getCognitoRedirectUri();
      this.cognitoRedirectUri.set(uri);
    } catch {
      // Leaves the signal empty; the template hides the redirect-URI section
      // via @if. Happens when app-api has no COGNITO_DOMAIN_URL (e.g. local dev).
    }
  }

  private async loadProviderData(id: string): Promise<void> {
    this.loading.set(true);
    try {
      const provider = await this.authProvidersService.fetchProvider(id);
      this.providerForm.patchValue({
        providerId: provider.provider_id,
        displayName: provider.display_name,
        providerType: provider.provider_type,
        enabled: provider.enabled,
        issuerUrl: provider.issuer_url,
        clientId: provider.client_id,
        clientSecret: '',
        authorizationEndpoint: provider.authorization_endpoint ?? '',
        tokenEndpoint: provider.token_endpoint ?? '',
        jwksUri: provider.jwks_uri ?? '',
        userinfoEndpoint: provider.userinfo_endpoint ?? '',
        endSessionEndpoint: provider.end_session_endpoint ?? '',
        scopes: provider.scopes,
        responseType: provider.response_type,
        pkceEnabled: provider.pkce_enabled,
        redirectUri: provider.redirect_uri ?? '',
        userIdClaim: provider.user_id_claim,
        emailClaim: provider.email_claim,
        nameClaim: provider.name_claim,
        rolesClaim: provider.roles_claim,
        pictureClaim: provider.picture_claim ?? '',
        firstNameClaim: provider.first_name_claim ?? '',
        lastNameClaim: provider.last_name_claim ?? '',
        userIdPattern: provider.user_id_pattern ?? '',
        requiredScopes: provider.required_scopes?.join(', ') ?? '',
        allowedAudiences: provider.allowed_audiences?.join(', ') ?? '',
        logoUrl: provider.logo_url ?? '',
        buttonColor: provider.button_color ?? '',
      });
    } catch (error) {
      console.error('Error loading provider:', error);
      alert('Failed to load provider. Returning to list.');
      this.router.navigate(['/admin/auth-providers']);
    } finally {
      this.loading.set(false);
    }
  }

  async discoverEndpoints(): Promise<void> {
    const issuerUrl = this.providerForm.controls.issuerUrl.value;
    if (!issuerUrl) return;

    this.discovering.set(true);
    this.discoveryResult.set(null);
    this.discoveryError.set(null);

    try {
      const result = await this.authProvidersService.discoverEndpoints(issuerUrl);
      this.discoveryResult.set(result);

      // Auto-fill endpoint fields
      this.providerForm.patchValue({
        authorizationEndpoint: result.authorization_endpoint ?? '',
        tokenEndpoint: result.token_endpoint ?? '',
        jwksUri: result.jwks_uri ?? '',
        userinfoEndpoint: result.userinfo_endpoint ?? '',
        endSessionEndpoint: result.end_session_endpoint ?? '',
      });
    } catch (error: any) {
      console.error('Error discovering endpoints:', error);
      this.discoveryError.set(
        error?.error?.detail || error?.message || 'Failed to discover OIDC endpoints'
      );
    } finally {
      this.discovering.set(false);
    }
  }

  async onSubmit(): Promise<void> {
    if (this.providerForm.invalid) {
      this.providerForm.markAllAsTouched();
      return;
    }

    this.isSubmitting.set(true);

    try {
      const fv = this.providerForm.value;

      // Parse comma-separated list fields
      const parseList = (val: string | undefined): string[] | undefined => {
        if (!val?.trim()) return undefined;
        return val.split(',').map(s => s.trim()).filter(s => s.length > 0);
      };

      if (this.isEditMode() && this.providerId()) {
        const updates: AuthProviderUpdateRequest = {
          display_name: fv.displayName,
          enabled: fv.enabled,
          issuer_url: fv.issuerUrl,
          client_id: fv.clientId,
          ...(fv.clientSecret ? { client_secret: fv.clientSecret } : {}),
          authorization_endpoint: fv.authorizationEndpoint || null,
          token_endpoint: fv.tokenEndpoint || null,
          jwks_uri: fv.jwksUri || null,
          userinfo_endpoint: fv.userinfoEndpoint || null,
          end_session_endpoint: fv.endSessionEndpoint || null,
          scopes: fv.scopes,
          pkce_enabled: fv.pkceEnabled,
          redirect_uri: fv.redirectUri || null,
          user_id_claim: fv.userIdClaim,
          email_claim: fv.emailClaim,
          name_claim: fv.nameClaim,
          roles_claim: fv.rolesClaim,
          picture_claim: fv.pictureClaim || null,
          first_name_claim: fv.firstNameClaim || null,
          last_name_claim: fv.lastNameClaim || null,
          user_id_pattern: fv.userIdPattern || null,
          allowed_audiences: parseList(fv.allowedAudiences) ?? null,
          required_scopes: parseList(fv.requiredScopes) ?? null,
          logo_url: fv.logoUrl || null,
          button_color: fv.buttonColor || null,
        };
        await this.authProvidersService.updateProvider(this.providerId()!, updates);
      } else {
        const createData: AuthProviderCreateRequest = {
          provider_id: fv.providerId!,
          display_name: fv.displayName!,
          provider_type: fv.providerType ?? 'oidc',
          enabled: fv.enabled ?? true,
          issuer_url: fv.issuerUrl!,
          client_id: fv.clientId!,
          client_secret: fv.clientSecret!,
          authorization_endpoint: fv.authorizationEndpoint || null,
          token_endpoint: fv.tokenEndpoint || null,
          jwks_uri: fv.jwksUri || null,
          userinfo_endpoint: fv.userinfoEndpoint || null,
          end_session_endpoint: fv.endSessionEndpoint || null,
          scopes: fv.scopes ?? 'openid profile email',
          pkce_enabled: fv.pkceEnabled ?? true,
          redirect_uri: fv.redirectUri || null,
          user_id_claim: fv.userIdClaim ?? 'sub',
          email_claim: fv.emailClaim ?? 'email',
          name_claim: fv.nameClaim ?? 'name',
          roles_claim: fv.rolesClaim ?? 'roles',
          picture_claim: fv.pictureClaim || null,
          first_name_claim: fv.firstNameClaim || null,
          last_name_claim: fv.lastNameClaim || null,
          user_id_pattern: fv.userIdPattern || null,
          allowed_audiences: parseList(fv.allowedAudiences) ?? null,
          required_scopes: parseList(fv.requiredScopes) ?? null,
          logo_url: fv.logoUrl || null,
          button_color: fv.buttonColor || null,
        };
        await this.authProvidersService.createProvider(createData);
      }

      this.router.navigate(['/admin/auth-providers']);
    } catch (error: any) {
      console.error('Error saving provider:', error);
      const message =
        error?.error?.detail || error?.message || 'Failed to save provider.';
      alert(message);
    } finally {
      this.isSubmitting.set(false);
    }
  }

  async copyRedirectUri(): Promise<void> {
    const uri = this.cognitoRedirectUri();
    if (uri) {
      await navigator.clipboard.writeText(uri);
      this.copiedRedirectUri.set(true);
      setTimeout(() => this.copiedRedirectUri.set(false), 2000);
    }
  }

  goBack(): void {
    this.router.navigate(['/admin/auth-providers']);
  }
}
