import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  signal,
} from '@angular/core';
import { DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowDownTray,
  heroArrowLeft,
  heroArrowPath,
  heroChevronRight,
  heroCloud,
  heroDocument,
  heroExclamationTriangle,
  heroFolder,
  heroHome,
  heroLink,
  heroMagnifyingGlass,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { FileSourceService, FileSourceError } from '../services/file-source.service';
import {
  Breadcrumb,
  FileEntry,
  FileSourceConnector,
  ImportFileRef,
  SourceRoot,
} from '../models/file-source.model';
import { Document } from '../models/document.model';
import { UserConnectorsService } from '../../settings/connectors/services/user-connectors.service';
import { OAuthConsentService } from '../../services/oauth-consent/oauth-consent.service';
import { ToastService } from '../../services/toast/toast.service';

/**
 * What the dialog is for. `import` (default) multi-selects files to ingest
 * into an assistant; `pick-folder` walks the same folder tree but selects a
 * single destination folder (the conversation-export folder picker) and never
 * imports anything.
 */
export type FileSourceBrowserMode = 'import' | 'pick-folder';

/** Data passed in when a caller opens the browser. */
export interface FileSourceBrowserDialogData {
  /** Required in `import` mode — the assistant the files are imported into. */
  assistantId?: string;
  /**
   * When set, the dialog opens straight into this connector — its folder
   * browser if already connected, or an inline Connect prompt if not — and
   * hides the source picker. The assistant editor surfaces each connector as
   * its own button, so the in-modal source list is redundant when targeted.
   * `pick-folder` mode always targets a connector this way.
   */
  connector?: FileSourceConnector;
  /** Defaults to `import`. */
  mode?: FileSourceBrowserMode;
}

/** A destination folder chosen in `pick-folder` mode. */
export interface FolderSelection {
  /** Provider folder id to write into; a provider root id is valid too. */
  folderId: string;
  /** Human-readable name of the chosen folder, for display. */
  folderName: string;
}

/**
 * Closed value: the imported {@link Document} records in `import` mode, a
 * {@link FolderSelection} in `pick-folder` mode, or `undefined` if cancelled.
 */
export type FileSourceBrowserDialogResult = Document[] | FolderSelection;

type DialogView = 'sources' | 'browser';
type ConnectPhase = 'initiating' | 'awaiting';

/**
 * Modal that walks a connected file source's folder tree.
 *
 * In `import` mode (default) the user multi-selects files to ingest into an
 * assistant's RAG index. In `pick-folder` mode the same browser is reused to
 * choose a single destination folder — the conversation-export folder picker —
 * and nothing is imported.
 *
 * Flow: pick a file source (connecting it via the OAuth consent popup if
 * needed) → browse the provider's folder tree or search → either multi-select
 * files and import, or select the current folder. Closes with the created
 * {@link Document} records, a {@link FolderSelection}, or `undefined` if
 * cancelled.
 */
@Component({
  selector: 'app-file-source-browser-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroArrowDownTray,
      heroArrowLeft,
      heroArrowPath,
      heroChevronRight,
      heroCloud,
      heroDocument,
      heroExclamationTriangle,
      heroFolder,
      heroHome,
      heroLink,
      heroMagnifyingGlass,
      heroXMark,
    }),
  ],
  host: {
    class: 'block',
    '(keydown.escape)': 'cancel()',
  },
  templateUrl: './file-source-browser-dialog.component.html',
  styles: `
    @import 'tailwindcss';

    @custom-variant dark (&:where(.dark, .dark *));

    .dialog-backdrop {
      animation: backdrop-fade-in 200ms ease-out;
    }

    @keyframes backdrop-fade-in {
      from {
        opacity: 0;
      }
      to {
        opacity: 1;
      }
    }

    .dialog-panel {
      animation: dialog-fade-in-up 200ms ease-out;
    }

    @keyframes dialog-fade-in-up {
      from {
        opacity: 0;
        transform: translateY(1rem) scale(0.95);
      }
      to {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
    }
  `,
})
export class FileSourceBrowserDialogComponent {
  private readonly dialogRef =
    inject<DialogRef<FileSourceBrowserDialogResult>>(DialogRef);
  private readonly data = inject<FileSourceBrowserDialogData>(DIALOG_DATA);
  private readonly fileSourceService = inject(FileSourceService);
  private readonly connectorsService = inject(UserConnectorsService);
  private readonly consentService = inject(OAuthConsentService);
  private readonly toast = inject(ToastService);

  /** `import` (default) or `pick-folder` for the export destination picker. */
  protected readonly mode: FileSourceBrowserMode = this.data.mode ?? 'import';
  /** True when choosing a destination folder rather than importing files. */
  protected readonly isPicker = this.mode === 'pick-folder';

  /** True when the dialog was opened targeting a single connector. */
  protected readonly lockedToConnector = !!this.data.connector;

  // --- View state -----------------------------------------------------------
  protected readonly view = signal<DialogView>('sources');

  // --- Source catalog -------------------------------------------------------
  protected readonly sources = signal<FileSourceConnector[]>([]);
  protected readonly sourcesLoading = signal<boolean>(true);
  protected readonly sourcesError = signal<string | null>(null);

  /** Provider whose consent popup is in flight, plus the UI phase. */
  protected readonly connectingId = signal<string | null>(null);
  protected readonly connectPhase = signal<ConnectPhase | null>(null);

  // --- Browser state --------------------------------------------------------
  protected readonly connector = signal<FileSourceConnector | null>(null);
  protected readonly roots = signal<SourceRoot[]>([]);
  /** Current folder id; `null` means the top-level roots list is shown. */
  protected readonly currentFolderId = signal<string | null>(null);
  /**
   * Name of the current folder, tracked client-side as the user navigates (the
   * adapter returns a flat page, not a path). Drives the `pick-folder`
   * selection label; empty at the roots list.
   */
  protected readonly currentFolderName = signal<string>('');
  protected readonly breadcrumbs = signal<Breadcrumb[]>([]);
  protected readonly entries = signal<FileEntry[]>([]);
  protected readonly nextCursor = signal<string | null>(null);
  protected readonly browserLoading = signal<boolean>(false);
  protected readonly loadingMore = signal<boolean>(false);
  protected readonly browserError = signal<string | null>(null);
  /**
   * True when {@link browserError} is a 409 the user can fix in place — the
   * catalog reported the connector as connected, but its token expired or was
   * revoked before they drilled in. Drives an inline Connect button.
   */
  protected readonly browserErrorConnectable = signal<boolean>(false);

  // --- Search ---------------------------------------------------------------
  protected readonly searchTerm = signal<string>('');
  /** The executed query; `null` when not in search mode. */
  protected readonly activeSearch = signal<string | null>(null);

  // --- Selection / import ---------------------------------------------------
  protected readonly selected = signal<Map<string, ImportFileRef>>(new Map());
  protected readonly importing = signal<boolean>(false);

  /** True when the top-level roots list (not a folder or search) is shown. */
  protected readonly atRoots = computed(
    () => this.activeSearch() === null && this.currentFolderId() === null,
  );

  /** Entries to render — roots rendered as folders when at the top level. */
  protected readonly displayedEntries = computed<FileEntry[]>(() => {
    if (this.atRoots()) {
      return this.roots().map((root) => ({
        id: root.id,
        name: root.name,
        type: 'folder' as const,
        selectable: false,
      }));
    }
    // The folder picker navigates folders only; files would just be noise.
    if (this.isPicker) {
      return this.entries().filter((entry) => entry.type === 'folder');
    }
    return this.entries();
  });

  protected readonly selectedCount = computed(() => this.selected().size);

  /** True once the user has drilled into a folder/root they can select. */
  protected readonly canPickCurrentFolder = computed(
    () => this.currentFolderId() !== null && !this.browserLoading(),
  );

  constructor() {
    const preselected = this.data.connector;
    if (preselected) {
      // Opened from a connector button in the editor — skip the source
      // picker and go straight to this connector.
      this.connector.set(preselected);
      this.view.set('browser');
      if (preselected.connected) {
        void this.enterBrowser(preselected);
      } else {
        // Surface the inline Connect prompt the browser view already renders
        // for an expired/revoked token.
        this.browserError.set(
          'This file source needs to be connected before you can browse it.',
        );
        this.browserErrorConnectable.set(true);
      }
    } else {
      void this.loadSources();
    }

    // Drive the connect flow off the shared consent service: the
    // `/oauth-complete` popup broadcasts a completion the service surfaces
    // here. Mirrors the connectors settings page.
    effect(() => {
      const completion = this.consentService.completion();
      if (!completion || !completion.providerId) {
        return;
      }
      const connecting = this.connectingId();
      if (completion.providerId !== connecting) {
        return;
      }
      this.consentService.acknowledgeCompletion();
      this.connectingId.set(null);
      this.connectPhase.set(null);
      if (completion.status === 'success') {
        const conn = this.sources().find((s) => s.providerId === connecting);
        if (conn) {
          const connected = { ...conn, connected: true };
          this.sources.update((list) =>
            list.map((s) => (s.providerId === connecting ? connected : s)),
          );
          void this.enterBrowser(connected);
        }
      } else {
        this.toast.error(completion.error ?? 'Could not connect the file source.');
      }
    });

    // If the user closes the popup without finishing, the consent service
    // drops the provider from `inFlightProviders`. Reset the UI phase so the
    // Connect button becomes interactive again.
    effect(() => {
      const inFlight = this.consentService.inFlightProviders();
      const connecting = this.connectingId();
      if (connecting && this.connectPhase() === 'awaiting' && !inFlight.has(connecting)) {
        this.connectPhase.set(null);
      }
    });
  }

  // --- Source catalog -------------------------------------------------------

  protected async loadSources(): Promise<void> {
    this.sourcesLoading.set(true);
    this.sourcesError.set(null);
    try {
      this.sources.set(await this.fileSourceService.listFileSources());
    } catch (err) {
      this.sourcesError.set(this.errorMessage(err));
    } finally {
      this.sourcesLoading.set(false);
    }
  }

  /** Browse a connected source, or kick off consent for an unconnected one. */
  protected useSource(connector: FileSourceConnector): void {
    if (connector.connected) {
      void this.enterBrowser(connector);
    } else {
      void this.connect(connector);
    }
  }

  private async connect(connector: FileSourceConnector): Promise<void> {
    this.connectingId.set(connector.providerId);
    this.connectPhase.set('initiating');
    try {
      const result = await this.connectorsService.initiateConsent(connector.providerId);
      if (result.connected) {
        this.connectingId.set(null);
        this.connectPhase.set(null);
        await this.enterBrowser({ ...connector, connected: true });
        return;
      }
      if (!result.authorizationUrl) {
        this.connectingId.set(null);
        this.connectPhase.set(null);
        this.toast.error('Unexpected response from the server.');
        return;
      }
      this.consentService.requestConsent(connector.providerId, result.authorizationUrl);
      void this.consentService.openConsentPopup(connector.providerId);
      this.connectPhase.set('awaiting');
    } catch (err) {
      this.connectingId.set(null);
      this.connectPhase.set(null);
      this.toast.error(this.errorMessage(err));
    }
  }

  // --- Browser --------------------------------------------------------------

  private async enterBrowser(connector: FileSourceConnector): Promise<void> {
    this.connector.set(connector);
    this.resetBrowserState();
    this.view.set('browser');
    this.browserLoading.set(true);
    try {
      const roots = await this.fileSourceService.listRoots(connector.providerId);
      this.roots.set(roots);
      // A single root is an implementation detail — drop the user straight in.
      if (roots.length === 1) {
        await this.openFolder(roots[0].id, roots[0].name);
      } else {
        this.browserLoading.set(false);
      }
    } catch (err) {
      this.browserError.set(this.errorMessage(err));
      this.browserErrorConnectable.set(
        err instanceof FileSourceError && err.status === 409,
      );
      this.browserLoading.set(false);
    }
  }

  /** Re-run the consent flow for the connector currently failing in the browser. */
  protected reconnect(): void {
    const connector = this.connector();
    if (connector) {
      void this.connect(connector);
    }
  }

  protected async openFolder(folderId: string, folderName = ''): Promise<void> {
    const connector = this.connector();
    if (!connector) {
      return;
    }
    this.activeSearch.set(null);
    this.searchTerm.set('');
    this.currentFolderId.set(folderId);
    this.currentFolderName.set(folderName);
    this.entries.set([]);
    this.breadcrumbs.set([]);
    this.nextCursor.set(null);
    this.browserLoading.set(true);
    this.browserError.set(null);
    this.browserErrorConnectable.set(false);
    try {
      const result = await this.fileSourceService.browse(connector.providerId, folderId);
      this.entries.set(result.entries);
      this.breadcrumbs.set(result.breadcrumbs);
      this.nextCursor.set(result.nextCursor ?? null);
    } catch (err) {
      this.browserError.set(this.errorMessage(err));
    } finally {
      this.browserLoading.set(false);
    }
  }

  /** Return to the top-level roots list. */
  protected goToRoots(): void {
    this.activeSearch.set(null);
    this.searchTerm.set('');
    this.currentFolderId.set(null);
    this.currentFolderName.set('');
    this.entries.set([]);
    this.breadcrumbs.set([]);
    this.nextCursor.set(null);
    this.browserError.set(null);
    this.browserErrorConnectable.set(false);
  }

  protected onEntryActivate(entry: FileEntry): void {
    if (entry.type === 'folder') {
      void this.openFolder(entry.id, entry.name);
    } else {
      this.toggleSelect(entry);
    }
  }

  /** Close with the current folder as the chosen export destination. */
  protected pickCurrentFolder(): void {
    const folderId = this.currentFolderId();
    if (folderId === null) {
      return;
    }
    this.dialogRef.close({
      folderId,
      folderName: this.currentFolderName() || 'Selected folder',
    });
  }

  protected toggleSelect(entry: FileEntry): void {
    if (entry.type !== 'file' || !entry.selectable) {
      return;
    }
    this.selected.update((map) => {
      const next = new Map(map);
      if (next.has(entry.id)) {
        next.delete(entry.id);
      } else {
        next.set(entry.id, { fileId: entry.id, name: entry.name });
      }
      return next;
    });
  }

  protected isSelected(entryId: string): boolean {
    return this.selected().has(entryId);
  }

  // --- Search ---------------------------------------------------------------

  protected onSearchInput(value: string): void {
    this.searchTerm.set(value);
  }

  protected async runSearch(): Promise<void> {
    const connector = this.connector();
    const term = this.searchTerm().trim();
    if (!connector || term.length === 0) {
      return;
    }
    this.activeSearch.set(term);
    this.currentFolderId.set(null);
    this.entries.set([]);
    this.breadcrumbs.set([]);
    this.nextCursor.set(null);
    this.browserLoading.set(true);
    this.browserError.set(null);
    this.browserErrorConnectable.set(false);
    try {
      const result = await this.fileSourceService.search(connector.providerId, term);
      this.entries.set(result.entries);
      this.nextCursor.set(result.nextCursor ?? null);
    } catch (err) {
      this.browserError.set(this.errorMessage(err));
    } finally {
      this.browserLoading.set(false);
    }
  }

  protected clearSearch(): void {
    this.searchTerm.set('');
    this.activeSearch.set(null);
    this.goToRoots();
  }

  // --- Pagination -----------------------------------------------------------

  protected async loadMore(): Promise<void> {
    const connector = this.connector();
    const cursor = this.nextCursor();
    if (!connector || !cursor || this.loadingMore()) {
      return;
    }
    this.loadingMore.set(true);
    try {
      const search = this.activeSearch();
      const result = search
        ? await this.fileSourceService.search(connector.providerId, search, cursor)
        : await this.fileSourceService.browse(
            connector.providerId,
            this.currentFolderId() ?? '',
            cursor,
          );
      this.entries.update((current) => [...current, ...result.entries]);
      this.nextCursor.set(result.nextCursor ?? null);
    } catch (err) {
      this.toast.error(this.errorMessage(err));
    } finally {
      this.loadingMore.set(false);
    }
  }

  // --- Import ---------------------------------------------------------------

  protected async importSelected(): Promise<void> {
    const connector = this.connector();
    const assistantId = this.data.assistantId;
    const files = [...this.selected().values()];
    if (!connector || !assistantId || files.length === 0 || this.importing()) {
      return;
    }
    this.importing.set(true);
    try {
      const response = await this.fileSourceService.importDocuments(
        assistantId,
        connector.providerId,
        files,
      );
      this.dialogRef.close(response.documents);
    } catch (err) {
      this.importing.set(false);
      this.toast.error(this.errorMessage(err));
    }
  }

  // --- Navigation -----------------------------------------------------------

  protected backToSources(): void {
    this.view.set('sources');
    this.connector.set(null);
    this.resetBrowserState();
  }

  protected cancel(): void {
    this.dialogRef.close();
  }

  // --- Helpers --------------------------------------------------------------

  private resetBrowserState(): void {
    this.roots.set([]);
    this.currentFolderId.set(null);
    this.currentFolderName.set('');
    this.breadcrumbs.set([]);
    this.entries.set([]);
    this.nextCursor.set(null);
    this.browserError.set(null);
    this.browserErrorConnectable.set(false);
    this.searchTerm.set('');
    this.activeSearch.set(null);
    this.selected.set(new Map());
  }

  private errorMessage(err: unknown): string {
    if (err instanceof FileSourceError) {
      if (err.status === 409) {
        return 'This file source needs to be connected before you can browse it.';
      }
      return err.message;
    }
    if (err instanceof Error) {
      return err.message;
    }
    return 'Something went wrong. Try again.';
  }

  protected formatSize(bytes: number | null | undefined): string {
    if (bytes == null || bytes <= 0) {
      return '';
    }
    const units = ['B', 'KB', 'MB', 'GB'];
    let value = bytes;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit++;
    }
    return `${value.toFixed(value < 10 && unit > 0 ? 1 : 0)} ${units[unit]}`;
  }
}
