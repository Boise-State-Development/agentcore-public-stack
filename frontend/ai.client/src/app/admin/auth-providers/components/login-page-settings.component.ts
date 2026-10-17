import { Component, ChangeDetectionStrategy, inject, signal, computed } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { AuthProvidersService } from '../services/auth-providers.service';

/**
 * The "Hide Cognito sign-in" switch on the Auth Providers page.
 *
 * Hiding is cosmetic: Cognito stays an identity provider on the app client,
 * and `/auth/login?local=1` still shows the button, which is how the
 * first-boot admin (a Cognito user) gets in if the federated provider breaks.
 * The backend refuses to hide while no provider is enabled, and shows the
 * button anyway if every provider is later disabled.
 */
@Component({
  selector: 'app-login-page-settings',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    class: 'block',
  },
  template: `
    <section
      aria-labelledby="login-page-settings-heading"
      class="rounded-xs border border-gray-200 bg-white p-4 dark:border-gray-700 dark:bg-gray-800"
    >
      <h2 id="login-page-settings-heading" class="text-base/7 font-semibold">Sign-in page</h2>

      <div class="mt-2 flex items-start justify-between gap-4">
        <div class="min-w-0">
          <p id="hide-cognito-label" class="text-sm/6 font-medium text-gray-900 dark:text-gray-100">
            Hide Cognito sign-in
          </p>
          <p id="hide-cognito-description" class="text-sm/6 text-gray-600 dark:text-gray-400">
            Show only your organization's providers on the sign-in page. Cognito users can still
            sign in at <code class="rounded-xs bg-gray-100 px-1 dark:bg-gray-700">/auth/login?local=1</code>.
          </p>
          @if (statusNote(); as note) {
            <p class="mt-1 text-sm/6 text-state-warning-800 dark:text-state-warning-300">{{ note }}</p>
          }
        </div>

        <button
          type="button"
          role="switch"
          aria-labelledby="hide-cognito-label"
          aria-describedby="hide-cognito-description"
          [attr.aria-checked]="hideRequested()"
          [disabled]="toggleDisabled()"
          (click)="toggle()"
          class="relative mt-1 inline-flex h-6 w-11 shrink-0 items-center rounded-2xl transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
          [class]="hideRequested() ? 'bg-primary-accessible' : 'bg-gray-300 dark:bg-gray-600'"
        >
          <span
            class="inline-block size-5 transform rounded-full bg-white shadow transition-transform duration-150"
            [class]="hideRequested() ? 'translate-x-5' : 'translate-x-0.5'"
          ></span>
        </button>
      </div>

      @if (errorMessage(); as message) {
        <p role="alert" class="mt-2 text-sm/6 text-state-danger-700 dark:text-state-danger-300">{{ message }}</p>
      }
    </section>
  `,
})
export class LoginPageSettingsComponent {
  private readonly authProvidersService = inject(AuthProvidersService);
  private readonly settingsResource = this.authProvidersService.loginPageSettingsResource;

  readonly saving = signal(false);
  readonly errorMessage = signal<string | null>(null);

  readonly hideRequested = computed(() => this.settingsResource.value()?.hide_cognito_login ?? false);

  private readonly enabledProviderCount = computed(
    () => this.settingsResource.value()?.enabled_provider_count ?? 0,
  );

  /** Showing is always allowed; hiding needs an enabled provider to fall back on. */
  readonly toggleDisabled = computed(
    () =>
      this.saving() ||
      !this.settingsResource.hasValue() ||
      (!this.hideRequested() && this.enabledProviderCount() === 0),
  );

  readonly statusNote = computed(() => {
    const settings = this.settingsResource.value();
    if (!settings || settings.enabled_provider_count > 0) return null;
    return settings.hide_cognito_login
      ? 'Cognito sign-in is still shown because no provider is enabled.'
      : 'Enable a provider before hiding Cognito sign-in.';
  });

  async toggle(): Promise<void> {
    this.saving.set(true);
    this.errorMessage.set(null);
    try {
      await this.authProvidersService.updateLoginPageSettings({
        hide_cognito_login: !this.hideRequested(),
      });
    } catch (error: unknown) {
      const detail = error instanceof HttpErrorResponse ? error.error?.detail : null;
      this.errorMessage.set(typeof detail === 'string' ? detail : 'Failed to save the sign-in page setting.');
    } finally {
      this.saving.set(false);
    }
  }
}
