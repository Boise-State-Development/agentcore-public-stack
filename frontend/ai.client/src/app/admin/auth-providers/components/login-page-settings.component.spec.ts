import { describe, it, expect, beforeEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { signal, WritableSignal } from '@angular/core';
import { LoginPageSettingsComponent } from './login-page-settings.component';
import { AuthProvidersService } from '../services/auth-providers.service';
import { LoginPageSettings } from '../models/auth-provider.model';

function settings(overrides: Partial<LoginPageSettings> = {}): LoginPageSettings {
  return {
    hide_cognito_login: false,
    cognito_login_visible: true,
    enabled_provider_count: 1,
    updated_at: null,
    updated_by: null,
    ...overrides,
  };
}

describe('LoginPageSettingsComponent', () => {
  let fixture: ComponentFixture<LoginPageSettingsComponent>;
  let value: WritableSignal<LoginPageSettings | undefined>;
  let updateLoginPageSettings: ReturnType<typeof vi.fn>;

  function render(initial: LoginPageSettings | undefined): void {
    value = signal(initial);
    updateLoginPageSettings = vi.fn(async (body: { hide_cognito_login: boolean }) => {
      const next = settings({ ...value(), hide_cognito_login: body.hide_cognito_login });
      value.set(next);
      return next;
    });

    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [LoginPageSettingsComponent],
      providers: [
        {
          provide: AuthProvidersService,
          useValue: {
            loginPageSettingsResource: { value, hasValue: () => value() !== undefined },
            updateLoginPageSettings,
          },
        },
      ],
    });
    fixture = TestBed.createComponent(LoginPageSettingsComponent);
    fixture.detectChanges();
  }

  function toggle(): HTMLButtonElement {
    return fixture.nativeElement.querySelector('button[role="switch"]');
  }

  function text(): string {
    return fixture.nativeElement.textContent as string;
  }

  beforeEach(() => render(settings()));

  it('renders an accessible switch reflecting the saved setting', () => {
    expect(toggle().getAttribute('aria-checked')).toBe('false');
    expect(toggle().getAttribute('aria-labelledby')).toBe('hide-cognito-label');
    expect(text()).toContain('/auth/login?local=1');
  });

  it('saves the flipped value', async () => {
    toggle().click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(updateLoginPageSettings).toHaveBeenCalledWith({ hide_cognito_login: true });
    expect(toggle().getAttribute('aria-checked')).toBe('true');
  });

  it('cannot hide while no provider is enabled', () => {
    render(settings({ enabled_provider_count: 0 }));
    expect(toggle().disabled).toBe(true);
    expect(text()).toContain('Enable a provider before hiding Cognito sign-in.');
  });

  it('can always show again, and says why it is still shown', () => {
    render(settings({ hide_cognito_login: true, enabled_provider_count: 0 }));
    expect(toggle().disabled).toBe(false);
    expect(text()).toContain('still shown because no provider is enabled');
  });

  it('is disabled until the settings load', () => {
    render(undefined);
    expect(toggle().disabled).toBe(true);
  });

  it('shows the server error when saving fails', async () => {
    updateLoginPageSettings.mockRejectedValueOnce(
      new HttpErrorResponse({ status: 422, error: { detail: 'Enable at least one provider' } }),
    );
    toggle().click();
    await fixture.whenStable();
    fixture.detectChanges();

    const alert: HTMLElement = fixture.nativeElement.querySelector('[role="alert"]');
    expect(alert.textContent).toContain('Enable at least one provider');
    expect(toggle().disabled).toBe(false);
  });
});
