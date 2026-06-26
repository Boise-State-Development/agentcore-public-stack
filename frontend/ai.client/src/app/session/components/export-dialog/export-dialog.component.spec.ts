import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { of } from 'rxjs';
import { Dialog, DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { ExportDialogComponent } from './export-dialog.component';
import { ExportService } from '../../services/export/export.service';
import { ExportTargetConnector } from '../../services/export/export.model';
import { UserConnectorsService } from '../../../settings/connectors/services/user-connectors.service';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { ToastService } from '../../../services/toast/toast.service';

const CONNECTED: ExportTargetConnector = {
  providerId: 'gdrive',
  displayName: 'Google Drive',
  iconName: 'heroCloud',
  connected: true,
  supportedFormats: ['google_doc', 'markdown'],
  browsable: false,
};
const NOT_CONNECTED: ExportTargetConnector = { ...CONNECTED, connected: false };
const BROWSABLE: ExportTargetConnector = { ...CONNECTED, browsable: true };

function setup(
  exportOverrides: Partial<Record<string, unknown>> = {},
  dialogOverride?: Partial<Record<string, unknown>>,
) {
  const exportService = {
    listExportTargets: vi.fn().mockResolvedValue([CONNECTED]),
    exportSession: vi.fn().mockResolvedValue({
      fileId: 'file-123',
      name: 'My Chat',
      webViewLink: 'https://drive.example/file-123',
      receipt: {
        connectorId: 'gdrive',
        adapterKey: 'google-drive',
        format: 'google_doc',
        fileId: 'file-123',
        fileName: 'My Chat',
        webViewLink: 'https://drive.example/file-123',
        exportedAt: '2026-06-26T00:00:00Z',
      },
    }),
    ...exportOverrides,
  };
  const dialogRef = { close: vi.fn() };
  const connectorsService = { initiateConsent: vi.fn() };
  const consentService = {
    completion: signal<unknown>(null),
    inFlightProviders: signal(new Set<string>()),
    requestConsent: vi.fn(),
    openConsentPopup: vi.fn().mockResolvedValue(true),
    acknowledgeCompletion: vi.fn(),
  };
  const toast = { error: vi.fn(), success: vi.fn() };
  // CDK Dialog used to open the folder picker; closes with a FolderSelection.
  const dialog = {
    open: vi.fn().mockReturnValue({
      closed: of({ folderId: 'folder-9', folderName: 'Reports' }),
    }),
    ...dialogOverride,
  };

  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: DIALOG_DATA, useValue: { sessionId: 'sess-1', title: 'My Chat' } },
      { provide: DialogRef, useValue: dialogRef },
      { provide: Dialog, useValue: dialog },
      { provide: ExportService, useValue: exportService },
      { provide: UserConnectorsService, useValue: connectorsService },
      { provide: OAuthConsentService, useValue: consentService },
      { provide: ToastService, useValue: toast },
    ],
  });

  const fixture = TestBed.createComponent(ExportDialogComponent);
  fixture.detectChanges();
  const component = fixture.componentInstance as unknown as Record<string, never>;
  return { fixture, component, exportService, dialogRef, dialog, connectorsService, consentService, toast };
}

describe('ExportDialogComponent', () => {
  beforeEach(() => TestBed.resetTestingModule());
  afterEach(() => TestBed.resetTestingModule());

  it('loads export targets on open and auto-selects a sole destination', async () => {
    const { component, exportService } = setup();
    await vi.waitFor(() => {
      expect(exportService.listExportTargets).toHaveBeenCalled();
      expect((component['targets'] as () => unknown[])()).toHaveLength(1);
    });
    // Single destination is auto-selected with its preferred format.
    expect((component['selectedConnectorId'] as () => string | null)()).toBe('gdrive');
    expect((component['selectedFormat'] as () => string | null)()).toBe('google_doc');
  });

  it('saves a connected destination and surfaces the result', async () => {
    const { component, exportService } = setup();
    await vi.waitFor(() => expect((component['targets'] as () => unknown[])()).toHaveLength(1));

    await (component['save'] as () => Promise<void>)();

    expect(exportService.exportSession).toHaveBeenCalledWith(
      'sess-1',
      expect.objectContaining({ connectorId: 'gdrive', format: 'google_doc' }),
    );
    expect((component['result'] as () => { fileId: string } | null)()?.fileId).toBe('file-123');
  });

  it('toggles include flags off and back on', async () => {
    const { component } = setup();
    await vi.waitFor(() => expect((component['targets'] as () => unknown[])()).toHaveLength(1));

    const include = () => (component['include'] as () => Record<string, boolean>)();
    expect(include()['citations']).toBe(true);
    (component['toggleInclude'] as (k: string) => void)('citations');
    expect(include()['citations']).toBe(false);
  });

  it('runs the consent flow when saving to an unconnected destination', async () => {
    const { component, connectorsService, consentService, exportService } = setup({
      listExportTargets: vi.fn().mockResolvedValue([NOT_CONNECTED]),
    });
    connectorsService.initiateConsent.mockResolvedValue({
      connected: false,
      authorizationUrl: 'https://auth.example/x',
    });
    await vi.waitFor(() => expect((component['targets'] as () => unknown[])()).toHaveLength(1));

    await (component['save'] as () => Promise<void>)();

    expect(connectorsService.initiateConsent).toHaveBeenCalledWith('gdrive');
    expect(consentService.openConsentPopup).toHaveBeenCalledWith('gdrive');
    // The save itself is deferred until consent completes.
    expect(exportService.exportSession).not.toHaveBeenCalled();
  });

  it('retries the export after consent completes successfully', async () => {
    const { component, connectorsService, consentService, exportService } = setup({
      listExportTargets: vi.fn().mockResolvedValue([NOT_CONNECTED]),
    });
    connectorsService.initiateConsent.mockResolvedValue({
      connected: false,
      authorizationUrl: 'https://auth.example/x',
    });
    await vi.waitFor(() => expect((component['targets'] as () => unknown[])()).toHaveLength(1));

    await (component['save'] as () => Promise<void>)();
    // Simulate the /oauth-complete popup broadcasting success.
    (consentService.completion as ReturnType<typeof signal>).set({
      type: 'agentcore-oauth-complete',
      status: 'success',
      providerId: 'gdrive',
      error: null,
    });

    await vi.waitFor(() => {
      expect(exportService.exportSession).toHaveBeenCalledWith(
        'sess-1',
        expect.objectContaining({ connectorId: 'gdrive' }),
      );
    });
    expect(consentService.acknowledgeCompletion).toHaveBeenCalled();
  });

  it('offers the folder picker only for a browsable destination', async () => {
    const { component } = setup({
      listExportTargets: vi.fn().mockResolvedValue([BROWSABLE]),
    });
    await vi.waitFor(() => expect((component['targets'] as () => unknown[])()).toHaveLength(1));
    expect((component['canChooseFolder'] as () => boolean)()).toBe(true);
    // Default destination is the app folder until a folder is chosen.
    expect((component['destinationLabel'] as () => string)()).toBe('App folder');
  });

  it('hides the folder picker for a non-browsable destination', async () => {
    const { component } = setup();
    await vi.waitFor(() => expect((component['targets'] as () => unknown[])()).toHaveLength(1));
    expect((component['canChooseFolder'] as () => boolean)()).toBe(false);
  });

  it('passes the chosen folder as parentId on save', async () => {
    const { component, dialog, exportService } = setup({
      listExportTargets: vi.fn().mockResolvedValue([BROWSABLE]),
    });
    await vi.waitFor(() => expect((component['targets'] as () => unknown[])()).toHaveLength(1));

    await (component['chooseFolder'] as () => Promise<void>)();
    expect(dialog.open).toHaveBeenCalled();
    expect((component['destinationLabel'] as () => string)()).toBe('Reports');

    await (component['save'] as () => Promise<void>)();
    expect(exportService.exportSession).toHaveBeenCalledWith(
      'sess-1',
      expect.objectContaining({ connectorId: 'gdrive', parentId: 'folder-9' }),
    );
  });

  it('saves to the app folder (no parentId) when no folder is chosen', async () => {
    const { component, exportService } = setup({
      listExportTargets: vi.fn().mockResolvedValue([BROWSABLE]),
    });
    await vi.waitFor(() => expect((component['targets'] as () => unknown[])()).toHaveLength(1));

    await (component['save'] as () => Promise<void>)();
    expect(exportService.exportSession).toHaveBeenCalledWith(
      'sess-1',
      expect.objectContaining({ connectorId: 'gdrive', parentId: undefined }),
    );
  });

  it('resets the chosen folder when the destination changes', async () => {
    const { component } = setup({
      listExportTargets: vi.fn().mockResolvedValue([BROWSABLE]),
    });
    await vi.waitFor(() => expect((component['targets'] as () => unknown[])()).toHaveLength(1));

    await (component['chooseFolder'] as () => Promise<void>)();
    expect((component['destinationLabel'] as () => string)()).toBe('Reports');

    (component['selectConnector'] as (t: ExportTargetConnector) => void)(BROWSABLE);
    expect((component['destinationLabel'] as () => string)()).toBe('App folder');
    expect((component['parentId'] as () => string | null)()).toBeNull();
  });

  it('cancel closes with the result (undefined before any save)', async () => {
    const { component, dialogRef } = setup();
    await vi.waitFor(() => expect((component['targets'] as () => unknown[])()).toHaveLength(1));

    (component['cancel'] as () => void)();
    expect(dialogRef.close).toHaveBeenCalledWith(undefined);
  });
});
