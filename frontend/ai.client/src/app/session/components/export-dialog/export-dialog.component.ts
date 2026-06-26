import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  OnInit,
  signal,
} from '@angular/core';
import { Dialog, DIALOG_DATA, DialogRef } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowPath,
  heroArrowTopRightOnSquare,
  heroCheckCircle,
  heroCloudArrowUp,
  heroExclamationTriangle,
  heroFolder,
  heroLink,
  heroXMark,
} from '@ng-icons/heroicons/outline';
import { ExportError, ExportService } from '../../services/export/export.service';
import {
  DEFAULT_EXPORT_INCLUDE,
  ExportFormat,
  ExportInclude,
  ExportResponse,
  ExportTargetConnector,
} from '../../services/export/export.model';
import { UserConnectorsService } from '../../../settings/connectors/services/user-connectors.service';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { ToastService } from '../../../services/toast/toast.service';
import {
  FileSourceBrowserDialogComponent,
  FileSourceBrowserDialogData,
  FileSourceBrowserDialogResult,
  FolderSelection,
} from '../../../assistants/components/file-source-browser-dialog.component';
import { FileSourceConnector } from '../../../assistants/models/file-source.model';

/** Data passed in when the session list opens the export dialog. */
export interface ExportDialogData {
  sessionId: string;
  /** Conversation title, shown for context in the header. */
  title?: string;
}

/** Closed with the export result on success, or `undefined` if cancelled. */
export type ExportDialogResult = ExportResponse;

type ConnectPhase = 'initiating' | 'awaiting';

interface IncludeOption {
  key: keyof ExportInclude;
  label: string;
}

const FORMAT_LABELS: Record<ExportFormat, string> = {
  google_doc: 'Google Doc',
  markdown: 'Markdown',
  pdf: 'PDF',
};

/**
 * Modal that saves the current conversation transcript to a connected app
 * ("Save to…"). Flow: pick a destination connector → choose a format and what
 * to include → save. An unconnected (or expired) connector runs the shared
 * OAuth consent popup, then the save retries automatically. On success it
 * surfaces an "Open in <app>" link.
 *
 * Mirrors the file-source browser dialog's consent plumbing; the only data
 * flow that differs is the direction (write a transcript out vs. read files in).
 */
@Component({
  selector: 'app-export-dialog',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroArrowPath,
      heroArrowTopRightOnSquare,
      heroCheckCircle,
      heroCloudArrowUp,
      heroExclamationTriangle,
      heroFolder,
      heroLink,
      heroXMark,
    }),
  ],
  host: {
    class: 'block',
    '(keydown.escape)': 'cancel()',
  },
  template: `
    <!-- Backdrop -->
    <div
      class="dialog-backdrop fixed inset-0 bg-gray-500/75 dark:bg-gray-900/80"
      aria-hidden="true"
      (click)="cancel()"
    ></div>

    <!-- Centering wrapper -->
    <div class="fixed inset-0 z-10 flex min-h-full items-center justify-center p-4">
      <div
        class="dialog-panel relative flex max-h-[85vh] w-full max-w-md flex-col overflow-hidden rounded-2xl bg-white shadow-xl dark:bg-gray-800 dark:outline dark:-outline-offset-1 dark:outline-white/10"
        role="dialog"
        aria-modal="true"
        aria-labelledby="export-dialog-title"
        (click)="$event.stopPropagation()"
      >
        <!-- Header -->
        <div class="flex items-center justify-between gap-4 border-b border-gray-200 px-5 py-4 dark:border-gray-700">
          <div class="flex items-center gap-2">
            <ng-icon name="heroCloudArrowUp" class="size-5 text-gray-500 dark:text-gray-400" aria-hidden="true" />
            <h2 id="export-dialog-title" class="text-base/7 font-semibold text-gray-900 dark:text-white">
              Save conversation
            </h2>
          </div>
          <button
            type="button"
            (click)="cancel()"
            aria-label="Close"
            class="-mr-1 rounded-2xl p-1 text-gray-400 hover:bg-gray-100 hover:text-gray-600 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:hover:bg-gray-700 dark:hover:text-gray-200"
          >
            <ng-icon name="heroXMark" class="size-5" />
          </button>
        </div>

        <div class="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          @if (result(); as saved) {
            <!-- ─────────────── Success ─────────────── -->
            <div class="flex flex-col items-center gap-3 py-4 text-center">
              <ng-icon name="heroCheckCircle" class="size-10 text-green-600 dark:text-green-400" aria-hidden="true" />
              <p class="text-sm/6 font-medium text-gray-900 dark:text-white">
                Saved to {{ selectedTargetName() }}
              </p>
              <p class="text-sm/6 text-gray-500 dark:text-gray-400">{{ saved.name }}</p>
              @if (saved.webViewLink) {
                <a
                  [href]="saved.webViewLink"
                  target="_blank"
                  rel="noopener noreferrer"
                  class="inline-flex items-center gap-1.5 rounded-2xl bg-blue-600 px-3.5 py-2 text-sm/6 font-semibold text-white shadow-xs hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:bg-blue-500 dark:hover:bg-blue-600"
                >
                  <ng-icon name="heroArrowTopRightOnSquare" class="size-4" aria-hidden="true" />
                  Open in {{ selectedTargetName() }}
                </a>
              }
            </div>
          } @else if (loading()) {
            <div class="flex items-center gap-3 text-sm/6 text-gray-500 dark:text-gray-400">
              <div class="size-4 animate-spin rounded-full border-2 border-gray-300 border-t-blue-600 dark:border-gray-600 dark:border-t-blue-400"></div>
              Loading destinations…
            </div>
          } @else if (loadError(); as err) {
            <div class="flex items-start gap-3 rounded-2xl border border-red-200 bg-red-50 p-4 dark:border-red-800 dark:bg-red-900/20">
              <ng-icon name="heroExclamationTriangle" class="size-5 shrink-0 text-red-600 dark:text-red-400" aria-hidden="true" />
              <div>
                <p class="text-sm/6 text-red-700 dark:text-red-300">{{ err }}</p>
                <button
                  type="button"
                  (click)="loadTargets()"
                  class="mt-2 text-sm/6 font-medium text-red-700 underline hover:text-red-800 dark:text-red-200"
                >
                  Retry
                </button>
              </div>
            </div>
          } @else if (targets().length === 0) {
            <div class="rounded-2xl border border-dashed border-gray-300 bg-white p-8 text-center dark:border-gray-700 dark:bg-gray-800">
              <ng-icon name="heroLink" class="mx-auto size-8 text-gray-400" aria-hidden="true" />
              <p class="mt-3 text-sm/6 font-medium text-gray-700 dark:text-gray-300">No destinations available</p>
              <p class="mt-1 text-sm/6 text-gray-500 dark:text-gray-400">
                Ask an administrator to connect an app you can save conversations to.
              </p>
            </div>
          } @else {
            <!-- ─────────────── Destination ─────────────── -->
            <fieldset class="space-y-2">
              <legend class="mb-1.5 text-sm/6 font-medium text-gray-700 dark:text-gray-300">Destination</legend>
              @for (target of targets(); track target.providerId) {
                <label
                  class="flex cursor-pointer items-center gap-3 rounded-2xl border p-3 transition-colors"
                  [class]="selectedConnectorId() === target.providerId
                    ? 'border-blue-500 bg-blue-50 dark:border-blue-400 dark:bg-blue-500/10'
                    : 'border-gray-200 hover:bg-gray-50 dark:border-gray-700 dark:hover:bg-white/5'"
                >
                  <input
                    type="radio"
                    name="exportDestination"
                    class="size-4 text-blue-600 focus:ring-blue-500"
                    [value]="target.providerId"
                    [checked]="selectedConnectorId() === target.providerId"
                    (change)="selectConnector(target)"
                  />
                  <span class="flex-1 text-sm/6 font-medium text-gray-900 dark:text-white">{{ target.displayName }}</span>
                  @if (!target.connected) {
                    <span class="rounded-full bg-gray-100 px-2 py-0.5 text-xs/5 font-medium text-gray-500 dark:bg-gray-700 dark:text-gray-400">
                      Not connected
                    </span>
                  }
                </label>
              }
            </fieldset>

            <!-- ─────────────── Format ─────────────── -->
            @if (availableFormats().length > 0) {
              <div class="mt-4">
                <label for="export-format" class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Format</label>
                <select
                  id="export-format"
                  class="block w-full rounded-2xl border border-gray-300 bg-white px-3 py-2 text-sm/6 text-gray-900 focus:border-blue-500 focus:outline-hidden focus:ring-2 focus:ring-blue-500/40 dark:border-gray-600 dark:bg-gray-700 dark:text-white"
                  [value]="selectedFormat()"
                  (change)="onFormatChange($event)"
                >
                  @for (fmt of availableFormats(); track fmt) {
                    <option [value]="fmt">{{ formatLabel(fmt) }}</option>
                  }
                </select>
              </div>
            }

            <!-- ─────────────── Folder ─────────────── -->
            @if (canChooseFolder()) {
              <div class="mt-4">
                <span class="mb-1.5 block text-sm/6 font-medium text-gray-700 dark:text-gray-300">Folder</span>
                <div class="flex items-center gap-2 rounded-2xl border border-gray-300 bg-white px-3 py-2 dark:border-gray-600 dark:bg-gray-700">
                  <ng-icon name="heroFolder" class="size-4 shrink-0 text-gray-400" aria-hidden="true" />
                  <span class="min-w-0 flex-1 truncate text-sm/6 text-gray-900 dark:text-white">{{ destinationLabel() }}</span>
                  @if (destinationFolderName()) {
                    <button
                      type="button"
                      (click)="resetDestinationFolder()"
                      class="shrink-0 rounded-2xl px-2 py-1 text-sm/6 font-medium text-gray-500 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-gray-400 dark:hover:bg-gray-600 dark:hover:text-gray-200"
                    >
                      Default
                    </button>
                  }
                  <button
                    type="button"
                    (click)="chooseFolder()"
                    class="shrink-0 rounded-2xl px-2 py-1 text-sm/6 font-medium text-blue-600 hover:bg-blue-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:text-blue-400 dark:hover:bg-blue-500/10"
                  >
                    Choose…
                  </button>
                </div>
              </div>
            }

            <!-- ─────────────── Include ─────────────── -->
            <fieldset class="mt-4">
              <legend class="mb-1.5 text-sm/6 font-medium text-gray-700 dark:text-gray-300">Include</legend>
              <div class="space-y-1.5">
                <!-- Messages are the transcript itself: always on, shown disabled. -->
                <label class="flex items-center gap-2.5 text-sm/6 text-gray-500 dark:text-gray-400">
                  <input type="checkbox" checked disabled class="size-4 rounded-sm text-blue-600" />
                  Messages
                </label>
                @for (opt of includeOptions; track opt.key) {
                  <label class="flex cursor-pointer items-center gap-2.5 text-sm/6 text-gray-700 dark:text-gray-300">
                    <input
                      type="checkbox"
                      class="size-4 rounded-sm text-blue-600 focus:ring-blue-500"
                      [checked]="include()[opt.key]"
                      (change)="toggleInclude(opt.key)"
                    />
                    {{ opt.label }}
                  </label>
                }
              </div>
            </fieldset>

            @if (error(); as err) {
              <div class="mt-4 flex items-start gap-2.5 rounded-2xl border border-red-200 bg-red-50 p-3 dark:border-red-800 dark:bg-red-900/20">
                <ng-icon name="heroExclamationTriangle" class="size-5 shrink-0 text-red-600 dark:text-red-400" aria-hidden="true" />
                <p class="text-sm/6 text-red-700 dark:text-red-300">{{ err }}</p>
              </div>
            }
          }
        </div>

        <!-- Footer -->
        <div class="flex justify-end gap-3 border-t border-gray-200 px-5 py-4 dark:border-gray-700">
          <button
            type="button"
            (click)="cancel()"
            class="rounded-2xl bg-white px-3.5 py-2 text-sm/6 font-semibold text-gray-900 shadow-xs ring-1 ring-gray-300 ring-inset hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 dark:bg-white/10 dark:text-white dark:ring-white/10 dark:hover:bg-white/20"
          >
            {{ result() ? 'Done' : 'Cancel' }}
          </button>
          @if (!result()) {
            <button
              type="button"
              (click)="save()"
              [disabled]="!canSubmit()"
              class="inline-flex items-center gap-1.5 rounded-2xl bg-blue-600 px-3.5 py-2 text-sm/6 font-semibold text-white shadow-xs hover:bg-blue-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-blue-500 dark:hover:bg-blue-600"
            >
              @if (busy()) {
                <ng-icon name="heroArrowPath" class="size-4 animate-spin" aria-hidden="true" />
              }
              {{ busyLabel() }}
            </button>
          }
        </div>
      </div>
    </div>
  `,
  styles: `
    @import 'tailwindcss';
    @custom-variant dark (&:where(.dark, .dark *));

    .dialog-backdrop {
      animation: backdrop-fade-in 200ms ease-out;
    }
    @keyframes backdrop-fade-in {
      from { opacity: 0; }
      to { opacity: 1; }
    }
    .dialog-panel {
      animation: dialog-fade-in-up 200ms ease-out;
    }
    @keyframes dialog-fade-in-up {
      from { opacity: 0; transform: translateY(1rem) scale(0.95); }
      to { opacity: 1; transform: translateY(0) scale(1); }
    }
  `,
})
export class ExportDialogComponent implements OnInit {
  private readonly dialogRef = inject<DialogRef<ExportDialogResult>>(DialogRef);
  private readonly data = inject<ExportDialogData>(DIALOG_DATA);
  private readonly exportService = inject(ExportService);
  private readonly connectorsService = inject(UserConnectorsService);
  private readonly consentService = inject(OAuthConsentService);
  private readonly toast = inject(ToastService);
  private readonly dialog = inject(Dialog);

  protected readonly targets = signal<ExportTargetConnector[]>([]);
  protected readonly loading = signal<boolean>(true);
  protected readonly loadError = signal<string | null>(null);

  protected readonly selectedConnectorId = signal<string | null>(null);
  protected readonly selectedFormat = signal<ExportFormat | null>(null);
  protected readonly include = signal<ExportInclude>({ ...DEFAULT_EXPORT_INCLUDE });

  /**
   * Chosen destination folder for a browsable target. `null` means the
   * adapter's default app folder — the common case, so no picker interaction
   * is required to save.
   */
  protected readonly parentId = signal<string | null>(null);
  protected readonly destinationFolderName = signal<string | null>(null);

  protected readonly submitting = signal<boolean>(false);
  protected readonly error = signal<string | null>(null);
  protected readonly result = signal<ExportResponse | null>(null);

  /** Provider whose consent popup is in flight, plus the UI phase. */
  protected readonly connectingId = signal<string | null>(null);
  protected readonly connectPhase = signal<ConnectPhase | null>(null);

  protected readonly includeOptions: readonly IncludeOption[] = [
    { key: 'toolCalls', label: 'Tool calls & results' },
    { key: 'images', label: 'Images' },
    { key: 'citations', label: 'Citations' },
    { key: 'reasoning', label: 'Reasoning' },
    { key: 'timestamps', label: 'Timestamps' },
  ];

  protected readonly selectedTarget = computed<ExportTargetConnector | null>(
    () => this.targets().find((t) => t.providerId === this.selectedConnectorId()) ?? null,
  );

  protected readonly selectedTargetName = computed<string>(
    () => this.selectedTarget()?.displayName ?? 'the destination',
  );

  protected readonly availableFormats = computed<ExportFormat[]>(
    () => this.selectedTarget()?.supportedFormats ?? [],
  );

  /** A folder picker is offered only for combined-scope (browsable) targets. */
  protected readonly canChooseFolder = computed<boolean>(
    () => this.selectedTarget()?.browsable ?? false,
  );

  /** Folder shown in the destination row; the app default until one is picked. */
  protected readonly destinationLabel = computed<string>(
    () => this.destinationFolderName() ?? 'App folder',
  );

  /** True while either the save request or a consent popup is in flight. */
  protected readonly busy = computed<boolean>(
    () => this.submitting() || this.connectPhase() !== null,
  );

  protected readonly canSubmit = computed<boolean>(
    () => !!this.selectedTarget() && !!this.selectedFormat() && !this.busy(),
  );

  protected readonly busyLabel = computed<string>(() => {
    if (this.connectPhase()) {
      return 'Connecting…';
    }
    if (this.submitting()) {
      return 'Saving…';
    }
    return 'Save';
  });

  constructor() {
    // Drive the connect flow off the shared consent service: the
    // `/oauth-complete` popup broadcasts a completion the service surfaces
    // here. Mirrors the file-source browser dialog. On success we retry the
    // save now that the connector is authorized.
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
        this.markConnected(connecting);
        void this.save();
      } else {
        this.error.set(completion.error ?? 'Could not connect the destination.');
      }
    });

    // If the user closes the popup without finishing, the consent service
    // drops the provider from `inFlightProviders`. Reset the phase so the
    // Save button becomes interactive again.
    effect(() => {
      const inFlight = this.consentService.inFlightProviders();
      const connecting = this.connectingId();
      if (connecting && this.connectPhase() === 'awaiting' && !inFlight.has(connecting)) {
        this.connectingId.set(null);
        this.connectPhase.set(null);
      }
    });
  }

  ngOnInit(): void {
    void this.loadTargets();
  }

  protected async loadTargets(): Promise<void> {
    this.loading.set(true);
    this.loadError.set(null);
    try {
      const targets = await this.exportService.listExportTargets();
      this.targets.set(targets);
      // Auto-select when there's only one destination — the common case.
      if (targets.length === 1) {
        this.selectConnector(targets[0]);
      }
    } catch (err) {
      this.loadError.set(this.errorMessage(err));
    } finally {
      this.loading.set(false);
    }
  }

  protected selectConnector(target: ExportTargetConnector): void {
    this.selectedConnectorId.set(target.providerId);
    this.error.set(null);
    // A folder is destination-specific; reset to the app default when the
    // destination changes.
    this.resetDestinationFolder();
    // Prefer a native Google Doc when offered, else the first supported format.
    const formats = target.supportedFormats;
    const preferred: ExportFormat | null = formats.includes('google_doc')
      ? 'google_doc'
      : (formats[0] ?? null);
    this.selectedFormat.set(preferred);
  }

  /** Clear any chosen folder, falling back to the adapter's app folder. */
  protected resetDestinationFolder(): void {
    this.parentId.set(null);
    this.destinationFolderName.set(null);
  }

  /**
   * Open the import browse dialog in folder-pick mode to choose a destination
   * folder. Only offered for browsable (combined-scope) targets, where the
   * file-source browse endpoints resolve. A successful pick implies the
   * connector is authorized, so we reconcile the connected flag too.
   */
  protected async chooseFolder(): Promise<void> {
    const target = this.selectedTarget();
    if (!target || !target.browsable) {
      return;
    }
    const connector: FileSourceConnector = {
      providerId: target.providerId,
      displayName: target.displayName,
      iconName: target.iconName,
      iconData: target.iconData,
      connected: target.connected,
    };
    const ref = this.dialog.open<
      FileSourceBrowserDialogResult,
      FileSourceBrowserDialogData
    >(FileSourceBrowserDialogComponent, {
      data: { connector, mode: 'pick-folder' },
      hasBackdrop: false,
    });
    const result = await firstValueFrom(ref.closed);
    // Cancelled, or closed in import mode (never happens here) — leave as-is.
    if (!result || Array.isArray(result)) {
      return;
    }
    const selection: FolderSelection = result;
    this.parentId.set(selection.folderId);
    this.destinationFolderName.set(selection.folderName);
    // Browsing succeeded, so the token is good — keep local state honest.
    this.markConnected(target.providerId);
  }

  protected onFormatChange(event: Event): void {
    this.selectedFormat.set((event.target as HTMLSelectElement).value as ExportFormat);
  }

  protected toggleInclude(key: keyof ExportInclude): void {
    this.include.update((inc) => ({ ...inc, [key]: !inc[key] }));
  }

  protected formatLabel(fmt: ExportFormat): string {
    return FORMAT_LABELS[fmt] ?? fmt;
  }

  protected async save(): Promise<void> {
    const target = this.selectedTarget();
    const format = this.selectedFormat();
    if (!target || !format || this.busy()) {
      return;
    }

    // An unconnected destination needs consent first; the completion effect
    // re-enters save() once authorized.
    if (!target.connected) {
      await this.connect(target);
      return;
    }

    this.submitting.set(true);
    this.error.set(null);
    try {
      const response = await this.exportService.exportSession(this.data.sessionId, {
        connectorId: target.providerId,
        format,
        parentId: this.parentId() ?? undefined,
        include: this.include(),
      });
      this.result.set(response);
    } catch (err) {
      if (err instanceof ExportError && err.status === 409) {
        // The catalog said connected, but the token expired or was revoked
        // before we saved. Flip it to unconnected and run consent → retry.
        this.submitting.set(false);
        this.markConnected(target.providerId, false);
        await this.connect(target);
        return;
      }
      this.error.set(this.errorMessage(err));
    } finally {
      this.submitting.set(false);
    }
  }

  /** Kick off the OAuth consent popup for a destination connector. */
  private async connect(target: ExportTargetConnector): Promise<void> {
    this.connectingId.set(target.providerId);
    this.connectPhase.set('initiating');
    this.error.set(null);
    try {
      const result = await this.connectorsService.initiateConsent(target.providerId);
      if (result.connected) {
        // Authorized in another tab while we were here — proceed straight to save.
        this.connectingId.set(null);
        this.connectPhase.set(null);
        this.markConnected(target.providerId);
        await this.save();
        return;
      }
      if (!result.authorizationUrl) {
        this.connectingId.set(null);
        this.connectPhase.set(null);
        this.toast.error('Unexpected response from the server.');
        return;
      }
      this.consentService.requestConsent(target.providerId, result.authorizationUrl);
      void this.consentService.openConsentPopup(target.providerId);
      this.connectPhase.set('awaiting');
    } catch (err) {
      this.connectingId.set(null);
      this.connectPhase.set(null);
      this.error.set(this.errorMessage(err));
    }
  }

  /** Flip a destination's connected flag in local state. */
  private markConnected(providerId: string, connected = true): void {
    this.targets.update((list) =>
      list.map((t) => (t.providerId === providerId ? { ...t, connected } : t)),
    );
  }

  protected cancel(): void {
    this.dialogRef.close(this.result() ?? undefined);
  }

  private errorMessage(err: unknown): string {
    if (err instanceof ExportError) {
      if (err.status === 409) {
        return 'This destination needs to be connected before you can save to it.';
      }
      return err.message;
    }
    if (err instanceof Error) {
      return err.message;
    }
    return 'Something went wrong. Try again.';
  }
}
