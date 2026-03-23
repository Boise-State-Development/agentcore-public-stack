import { Component, signal, ChangeDetectionStrategy, inject, OnInit, OnDestroy } from '@angular/core';
import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { ActivatedRoute, Router } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { AuthService } from '../auth.service';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { ConfigService } from '../../services/config.service';
import { SystemService } from '../../services/system.service';

interface AuthProviderPublicInfo {
  provider_id: string;
  display_name: string;
  logo_url?: string;
  button_color?: string;
}

interface AuthProviderPublicListResponse {
  providers: AuthProviderPublicInfo[];
}

@Component({
  selector: 'app-login',
  imports: [CommonModule],
  styleUrl: './login.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <div class="fixed inset-0 flex items-center justify-center bg-gray-50 dark:bg-gray-900 overflow-y-auto">
      <div class="w-full max-w-md px-4 py-12">
        <!-- Logo -->
        <div class="mb-8 flex justify-center">
          <img
            src="/img/logo-light.png"
            alt="Logo"
            class="size-16 dark:hidden">
          <img
            src="/img/logo-dark.png"
            alt="Logo"
            class="hidden size-16 dark:block">
        </div>

        <div class="bg-white dark:bg-gray-800 rounded-lg shadow-sm p-8">
          <div class="flex flex-col items-center gap-6">
            <div class="flex flex-col items-center gap-2">
              <h1 class="text-2xl font-semibold text-gray-900 dark:text-gray-100">
                Sign In
              </h1>
              <p class="text-base/7 text-gray-600 dark:text-gray-400 text-center">
                Sign in to continue
              </p>
            </div>

            @if (errorMessage()) {
              <div class="w-full p-4 bg-red-50 dark:bg-red-900/20 border border-red-200 dark:border-red-800 rounded-lg" role="alert">
                <div class="flex items-start gap-3">
                  <svg class="size-5 text-red-600 dark:text-red-400 shrink-0 mt-0.5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                  </svg>
                  <p class="text-sm text-red-800 dark:text-red-300">
                    {{ errorMessage() }}
                  </p>
                </div>
              </div>
            }

            <!-- Login buttons -->
            <div class="w-full flex flex-col gap-3">
              <!-- Primary Cognito login button -->
              <button
                type="button"
                (click)="handleCognitoLogin()"
                [disabled]="isLoading()"
                class="w-full px-4 py-3 text-white font-medium rounded-lg transition-all duration-200 flex items-center justify-center gap-3 bg-blue-600 hover:bg-blue-700 disabled:opacity-60"
              >
                @if (isLoading() && !activeProviderId()) {
                  <div class="size-5 border-2 border-white border-t-transparent rounded-full animate-spin"></div>
                  <span>Connecting...</span>
                } @else {
                  <svg class="size-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                    <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
                  </svg>
                  <span>Sign in with Cognito</span>
                }
              </button>

              <!-- Federated providers section -->
              @if (providers().length > 0) {
                <!-- Divider -->
                <div class="relative my-2">
                  <div class="absolute inset-0 flex items-center">
                    <div class="w-full border-t border-gray-200 dark:border-gray-700"></div>
                  </div>
                  <div class="relative flex justify-center text-xs">
                    <span class="bg-white dark:bg-gray-800 px-2 text-gray-500 dark:text-gray-400">or continue with</span>
                  </div>
                </div>

                @for (provider of providers(); track provider.provider_id) {
                  <button
                    type="button"
                    (click)="handleProviderLogin(provider)"
                    [disabled]="isLoading()"
                    class="w-full px-4 py-3 text-white font-medium rounded-lg transition-all duration-200 flex items-center justify-center gap-3 disabled:opacity-60"
                    [style.background-color]="provider.button_color || '#2563eb'"
                    [style.--hover-bg]="provider.button_color ? adjustBrightness(provider.button_color, -15) : '#1d4ed8'"
                  >
                    @if (isLoading() && activeProviderId() === provider.provider_id) {
                      <div class="size-5 border-2 border-white border-t-transparent rounded-full animate-spin"></div>
                      <span>Connecting...</span>
                    } @else {
                      @if (provider.logo_url) {
                        <img [src]="provider.logo_url" [alt]="provider.display_name" class="size-5 object-contain" />
                      } @else {
                        <svg class="size-5" fill="none" viewBox="0 0 24 24" stroke="currentColor" aria-hidden="true">
                          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 15v2m-6 4h12a2 2 0 002-2v-6a2 2 0 00-2-2H6a2 2 0 00-2 2v6a2 2 0 002 2zm10-10V7a4 4 0 00-8 0v4h8z" />
                        </svg>
                      }
                      <span>Sign in with {{ provider.display_name }}</span>
                    }
                  </button>
                }
              }

              <!-- Loading spinner for federated providers -->
              @if (providersLoading()) {
                <div class="flex justify-center py-2">
                  <div class="size-5 border-2 border-gray-300 dark:border-gray-600 border-t-blue-600 dark:border-t-blue-400 rounded-full animate-spin" role="status">
                    <span class="sr-only">Loading federated providers</span>
                  </div>
                </div>
              }
            </div>

            <p class="text-xs text-gray-500 dark:text-gray-400 text-center">
              You will be redirected to complete authentication
            </p>
          </div>
        </div>
      </div>
    </div>
  `
})
export class LoginPage implements OnInit, OnDestroy {
  private authService = inject(AuthService);
  private sidenavService = inject(SidenavService);
  private config = inject(ConfigService);
  private http = inject(HttpClient);
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  private systemService = inject(SystemService);

  isLoading = signal(false);
  errorMessage = signal<string | null>(null);
  providers = signal<AuthProviderPublicInfo[]>([]);
  providersLoading = signal(true);
  activeProviderId = signal<string | null>(null);

  ngOnInit(): void {
    this.sidenavService.hide();
    this.checkFirstBootStatus();
    this.loadProviders();
  }

  ngOnDestroy(): void {
    this.sidenavService.show();
  }

  private async checkFirstBootStatus(): Promise<void> {
    try {
      const completed = await this.systemService.checkStatus();
      if (!completed) {
        this.router.navigate(['/auth/first-boot']);
      }
    } catch {
      // If status check fails, stay on login page
    }
  }

  private async loadProviders(): Promise<void> {
    try {
      const url = `${this.config.appApiUrl()}/auth/providers`;
      const response = await firstValueFrom(
        this.http.get<AuthProviderPublicListResponse>(url)
      );

      this.providers.set(response?.providers ?? []);
    } catch (error) {
      // Federated providers failed to load — Cognito button still works
      this.providers.set([]);
    } finally {
      this.providersLoading.set(false);
    }
  }

  async handleCognitoLogin(): Promise<void> {
    this.isLoading.set(true);
    this.activeProviderId.set(null);
    this.errorMessage.set(null);

    try {
      this.storeReturnUrl();
      await this.authService.login();
    } catch (error) {
      this.isLoading.set(false);
      const errorMsg = error instanceof Error ? error.message : 'An error occurred during login';
      this.errorMessage.set(errorMsg);
    }
  }

  async handleProviderLogin(provider: AuthProviderPublicInfo): Promise<void> {
    this.isLoading.set(true);
    this.activeProviderId.set(provider.provider_id);
    this.errorMessage.set(null);

    try {
      this.storeReturnUrl();
      await this.authService.login(provider.provider_id);
    } catch (error) {
      this.isLoading.set(false);
      this.activeProviderId.set(null);
      const errorMsg = error instanceof Error ? error.message : 'An error occurred during login';
      this.errorMessage.set(errorMsg);
    }
  }

  /**
   * Darken or lighten a hex color for hover states.
   */
  adjustBrightness(hex: string, percent: number): string {
    const num = parseInt(hex.replace('#', ''), 16);
    const r = Math.min(255, Math.max(0, (num >> 16) + percent));
    const g = Math.min(255, Math.max(0, ((num >> 8) & 0x00ff) + percent));
    const b = Math.min(255, Math.max(0, (num & 0x0000ff) + percent));
    return `#${((1 << 24) | (r << 16) | (g << 8) | b).toString(16).slice(1)}`;
  }

  private storeReturnUrl(): void {
    const returnUrl = this.route.snapshot.queryParams['returnUrl'];

    let finalDestination: string | undefined;
    if (returnUrl) {
      finalDestination = returnUrl.startsWith('/') ? returnUrl : `/${returnUrl}`;
    } else {
      const referrer = document.referrer;
      if (referrer) {
        try {
          const referrerUrl = new URL(referrer);
          if (referrerUrl.origin === window.location.origin) {
            const referrerPath = referrerUrl.pathname + referrerUrl.search;
            if (referrerPath !== '/auth/login' && referrerPath !== '/auth/callback') {
              finalDestination = referrerPath;
            }
          }
        } catch (e) {
          // Invalid referrer URL, ignore
        }
      }
    }

    if (finalDestination) {
      sessionStorage.setItem('auth_return_url', finalDestination);
    }
  }
}
