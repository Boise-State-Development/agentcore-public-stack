import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { signal } from '@angular/core';
import { LoginPage } from './login.page';
import { SessionService } from '../session.service';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { ConfigService } from '../../services/config.service';
import { SystemService } from '../../services/system.service';
import { BrandingService } from '../../../branding/branding.service';

function pageshow(persisted: boolean): PageTransitionEvent {
  return new PageTransitionEvent('pageshow', { persisted });
}

describe('LoginPage', () => {
  let fixture: ComponentFixture<LoginPage>;
  let redirectToLogin: ReturnType<typeof vi.fn>;
  let http: HttpTestingController;

  function configure(queryParams: Record<string, string> = {}): void {
    redirectToLogin = vi.fn();
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [LoginPage],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        {
          provide: SessionService,
          useValue: { isAuthenticated: signal(false), redirectToLogin },
        },
        { provide: SidenavService, useValue: { hide: vi.fn(), show: vi.fn() } },
        { provide: ConfigService, useValue: { appApiUrl: signal('/api') } },
        { provide: SystemService, useValue: { checkStatus: vi.fn().mockResolvedValue(true) } },
        {
          provide: BrandingService,
          useValue: { logo: { light: '/l.svg', dark: '/d.svg' }, appName: 'App' },
        },
        {
          provide: ActivatedRoute,
          useValue: { snapshot: { queryParams, queryParamMap: convertToParamMap(queryParams) } },
        },
      ],
    });
    fixture = TestBed.createComponent(LoginPage);
    http = TestBed.inject(HttpTestingController);
    fixture.detectChanges();
  }

  async function respond(body: object): Promise<void> {
    http.expectOne('/api/auth/providers').flush(body);
    await fixture.whenStable();
    fixture.detectChanges();
  }

  function buttonLabels(): string[] {
    const buttons: NodeListOf<HTMLButtonElement> = fixture.nativeElement.querySelectorAll('button');
    return Array.from(buttons).map((b) => b.textContent?.trim() ?? '');
  }

  function hasDivider(): boolean {
    return (fixture.nativeElement.textContent as string).includes('or continue with');
  }

  const entra = { provider_id: 'entra', display_name: 'Entra' };

  beforeEach(() => configure());

  describe('back/forward cache restore', () => {
    it('clears the loading state left by a sign-in redirect', () => {
      const page = fixture.componentInstance;
      page.handleProviderLogin(entra);
      expect(redirectToLogin).toHaveBeenCalled();
      expect(page.isLoading()).toBe(true);

      window.dispatchEvent(pageshow(true));

      expect(page.isLoading()).toBe(false);
      expect(page.activeProviderId()).toBeNull();
    });

    it('leaves a fresh load alone', () => {
      const page = fixture.componentInstance;
      page.handleCognitoLogin();

      window.dispatchEvent(pageshow(false));

      expect(page.isLoading()).toBe(true);
    });
  });

  describe('Cognito sign-in button', () => {
    it('waits for the provider list before showing', () => {
      expect(buttonLabels()).toEqual([]);
    });

    it('shows with a divider above the providers by default', async () => {
      await respond({ providers: [entra], cognito_login_visible: true });
      expect(buttonLabels()).toEqual(['Sign in with Cognito', 'Sign in with Entra']);
      expect(hasDivider()).toBe(true);
    });

    it('shows when the backend predates the setting', async () => {
      await respond({ providers: [entra] });
      expect(buttonLabels()).toContain('Sign in with Cognito');
    });

    it('hides, with no divider, when a system admin hid it', async () => {
      await respond({ providers: [entra], cognito_login_visible: false });
      expect(buttonLabels()).toEqual(['Sign in with Entra']);
      expect(hasDivider()).toBe(false);
    });

    it('shows when the provider list fails to load', async () => {
      http
        .expectOne('/api/auth/providers')
        .flush('boom', { status: 500, statusText: 'Server Error' });
      await fixture.whenStable();
      fixture.detectChanges();
      expect(buttonLabels()).toEqual(['Sign in with Cognito']);
    });

    it('shows on the ?local=1 break-glass URL even when hidden', async () => {
      configure({ local: '1' });
      expect(buttonLabels()).toEqual(['Sign in with Cognito']);

      await respond({ providers: [entra], cognito_login_visible: false });

      expect(buttonLabels()).toEqual(['Sign in with Cognito', 'Sign in with Entra']);
      expect(hasDivider()).toBe(true);
    });
  });
});
