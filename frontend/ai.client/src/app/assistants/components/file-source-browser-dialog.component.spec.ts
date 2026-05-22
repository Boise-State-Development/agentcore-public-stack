import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { FileSourceBrowserDialogComponent } from './file-source-browser-dialog.component';
import { FileSourceService } from '../services/file-source.service';
import { UserConnectorsService } from '../../settings/connectors/services/user-connectors.service';
import { OAuthConsentService } from '../../services/oauth-consent/oauth-consent.service';
import { ToastService } from '../../services/toast/toast.service';
import { FileEntry, FileSourceConnector } from '../models/file-source.model';

const CONNECTED: FileSourceConnector = {
  providerId: 'google',
  displayName: 'Google Drive',
  iconName: 'heroCloud',
  connected: true,
};
const NOT_CONNECTED: FileSourceConnector = { ...CONNECTED, connected: false };

function fileEntry(over: Partial<FileEntry>): FileEntry {
  return { id: 'f1', name: 'a.txt', type: 'file', selectable: true, ...over };
}

function setup(fileSourceOverrides: Partial<Record<string, unknown>> = {}) {
  const fileSourceService = {
    listFileSources: vi.fn().mockResolvedValue([CONNECTED]),
    listRoots: vi.fn().mockResolvedValue([{ id: 'root', name: 'My Drive' }]),
    browse: vi.fn().mockResolvedValue({ entries: [], breadcrumbs: [], nextCursor: null }),
    search: vi.fn().mockResolvedValue({ entries: [], breadcrumbs: [], nextCursor: null }),
    importDocuments: vi.fn().mockResolvedValue({ documents: [{ documentId: 'DOC-1' }] }),
    ...fileSourceOverrides,
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

  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: DIALOG_DATA, useValue: { assistantId: 'AST-1' } },
      { provide: DialogRef, useValue: dialogRef },
      { provide: FileSourceService, useValue: fileSourceService },
      { provide: UserConnectorsService, useValue: connectorsService },
      { provide: OAuthConsentService, useValue: consentService },
      { provide: ToastService, useValue: toast },
    ],
  });

  const fixture = TestBed.createComponent(FileSourceBrowserDialogComponent);
  fixture.detectChanges();
  // Bracket access reaches the component's protected signals/methods in tests.
  const component = fixture.componentInstance as unknown as Record<string, never>;
  return { fixture, component, fileSourceService, dialogRef, connectorsService, consentService, toast };
}

describe('FileSourceBrowserDialogComponent', () => {
  beforeEach(() => TestBed.resetTestingModule());
  afterEach(() => TestBed.resetTestingModule());

  it('loads file sources on open', async () => {
    const { component, fileSourceService } = setup();
    await vi.waitFor(() => {
      expect(fileSourceService.listFileSources).toHaveBeenCalled();
      expect((component['sources'] as () => unknown[])()).toHaveLength(1);
    });
    expect((component['sourcesLoading'] as () => boolean)()).toBe(false);
  });

  it('browsing a connected source enters the browser and loads roots', async () => {
    const { component, fileSourceService } = setup();
    await vi.waitFor(() => expect((component['sources'] as () => unknown[])()).toHaveLength(1));

    (component['useSource'] as (c: FileSourceConnector) => void)(CONNECTED);

    await vi.waitFor(() => {
      expect(fileSourceService.listRoots).toHaveBeenCalledWith('google');
      expect((component['view'] as () => string)()).toBe('browser');
    });
  });

  it('using an unconnected source kicks off the OAuth consent flow', async () => {
    const { component, connectorsService, consentService } = setup({
      listFileSources: vi.fn().mockResolvedValue([NOT_CONNECTED]),
    });
    connectorsService.initiateConsent.mockResolvedValue({
      connected: false,
      authorizationUrl: 'https://auth.example/x',
    });
    await vi.waitFor(() => expect((component['sources'] as () => unknown[])()).toHaveLength(1));

    (component['useSource'] as (c: FileSourceConnector) => void)(NOT_CONNECTED);

    await vi.waitFor(() => {
      expect(connectorsService.initiateConsent).toHaveBeenCalledWith('google');
      expect(consentService.openConsentPopup).toHaveBeenCalledWith('google');
    });
  });

  it('toggleSelect tracks selectable files and ignores non-selectable ones', async () => {
    const { component } = setup();
    await vi.waitFor(() => expect((component['sources'] as () => unknown[])()).toHaveLength(1));

    const count = () => (component['selectedCount'] as () => number)();
    // Called inline so the method keeps its `this` binding to the component.
    (component['toggleSelect'] as (e: FileEntry) => void)(fileEntry({ id: 'f1', selectable: true }));
    expect(count()).toBe(1);
    (component['toggleSelect'] as (e: FileEntry) => void)(fileEntry({ id: 'f1', selectable: true }));
    expect(count()).toBe(0);
    (component['toggleSelect'] as (e: FileEntry) => void)(fileEntry({ id: 'f2', selectable: false }));
    expect(count()).toBe(0);
  });

  it('importSelected posts the selection and closes with the created documents', async () => {
    const { component, fileSourceService, dialogRef } = setup();
    await vi.waitFor(() => expect((component['sources'] as () => unknown[])()).toHaveLength(1));

    (component['useSource'] as (c: FileSourceConnector) => void)(CONNECTED);
    await vi.waitFor(() => expect((component['view'] as () => string)()).toBe('browser'));

    (component['toggleSelect'] as (e: FileEntry) => void)(fileEntry({ id: 'f1' }));
    await (component['importSelected'] as () => Promise<void>)();

    expect(fileSourceService.importDocuments).toHaveBeenCalledWith('AST-1', 'google', [
      { fileId: 'f1', name: 'a.txt' },
    ]);
    expect(dialogRef.close).toHaveBeenCalledWith([{ documentId: 'DOC-1' }]);
  });
});
