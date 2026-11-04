import { describe, it, expect, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { ActivatedRoute, Router, convertToParamMap, provideRouter } from '@angular/router';
import { ConnectorFormPage } from './connector-form.page';
import { ConnectorsService } from '../services/connectors.service';
import { RoleOptionsService } from '../../roles/services/role-options.service';
import { Connector, CONNECTOR_PRESETS } from '../models/connector.model';
import { getBrandIconDataUrl } from '../models/connector-brand-icons';

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

function setup(providerId: string | null, connector: Connector = CONNECTOR) {
  const service = {
    fetchConnector: vi.fn().mockResolvedValue(connector),
    createConnector: vi.fn().mockResolvedValue(connector),
    updateConnector: vi.fn().mockResolvedValue(connector),
    getFileSourceAdapters: () => [],
    getExportTargetAdapters: () => [],
  };
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
        useValue: service,
      },
      {
        provide: RoleOptionsService,
        useValue: {
          rolesResource: { isLoading: () => false, value: () => [] },
          getEnabledRoles: () => [],
        },
      },
    ],
  });
  return Object.assign(TestBed.createComponent(ConnectorFormPage), { service });
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

const TENANT = '11111111-2222-3333-4444-555555555555';

const MICROSOFT_CONNECTOR = {
  ...CONNECTOR,
  providerId: 'azure-devops',
  displayName: 'Azure DevOps',
  providerType: 'microsoft',
  tenantId: TENANT,
} as Connector;

async function loadEdit(connector: Connector) {
  const fixture = setup(connector.providerId, connector);
  // A saved edit navigates back to the list, which this module has no route for.
  vi.spyOn(TestBed.inject(Router), 'navigate').mockResolvedValue(true);
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  return fixture;
}

// A single-tenant Entra app registration only signs in through its own
// tenant, so a Microsoft connector carries one; no other vendor does.
describe('ConnectorFormPage Entra tenant', () => {
  it('offers the tenant field only for Microsoft', () => {
    const fixture = setup(null);
    fixture.componentInstance.selectConnectorType('google');
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.querySelector('#tenantId')).toBeNull();

    fixture.componentInstance.selectConnectorType('microsoft');
    fixture.detectChanges();
    expect(el.querySelector('#tenantId')).not.toBeNull();
  });

  it('sends the trimmed tenant on create', async () => {
    const fixture = setup(null);
    fixture.detectChanges();
    const page = fixture.componentInstance;
    page.selectConnectorType('microsoft');
    page.connectorForm.patchValue({
      providerId: 'azure-devops',
      clientId: 'cid',
      clientSecret: 'sec',
      tenantId: `  ${TENANT} `,
    });

    await page.onSubmit();

    expect(fixture.service.createConnector).toHaveBeenCalledWith(
      expect.objectContaining({ providerType: 'microsoft', tenantId: TENANT }),
    );
  });

  it('drops the tenant when the type changes away from Microsoft', () => {
    const fixture = setup(null);
    fixture.detectChanges();
    const page = fixture.componentInstance;
    page.selectConnectorType('microsoft');
    page.connectorForm.controls.tenantId.setValue(TENANT);

    page.selectConnectorType('google');

    expect(page.connectorForm.controls.tenantId.value).toBe('');
  });

  it('rejects a value that is not a tenant ID or domain', () => {
    const fixture = setup(null);
    fixture.detectChanges();
    const page = fixture.componentInstance;
    page.selectConnectorType('microsoft');
    const ctrl = page.connectorForm.controls.tenantId;

    ctrl.setValue('https://login.microsoftonline.com/contoso');
    ctrl.markAsTouched();
    fixture.detectChanges();
    expect(ctrl.invalid).toBe(true);
    // The error is announced with the field, not just painted under it.
    const input = (fixture.nativeElement as HTMLElement).querySelector('#tenantId')!;
    expect(input.getAttribute('aria-invalid')).toBe('true');
    expect(input.getAttribute('aria-describedby')).toBe('tenantId-error tenantId-help');

    ctrl.setValue('contoso.onmicrosoft.com');
    expect(ctrl.valid).toBe(true);
  });

  it('omits an unchanged tenant on a metadata-only edit', async () => {
    // The backend treats a tenant change as a credential rotation, so
    // resending the loaded value would reject every edit without the secret.
    const fixture = await loadEdit(MICROSOFT_CONNECTOR);
    const page = fixture.componentInstance;
    page.connectorForm.controls.displayName.setValue('Azure DevOps (Boise State)');

    await page.onSubmit();

    const updates = fixture.service.updateConnector.mock.calls[0][1];
    expect(updates.displayName).toBe('Azure DevOps (Boise State)');
    expect('tenantId' in updates).toBe(false);
  });

  it('sends an empty string to clear a stored tenant', async () => {
    const fixture = await loadEdit(MICROSOFT_CONNECTOR);
    const page = fixture.componentInstance;
    page.connectorForm.patchValue({ tenantId: '', clientId: 'cid', clientSecret: 'sec' });

    await page.onSubmit();

    expect(fixture.service.updateConnector.mock.calls[0][1]).toMatchObject({
      tenantId: '',
      clientId: 'cid',
    });
  });
});

describe('ConnectorFormPage provider picker', () => {
  const radios = (el: HTMLElement) =>
    Array.from(el.querySelectorAll<HTMLInputElement>('input[name="providerTypePreset"]'));

  it('offers every preset once, as radio inputs', () => {
    const fixture = setup(null);
    fixture.detectChanges();

    expect(radios(fixture.nativeElement)).toHaveLength(CONNECTOR_PRESETS.length);
  });

  it('narrows the presets by search', () => {
    const fixture = setup(null);
    fixture.componentInstance.presetQuery.set('git');
    fixture.detectChanges();

    expect(radios(fixture.nativeElement).map(r => r.value)).toEqual(['github']);
  });

  it('says so when nothing matches the search', () => {
    const fixture = setup(null);
    fixture.componentInstance.presetQuery.set('zzzz');
    fixture.detectChanges();

    expect(radios(fixture.nativeElement)).toHaveLength(0);
    expect(fixture.nativeElement.textContent).toContain('No providers match');
  });

  it('fills the icon with the vendor logo when a preset is chosen', () => {
    const fixture = setup(null);
    fixture.detectChanges();
    fixture.componentInstance.selectConnectorType('github');

    expect(fixture.componentInstance.connectorForm.controls.iconData.value).toBe(
      getBrandIconDataUrl('github'),
    );
  });

  it('swaps the logo when the preset changes, and clears it when the new one has none', () => {
    const fixture = setup(null);
    fixture.detectChanges();
    const icon = fixture.componentInstance.connectorForm.controls.iconData;

    fixture.componentInstance.selectConnectorType('github');
    fixture.componentInstance.selectConnectorType('zoom');
    expect(icon.value).toBe(getBrandIconDataUrl('zoom'));

    fixture.componentInstance.selectConnectorType('onelogin');
    expect(icon.value).toBe('');
  });

  it("never replaces an admin's own upload", () => {
    const fixture = setup(null);
    fixture.detectChanges();
    const icon = fixture.componentInstance.connectorForm.controls.iconData;
    const upload = 'data:image/png;base64,AAAA';

    icon.setValue(upload);
    fixture.componentInstance.selectConnectorType('github');
    expect(icon.value).toBe(upload);
  });

  it('shows the tenant endpoints only for tenant-hosted providers', () => {
    const fixture = setup(null);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;

    fixture.componentInstance.selectConnectorType('okta');
    fixture.detectChanges();
    expect(el.querySelector('#tenantIssuer')).not.toBeNull();

    fixture.componentInstance.selectConnectorType('atlassian');
    fixture.detectChanges();
    expect(el.querySelector('#tenantIssuer')).toBeNull();
  });

  it('marks a touched invalid field aria-invalid, not by a competing border class', () => {
    const fixture = setup(null);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    const id = el.querySelector<HTMLInputElement>('#providerId')!;
    expect(id.getAttribute('aria-invalid')).toBeNull();

    fixture.componentInstance.connectorForm.controls.providerId.markAsTouched();
    fixture.detectChanges();
    expect(id.getAttribute('aria-invalid')).toBe('true');
  });
});
