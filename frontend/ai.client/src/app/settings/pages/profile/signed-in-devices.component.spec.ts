import { Dialog } from '@angular/cdk/dialog';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of } from 'rxjs';
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { ConfigService } from '../../../services/config.service';
import { SignedInDevicesComponent } from './signed-in-devices.component';
import { SignedInDevice } from './signed-in-devices.service';

const API = 'https://ai.example.edu/api';

const phone: SignedInDevice = {
  deviceId: 'a'.repeat(32),
  kind: 'native',
  deviceModel: 'iPhone',
  appVersion: '1.41.0',
  signedInAt: '2026-11-20T15:00:00Z',
  lastUsedAt: '2026-11-27T15:00:00Z',
  expiresAt: '2026-12-20T15:00:00Z',
  current: false,
};

const terminal: SignedInDevice = {
  deviceId: 'b'.repeat(32),
  kind: 'terminal',
  signedInAt: '2026-11-25T15:00:00Z',
  lastUsedAt: '2026-11-26T15:00:00Z',
  current: false,
};

describe('SignedInDevicesComponent', () => {
  let fixture: ComponentFixture<SignedInDevicesComponent>;
  let http: HttpTestingController;
  let confirm: boolean;
  let dialogOpen: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    confirm = true;
    dialogOpen = vi.fn(() => ({ closed: of(confirm) }));
    TestBed.configureTestingModule({
      imports: [SignedInDevicesComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
        { provide: ConfigService, useValue: { appApiUrl: () => API } },
        { provide: Dialog, useValue: { open: dialogOpen } },
      ],
    });
    fixture = TestBed.createComponent(SignedInDevicesComponent);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  /** Let pending promises, the resource and change detection catch up, without waiting on open requests. */
  async function settle(): Promise<void> {
    TestBed.tick();
    await new Promise((resolve) => setTimeout(resolve, 0));
    TestBed.tick();
    fixture.detectChanges();
  }

  async function load(devices: SignedInDevice[]): Promise<HTMLElement> {
    fixture.detectChanges();
    await settle();
    http.expectOne(`${API}/auth/devices`).flush({ devices });
    await settle();
    return fixture.nativeElement as HTMLElement;
  }

  it('lists the phone app and the command-line client', async () => {
    const el = await load([phone, terminal]);
    const rows = el.querySelectorAll('li');
    expect(rows.length).toBe(2);
    expect(rows[0].textContent).toContain('iPhone app');
    expect(rows[0].textContent).toContain('Version 1.41.0');
    expect(rows[1].textContent).toContain('Command-line client');
    expect(el.querySelector('button')?.getAttribute('aria-label')).toBe('Sign out iPhone app');
  });

  it('says so when nothing is signed in', async () => {
    const el = await load([]);
    expect(el.textContent).toContain('No apps or terminals are signed in.');
  });

  it('signs a device out after the confirmation and drops its row', async () => {
    const el = await load([phone, terminal]);
    (el.querySelector('button') as HTMLButtonElement).click();
    await settle();

    const del = http.expectOne(`${API}/auth/devices/${phone.deviceId}`);
    expect(del.request.method).toBe('DELETE');
    del.flush({ revoked: 1, scheduledRunsContinue: false });
    await settle();
    http.expectOne(`${API}/auth/devices`).flush({ devices: [terminal] });
    await settle();

    expect(dialogOpen).toHaveBeenCalledOnce();
    expect(el.querySelectorAll('li').length).toBe(1);
    expect(el.querySelector('[role="status"]')?.textContent).toContain('iPhone app is signed out.');
  });

  it('points to Schedules when the headless grant kept the token', async () => {
    const el = await load([phone]);
    (el.querySelector('button') as HTMLButtonElement).click();
    await settle();
    http.expectOne(`${API}/auth/devices/${phone.deviceId}`).flush({ revoked: 1, scheduledRunsContinue: true });
    await settle();
    http.expectOne(`${API}/auth/devices`).flush({ devices: [] });
    await settle();

    const status = el.querySelector('[role="status"]');
    expect(status?.textContent).toContain('Your scheduled runs keep running');
    expect(status?.querySelector('a')?.getAttribute('href')).toBe('/schedules');
  });

  it('does nothing when the confirmation is cancelled', async () => {
    confirm = false;
    const el = await load([phone]);
    (el.querySelector('button') as HTMLButtonElement).click();
    await settle();
    http.expectNone(`${API}/auth/devices/${phone.deviceId}`);
    expect(el.querySelectorAll('li').length).toBe(1);
  });
});
