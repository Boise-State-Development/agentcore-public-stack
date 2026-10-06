import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { provideRouter } from '@angular/router';
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

  beforeEach(() => {
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
      ],
    });
    fixture = TestBed.createComponent(LoginPage);
    fixture.detectChanges();
  });

  describe('back/forward cache restore', () => {
    it('clears the loading state left by a sign-in redirect', () => {
      const page = fixture.componentInstance;
      page.handleProviderLogin({ provider_id: 'entra', display_name: 'Entra' });
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
});
