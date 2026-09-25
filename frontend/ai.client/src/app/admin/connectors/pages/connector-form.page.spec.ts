import { describe, it, expect, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap, provideRouter } from '@angular/router';
import { ConnectorFormPage } from './connector-form.page';
import { ConnectorsService } from '../services/connectors.service';
import { AppRolesService } from '../../roles/services/app-roles.service';
import { Connector } from '../models/connector.model';

const CONNECTOR = {
  providerId: 'example-drive',
  displayName: 'Example Drive',
  providerType: 'google',
  scopes: ['openid'],
  allowedRoles: [],
  enabled: true,
  iconName: 'heroLink',
  callbackUrl: 'https://example.com/oauth/callback',
  createdAt: '2026-01-01T00:00:00Z',
  updatedAt: '2026-01-01T00:00:00Z',
} as Connector;

/** Accessible names of every control whose only content is an icon. */
function iconOnlyControlNames(el: HTMLElement): (string | null)[] {
  return Array.from(el.querySelectorAll('button, a'))
    .filter(c => c.querySelector('ng-icon') && !c.textContent?.trim())
    .map(c => c.getAttribute('aria-label'));
}

function setup(providerId: string | null) {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    imports: [ConnectorFormPage],
    providers: [
      provideRouter([]),
      {
        provide: ActivatedRoute,
        useValue: { snapshot: { paramMap: convertToParamMap(providerId ? { providerId } : {}) } },
      },
      {
        provide: ConnectorsService,
        useValue: {
          fetchConnector: vi.fn().mockResolvedValue(CONNECTOR),
          getFileSourceAdapters: () => [],
          getExportTargetAdapters: () => [],
        },
      },
      {
        provide: AppRolesService,
        useValue: {
          rolesResource: { isLoading: () => false, value: () => [] },
          getEnabledRoles: () => [],
        },
      },
    ],
  });
  return TestBed.createComponent(ConnectorFormPage);
}

// Icon-only controls: the tooltip only describes them (aria-describedby), so without
// a label a screen reader announced Copy and Show/Hide secret as "button".
describe('ConnectorFormPage icon-only controls', () => {
  it('names the secret toggle after its current action', () => {
    const fixture = setup(null);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;

    expect(iconOnlyControlNames(el)).toEqual(['Show client secret']);

    fixture.componentInstance.showClientSecret.set(true);
    fixture.detectChanges();
    expect(iconOnlyControlNames(el)).toEqual(['Hide client secret']);
  });

  it('names the callback-URL copy button when editing', async () => {
    const fixture = setup('example-drive');
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(iconOnlyControlNames(fixture.nativeElement)).toEqual([
      'Copy callback URL',
      'Show client secret',
    ]);
  });

  it('names the callback-URL copy button after creating', () => {
    const fixture = setup(null);
    fixture.componentInstance.createdConnector.set(CONNECTOR);
    fixture.detectChanges();

    expect(iconOnlyControlNames(fixture.nativeElement)).toEqual(['Copy callback URL']);
  });
});
