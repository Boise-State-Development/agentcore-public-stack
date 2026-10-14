import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { CdkMenu, CdkMenuItem, CdkMenuTrigger } from '@angular/cdk/menu';
import { ConnectedPosition } from '@angular/cdk/overlay';
import { Router } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroArrowTopRightOnSquare,
  heroCheck,
  heroChevronRight,
  heroCloud,
  heroCog6Tooth,
  heroLink,
  heroPaperClip,
  heroPlus,
  heroSparkles,
  heroWrenchScrewdriver,
} from '@ng-icons/heroicons/outline';
import { firstValueFrom } from 'rxjs';
import {
  AttachSelection,
  FileSourceBrowserDialogComponent,
  FileSourceBrowserDialogData,
  FileSourceBrowserDialogResult,
} from '../../../assistants/components/file-source-browser-dialog.component';
import { FileSourceConnector } from '../../../assistants/models/file-source.model';
import { FileSourceService } from '../../../assistants/services/file-source.service';
import { TooltipDirective } from '../../../components/tooltip';
import { UserConnector } from '../../../settings/connectors/models/user-connector.model';
import { ConnectorStatusService } from '../../../settings/connectors/services/connector-status.service';
import { UserConnectorsService } from '../../../settings/connectors/services/user-connectors.service';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { MAX_FILES_PER_MESSAGE } from '../../../services/file-upload';
import { SkillCommand, SkillCommandService } from '../../../services/skill/skill-command.service';
import { ToastService } from '../../../services/toast/toast.service';
import { Tool, ToolService, isRetiring } from '../../../services/tool/tool.service';
import { MenuToggleItemDirective } from './menu-toggle-item.directive';

/** What a connector row is doing while its switch is not yet settled. */
type ConnectorPhase = 'initiating' | 'awaiting' | 'disconnecting';

/**
 * The composer's `+` menu: everything a person can add to the message or
 * change about how it will be answered, in one place.
 *
 * Modelled on the claude.ai composer, with the rows this stack has: files from
 * the device, files from each connected file source (a connector an admin has
 * mapped to a file-source adapter, such as Google Drive), then the three
 * Customize tabs as submenus — Skills to run, Tools to switch on or off, and
 * Connectors as switches too: on runs the consent popup, off disconnects. The
 * `/` and `@` autocompletes stay; this is the discoverable path to the same
 * things.
 *
 * It owns no attachment state. The composer still owns the hidden file input,
 * the upload list and the draft; this component only tells it what was
 * chosen (`pickFromDevice`, `sourceFilesPicked`, `skillPicked`). That keeps
 * the composer's specs, which stub every child, untouched by the services
 * this menu reads.
 *
 * Lists load when the menu opens, not when the composer renders: the file
 * sources call asks AgentCore for each connector's token state, and most
 * composers are never opened here.
 */
@Component({
  selector: 'app-composer-add-menu',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CdkMenu, CdkMenuItem, CdkMenuTrigger, MenuToggleItemDirective, NgIcon, TooltipDirective],
  providers: [
    provideIcons({
      heroArrowTopRightOnSquare,
      heroCheck,
      heroChevronRight,
      heroCloud,
      heroCog6Tooth,
      heroLink,
      heroPaperClip,
      heroPlus,
      heroSparkles,
      heroWrenchScrewdriver,
    }),
  ],
  host: { class: 'contents' },
  templateUrl: './composer-add-menu.component.html',
})
export class ComposerAddMenuComponent {
  private readonly dialog = inject(Dialog);
  private readonly router = inject(Router);
  private readonly toast = inject(ToastService);
  private readonly fileSourceService = inject(FileSourceService);
  private readonly skillCommands = inject(SkillCommandService);
  private readonly toolService = inject(ToolService);
  private readonly connectorsService = inject(UserConnectorsService);
  private readonly connectorStatus = inject(ConnectorStatusService);
  private readonly consentService = inject(OAuthConsentService);

  /** How many more files the message can take; zero disables the file rows. */
  readonly remainingFiles = input<number>(MAX_FILES_PER_MESSAGE);
  /** Offer the Skills submenu. Off where the composer hides `/` commands too. */
  readonly showSkills = input<boolean>(true);
  /** The whole trigger, e.g. while dictating. */
  readonly disabled = input<boolean>(false);

  /** Open the device file picker (the composer owns the hidden input). */
  readonly pickFromDevice = output<void>();
  /** Files chosen from a connected file source, for the composer to attach. */
  readonly sourceFilesPicked = output<AttachSelection>();
  /** A skill to write into the message as its `/command`. */
  readonly skillPicked = output<SkillCommand>();

  protected readonly open = signal(false);

  // --- File sources -----------------------------------------------------------

  protected readonly fileSources = signal<FileSourceConnector[]>([]);
  private fileSourcesLoaded = false;

  // --- Skills -----------------------------------------------------------------

  protected readonly skills = this.skillCommands.commands;

  // --- Tools ------------------------------------------------------------------

  /** Every tool the picker would show, by name. */
  protected readonly tools = computed<Tool[]>(() =>
    [...this.toolService.visibleTools()].sort((a, b) => a.displayName.localeCompare(b.displayName)),
  );
  protected readonly toolsLocked = this.toolService.agentLocked;

  // --- Connectors -------------------------------------------------------------

  protected readonly connectors = computed<UserConnector[]>(
    () => this.connectorsService.connectorsResource.value() ?? [],
  );

  /**
   * What a connector row is in the middle of, keyed by provider. `initiating`
   * is the consent request in flight, `awaiting` the popup open, `disconnecting`
   * the delete in flight. Absent means idle: the row shows its cached state.
   */
  protected readonly connectorPhases = signal<Record<string, ConnectorPhase>>({});

  /**
   * The menu opens upward from the composer's leading edge: the composer sits
   * at the bottom of the viewport, and this control at its left end.
   */
  protected readonly menuPositions: ConnectedPosition[] = [
    { originX: 'start', originY: 'top', overlayX: 'start', overlayY: 'bottom', offsetY: -8 },
    { originX: 'start', originY: 'bottom', overlayX: 'start', overlayY: 'top', offsetY: 8 },
  ];

  /** Submenus fly out to the right of their row, flipping left when cramped. */
  protected readonly submenuPositions: ConnectedPosition[] = [
    { originX: 'end', originY: 'bottom', overlayX: 'start', overlayY: 'bottom', offsetX: 4 },
    { originX: 'start', originY: 'bottom', overlayX: 'end', overlayY: 'bottom', offsetX: -4 },
    { originX: 'end', originY: 'top', overlayX: 'start', overlayY: 'top', offsetX: 4 },
    { originX: 'start', originY: 'top', overlayX: 'end', overlayY: 'top', offsetX: -4 },
  ];

  constructor() {
    // Connection badges: probe once the connector list is known and the menu
    // is open. `ensure` skips providers it already knows, so reopening the
    // menu costs nothing.
    effect(() => {
      if (!this.open()) return;
      const ids = this.connectors().map((c) => c.providerId);
      if (ids.length > 0) void this.connectorStatus.ensure(ids);
    });

    // The consent popup reports back through the shared consent service (the
    // `/oauth-complete` landing page postMessages it), the same signal the
    // Connectors page and the file-source browser listen to. Only a provider
    // this menu switched on is ours to acknowledge; `ConnectorStatusService`
    // flips the badge on success by itself.
    effect(() => {
      const completion = this.consentService.completion();
      if (!completion?.providerId) return;
      if (this.connectorPhases()[completion.providerId] !== 'awaiting') return;
      this.consentService.acknowledgeCompletion();
      this.clearPhase(completion.providerId);
      if (completion.status !== 'success') {
        this.toast.error('Could not connect', completion.error ?? 'The sign-in did not complete.');
      }
    });

    // A popup closed without finishing drops out of `inFlightProviders`; the
    // row goes back to its switch rather than staying "Awaiting consent".
    effect(() => {
      const inFlight = this.consentService.inFlightProviders();
      for (const [id, phase] of Object.entries(this.connectorPhases())) {
        if (phase === 'awaiting' && !inFlight.has(id)) this.clearPhase(id);
      }
    });
  }

  protected onOpened(): void {
    this.open.set(true);
    void this.loadFileSources();
    void this.skillCommands.load();
    void this.toolService.ensureLoaded();
    this.connectorsService.ensureLoaded();
  }

  protected onClosed(): void {
    this.open.set(false);
  }

  // --- Files ------------------------------------------------------------------

  protected pickDevice(): void {
    if (this.remainingFiles() <= 0) {
      this.warnFileLimit();
      return;
    }
    this.pickFromDevice.emit();
  }

  /**
   * Open the file-source browser on one connector. The browser handles
   * consent itself when the connector is not connected yet, and comes back
   * with the files to attach; the composer imports them.
   */
  protected async openSource(source: FileSourceConnector): Promise<void> {
    if (this.remainingFiles() <= 0) {
      this.warnFileLimit();
      return;
    }
    const ref = this.dialog.open<FileSourceBrowserDialogResult, FileSourceBrowserDialogData>(
      FileSourceBrowserDialogComponent,
      { data: { connector: source, mode: 'attach', maxFiles: this.remainingFiles() } },
    );
    const result = await firstValueFrom(ref.closed);
    // Consent may have landed while the dialog was open; the row's state is
    // only decoration, but a stale "Not connected" would send the person
    // through the browser's connect prompt a second time.
    void this.loadFileSources(true);
    if (result && 'files' in result && result.files.length > 0) {
      this.sourceFilesPicked.emit(result);
    }
  }

  private async loadFileSources(refresh = false): Promise<void> {
    if (this.fileSourcesLoaded && !refresh) return;
    try {
      this.fileSources.set(await this.fileSourceService.listFileSources());
      this.fileSourcesLoaded = true;
    } catch {
      // No row is the honest state when the catalog cannot be read: the
      // device picker still works, and the next open tries again.
      this.fileSources.set([]);
    }
  }

  private warnFileLimit(): void {
    this.toast.warning(
      'File limit',
      `Maximum ${MAX_FILES_PER_MESSAGE} files per message. Remove one to add another.`,
    );
  }

  // --- Skills -----------------------------------------------------------------

  protected pickSkill(command: SkillCommand): void {
    this.skillPicked.emit(command);
  }

  // --- Tools ------------------------------------------------------------------

  protected isToolOn(tool: Tool): boolean {
    return this.toolService.isToolShownEnabled(tool);
  }

  /**
   * Why a row cannot be flipped: an Agent dictates the set, an admin pinned
   * the tool, or it is retiring and off (it may still be turned off, never
   * on). Same rules as the Customize page, which `ToolService.toggleTool`
   * enforces again underneath.
   */
  protected toolDisabled(tool: Tool): boolean {
    if (this.toolsLocked()) return true;
    if (tool.alwaysOn) return true;
    return isRetiring(tool) && !tool.isEnabled;
  }

  protected toggleTool(tool: Tool): void {
    void this.toolService.toggleTool(tool.toolId).catch((err: unknown) => {
      const message = err instanceof Error ? err.message : 'Please try again.';
      this.toast.error('Could not update the tool', message);
    });
  }

  // --- Connectors -------------------------------------------------------------

  protected connectorStateLabel(connector: UserConnector): string {
    switch (this.connectorPhases()[connector.providerId]) {
      case 'initiating':
        return 'Starting…';
      case 'awaiting':
        return 'Awaiting consent';
      case 'disconnecting':
        return 'Disconnecting…';
      default:
        break;
    }
    switch (this.connectorStatus.stateFor(connector.providerId)) {
      case 'connected':
        return 'Connected';
      case 'disconnected':
        return 'Not connected';
      default:
        return '';
    }
  }

  protected isConnected(connector: UserConnector): boolean {
    return this.connectorStatus.stateFor(connector.providerId) === 'connected';
  }

  /** A row mid-flight is not a switch until the flight lands. */
  protected connectorBusy(connector: UserConnector): boolean {
    return connector.providerId in this.connectorPhases();
  }

  /**
   * The switch: on runs the consent popup, off disconnects.
   *
   * No confirmation on the way off, unlike the Connectors page: a switch is
   * its own confirmation, and a disconnect is reversible by switching it back
   * on. The page's longer note about revoking the app at the provider still
   * applies and lives there.
   */
  protected toggleConnector(connector: UserConnector): void {
    if (this.connectorBusy(connector)) return;
    if (this.isConnected(connector)) {
      void this.disconnect(connector);
    } else {
      void this.connect(connector);
    }
  }

  private async connect(connector: UserConnector): Promise<void> {
    const id = connector.providerId;
    this.setPhase(id, 'initiating');
    try {
      const result = await this.connectorsService.initiateConsent(id);
      if (result.connected) {
        this.clearPhase(id);
        this.connectorStatus.markConnected(id);
        return;
      }
      if (!result.authorizationUrl) {
        this.clearPhase(id);
        this.toast.error('Could not connect', 'Unexpected response from the server.');
        return;
      }
      this.consentService.requestConsent(id, result.authorizationUrl);
      void this.consentService.openConsentPopup(id);
      this.setPhase(id, 'awaiting');
    } catch (err: unknown) {
      this.clearPhase(id);
      const detail = (err as { error?: { detail?: string } })?.error?.detail;
      this.toast.error('Could not connect', detail ?? 'Could not start the sign-in.');
    }
  }

  private async disconnect(connector: UserConnector): Promise<void> {
    const id = connector.providerId;
    this.setPhase(id, 'disconnecting');
    try {
      await this.connectorsService.disconnect(id);
      this.connectorStatus.markDisconnected(id);
      this.toast.success(`${connector.displayName} disconnected.`);
    } catch (err: unknown) {
      const detail = (err as { error?: { detail?: string } })?.error?.detail;
      this.toast.error('Could not disconnect', detail ?? 'Please try again.');
    } finally {
      this.clearPhase(id);
    }
  }

  private setPhase(id: string, phase: ConnectorPhase): void {
    this.connectorPhases.update((phases) => ({ ...phases, [id]: phase }));
  }

  private clearPhase(id: string): void {
    this.connectorPhases.update((phases) => {
      if (!(id in phases)) return phases;
      const next = { ...phases };
      delete next[id];
      return next;
    });
  }

  // --- Navigation -------------------------------------------------------------

  protected go(path: string): void {
    void this.router.navigate([path]);
  }
}
