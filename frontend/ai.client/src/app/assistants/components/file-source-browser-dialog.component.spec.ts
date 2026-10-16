import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { DIALOG_DATA, Dialog, DialogRef } from '@angular/cdk/dialog';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import {
  FileSourceBrowserDialogComponent,
  FileSourceBrowserDialogData,
} from './file-source-browser-dialog.component';
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

function setup(
  fileSourceOverrides: Partial<Record<string, unknown>> = {},
  dialogData: Record<string, unknown> = { target: 'AST-1' },
) {
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
    startUserConsent: vi.fn().mockResolvedValue(true),
    acknowledgeCompletion: vi.fn(),
  };
  const toast = { error: vi.fn(), success: vi.fn(), info: vi.fn() };

  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: DIALOG_DATA, useValue: dialogData },
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
      expect(consentService.startUserConsent).toHaveBeenCalledWith('google', 'https://auth.example/x');
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

  describe('pick-folder mode', () => {
    it('opens straight into the targeted connector and loads roots', async () => {
      const { component, fileSourceService } = setup(
        // Two roots so it stays on the roots list rather than auto-drilling in.
        {
          listRoots: vi.fn().mockResolvedValue([
            { id: 'root', name: 'My Drive' },
            { id: 'shared', name: 'Shared with me' },
          ]),
        },
        { connector: CONNECTED, mode: 'pick-folder' },
      );

      await vi.waitFor(() => {
        expect(fileSourceService.listRoots).toHaveBeenCalledWith('google');
        expect((component['view'] as () => string)()).toBe('browser');
      });
      expect((component['isPicker'] as boolean)).toBe(true);
      // Nothing selectable until the user drills into a folder.
      expect((component['canPickCurrentFolder'] as () => boolean)()).toBe(false);
    });

    it('shows only folders, not files, while browsing', async () => {
      const { component } = setup(
        {
          browse: vi.fn().mockResolvedValue({
            entries: [
              fileEntry({ id: 'sub', name: 'Subfolder', type: 'folder', selectable: false }),
              fileEntry({ id: 'doc', name: 'notes.txt', type: 'file', selectable: true }),
            ],
            breadcrumbs: [],
            nextCursor: null,
          }),
        },
        { connector: CONNECTED, mode: 'pick-folder' },
      );
      // Single root auto-drills into the folder, populating entries.
      await vi.waitFor(() =>
        expect((component['currentFolderId'] as () => string | null)()).toBe('root'),
      );

      const shown = (component['displayedEntries'] as () => FileEntry[])();
      expect(shown.map((e) => e.id)).toEqual(['sub']);
    });

    it('closes with the current folder as the selection', async () => {
      const { component, dialogRef } = setup(
        { browse: vi.fn().mockResolvedValue({ entries: [], breadcrumbs: [], nextCursor: null }) },
        { connector: CONNECTED, mode: 'pick-folder' },
      );
      await vi.waitFor(() =>
        expect((component['currentFolderId'] as () => string | null)()).toBe('root'),
      );

      expect((component['canPickCurrentFolder'] as () => boolean)()).toBe(true);
      (component['pickCurrentFolder'] as () => void)();
      expect(dialogRef.close).toHaveBeenCalledWith({ folderId: 'root', folderName: 'My Drive' });
    });
  });

  describe('attach mode (the composer\'s "Add from …")', () => {
    const LISTING = {
      entries: [
        fileEntry({ id: 'pdf', name: 'report.pdf', mimeType: 'application/pdf', sizeBytes: 1024 }),
        // A Google Doc has no bytes until exported; what matters is what it exports as.
        fileEntry({
          id: 'gdoc',
          name: 'Quarterly Notes',
          mimeType: 'application/vnd.google-apps.document',
          downloadMimeType: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
          sizeBytes: null,
        }),
        fileEntry({ id: 'zip', name: 'archive.zip', mimeType: 'application/zip', sizeBytes: 10 }),
        fileEntry({ id: 'huge', name: 'scan.pdf', mimeType: 'application/pdf', sizeBytes: 60 * 1024 * 1024 }),
        fileEntry({ id: 'form', name: 'Survey', mimeType: 'application/vnd.google-apps.form', selectable: false }),
      ],
      breadcrumbs: [],
      nextCursor: null,
    };

    function setupAttach(maxFiles = 5) {
      return setup(
        { browse: vi.fn().mockResolvedValue(LISTING) },
        { connector: CONNECTED, mode: 'attach', maxFiles },
      );
    }

    it('greys out what a conversation cannot take, judged on the downloaded type and size', async () => {
      const { component } = setupAttach();
      await vi.waitFor(() =>
        expect((component['displayedEntries'] as () => FileEntry[])()).toHaveLength(5),
      );

      const shown = (component['displayedEntries'] as () => FileEntry[])();
      const selectable = Object.fromEntries(shown.map((e) => [e.id, e.selectable]));
      expect(selectable).toEqual({ pdf: true, gdoc: true, zip: false, huge: false, form: false });
      expect((component['hasUnattachableEntries'] as () => boolean)()).toBe(true);
    });

    it('closes with the connector and the chosen files for the composer to import', async () => {
      const { component, dialogRef, fileSourceService } = setupAttach();
      await vi.waitFor(() =>
        expect((component['displayedEntries'] as () => FileEntry[])()).toHaveLength(5),
      );
      const shown = (component['displayedEntries'] as () => FileEntry[])();

      (component['toggleSelect'] as (e: FileEntry) => void)(shown[0]);
      (component['toggleSelect'] as (e: FileEntry) => void)(shown[1]);
      (component['toggleSelect'] as (e: FileEntry) => void)(shown[2]); // not selectable
      (component['confirmSelection'] as () => void)();

      expect(fileSourceService.importDocuments).not.toHaveBeenCalled();
      expect(dialogRef.close).toHaveBeenCalledWith({
        connector: CONNECTED,
        files: [
          { fileId: 'pdf', name: 'report.pdf' },
          { fileId: 'gdoc', name: 'Quarterly Notes' },
        ],
      });
    });

    it('stops at the message\'s remaining file slots', async () => {
      const { component, toast } = setupAttach(1);
      await vi.waitFor(() =>
        expect((component['displayedEntries'] as () => FileEntry[])()).toHaveLength(5),
      );
      const shown = (component['displayedEntries'] as () => FileEntry[])();

      (component['toggleSelect'] as (e: FileEntry) => void)(shown[0]);
      (component['toggleSelect'] as (e: FileEntry) => void)(shown[1]);

      expect((component['selectedCount'] as () => number)()).toBe(1);
      expect(toast.info).toHaveBeenCalledWith('File limit', expect.stringContaining('1 more file'));
    });
  });

  describe('opened through CDK Dialog', () => {
    function configure() {
      TestBed.resetTestingModule();
      TestBed.configureTestingModule({
        providers: [
          provideHttpClient(),
          provideHttpClientTesting(),
          {
            provide: FileSourceService,
            useValue: {
              listFileSources: vi.fn().mockResolvedValue([CONNECTED]),
              listRoots: vi.fn().mockResolvedValue([
                { id: 'root', name: 'My Drive' },
                { id: 'shared', name: 'Shared with me' },
              ]),
            },
          },
          { provide: UserConnectorsService, useValue: { initiateConsent: vi.fn() } },
          {
            provide: OAuthConsentService,
            useValue: {
              completion: signal<unknown>(null),
              inFlightProviders: signal(new Set<string>()),
              acknowledgeCompletion: vi.fn(),
            },
          },
          { provide: ToastService, useValue: { error: vi.fn(), success: vi.fn() } },
        ],
      });
    }

    afterEach(() => TestBed.inject(Dialog).closeAll());

    it('names the dialog from its heading in import mode', async () => {
      configure();
      const { container } = await openInCdkDialog<FileSourceBrowserDialogComponent, FileSourceBrowserDialogData>(
        FileSourceBrowserDialogComponent,
        { data: { target: 'AST-1' } },
      );
      expectNamedDialog(container, { name: 'Import from a connector' });
    });

    it('names the attach browser after the connector', async () => {
      configure();
      const { container } = await openInCdkDialog<FileSourceBrowserDialogComponent, FileSourceBrowserDialogData>(
        FileSourceBrowserDialogComponent,
        { data: { connector: CONNECTED, mode: 'attach', maxFiles: 5 } },
      );
      expectNamedDialog(container, { name: 'Add from Google Drive' });
    });

    it('names the folder picker after its purpose', async () => {
      configure();
      const { container } = await openInCdkDialog<FileSourceBrowserDialogComponent, FileSourceBrowserDialogData>(
        FileSourceBrowserDialogComponent,
        { data: { connector: CONNECTED, mode: 'pick-folder' } },
      );
      expectNamedDialog(container, { name: 'Choose a destination folder' });
    });
  });
});

describe('FileSourceBrowserDialogComponent — attach a whole folder', () => {
  beforeEach(() => TestBed.resetTestingModule());
  afterEach(() => TestBed.resetTestingModule());

  function folderButton(fixture: { nativeElement: HTMLElement }): HTMLButtonElement | null {
    return fixture.nativeElement.querySelector('[data-testid="file-source-attach-folder"]');
  }

  async function enterFolder(component: Record<string, never>, fixture: { detectChanges(): void }) {
    await (component['openFolder'] as (id: string, name: string) => Promise<void>)('fold-1', 'Week 1');
    fixture.detectChanges();
  }

  it('is not offered unless the composer allows a folder', async () => {
    const { fixture, component } = setup(
      { browse: vi.fn().mockResolvedValue({ entries: [], breadcrumbs: [{ id: 'root', name: 'My Drive' }], nextCursor: null }) },
      { connector: CONNECTED, mode: 'attach', maxFiles: 5 },
    );
    await enterFolder(component, fixture);
    expect(folderButton(fixture)).toBeNull();
  });

  it('closes with the folder, its name and its breadcrumb path', async () => {
    const { fixture, component, dialogRef } = setup(
      {
        listRoots: vi.fn().mockResolvedValue([{ id: 'root', name: 'My Drive' }, { id: 'shared', name: 'Shared' }]),
        browse: vi.fn().mockResolvedValue({ entries: [], breadcrumbs: [{ id: 'root', name: 'My Drive' }], nextCursor: null }),
      },
      { connector: CONNECTED, mode: 'attach', maxFiles: 5, allowFolder: true },
    );
    await vi.waitFor(() => expect((component['roots'] as () => unknown[])()).toHaveLength(2));
    fixture.detectChanges();
    // At the roots list there is no current folder yet.
    expect(folderButton(fixture)?.disabled).toBe(true);

    await enterFolder(component, fixture);
    const button = folderButton(fixture)!;
    expect(button.disabled).toBe(false);
    expect(button.getAttribute('aria-label')).toBe('Attach the folder Week 1');
    button.click();

    expect(dialogRef.close).toHaveBeenCalledWith({
      connector: CONNECTED,
      folder: { folderId: 'fold-1', folderName: 'Week 1', folderPath: 'My Drive/Week 1' },
    });
  });

  it('is disabled while files are ticked', async () => {
    const { fixture, component } = setup(
      { browse: vi.fn().mockResolvedValue({ entries: [fileEntry({ id: 'a', name: 'a.pdf', mimeType: 'application/pdf' })], breadcrumbs: [], nextCursor: null }) },
      { connector: CONNECTED, mode: 'attach', maxFiles: 5, allowFolder: true },
    );
    await enterFolder(component, fixture);
    (component['toggleSelect'] as (e: FileEntry) => void)(fileEntry({ id: 'a', name: 'a.pdf', mimeType: 'application/pdf' }));
    fixture.detectChanges();
    expect(folderButton(fixture)?.disabled).toBe(true);
  });
});
