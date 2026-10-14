import {
  ChangeDetectionStrategy,
  Component,
  Injector,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { CdkConnectedOverlay, CdkOverlayOrigin, ConnectedPosition } from '@angular/cdk/overlay';
import { ActivatedRoute, Router } from '@angular/router';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroCheck, heroChevronDown, heroFolder, heroFolderPlus, heroPlus, heroRectangleStack, heroXMark } from '@ng-icons/heroicons/outline';
import { firstValueFrom } from 'rxjs';
import {
  AttachSelection,
  FileSourceBrowserDialogComponent,
  FileSourceBrowserDialogData,
  FileSourceBrowserDialogResult,
  FolderAttachSelection,
  isFolderAttachSelection,
} from '../../../assistants/components/file-source-browser-dialog.component';
import { FileSourceConnector } from '../../../assistants/models/file-source.model';
import { FileSourceService } from '../../../assistants/services/file-source.service';
import {
  CreateProjectDialogComponent,
  CreateProjectDialogResult,
} from '../../../projects/components/create-project-dialog.component';
import { Project } from '../../../projects/models/project.model';
import { ProjectsService } from '../../../projects/services/projects.service';
import {
  BindingQueryParams,
  ConversationBindingService,
} from '../../services/session/conversation-binding.service';
import { FEATURES } from '../../../services/features';
import { FolderAttachmentService, RecentFolder } from '../../../services/folder-attachments';

/** One row of the listbox. Projects and recent folders filter; the actions always show. */
type ScopeOption =
  | { kind: 'clear'; key: string; label: string }
  | { kind: 'project'; key: string; label: string; detail: string | null; project: Project }
  | { kind: 'recent-folder'; key: string; label: string; detail: string | null; folder: RecentFolder; source: FileSourceConnector }
  | { kind: 'new-project'; key: string; label: string }
  | { kind: 'add-folder'; key: string; label: string; source: FileSourceConnector };

let nextPickerId = 0;

/** The listbox's sections, in order; a rule is drawn where one gives way to the next. */
function sectionOf(option: ScopeOption): 'projects' | 'folders' | 'actions' {
  switch (option.kind) {
    case 'clear':
    case 'project':
      return 'projects';
    case 'recent-folder':
      return 'folders';
    default:
      return 'actions';
  }
}

/** The key the composer and the recent list share for one source folder. */
export function folderKey(connectorId: string, providerFolderId: string): string {
  return `${connectorId}:${providerFolderId}`;
}

/**
 * "Project or folder" beneath a new conversation's composer
 * (docs/specs/composer-project-folder-picker.md).
 *
 * Scopes the work before the first message: bind the conversation to a Shared
 * Project, or attach a Drive folder. The two are different kinds of scope (spec
 * §2): a project binds the whole conversation through its harness Agent, named
 * on the URL by the project (`?project=`, see `ConversationBindingService`), while a folder is an
 * attachment on this message, handed to the composer exactly as the `+` menu's
 * "Attach this folder" is.
 *
 * Like `ComposerAddMenuComponent`, it owns no attachment state: a folder (or
 * files, if the person ticks some in the browser instead) goes back to the
 * composer through `sourceFolderPicked` / `sourceFilesPicked`.
 *
 * The popover is the APG combobox the memory link picker uses: the filter keeps
 * focus, ArrowUp and ArrowDown move the active option (`aria-activedescendant`),
 * Enter picks it, Escape closes and returns focus to the trigger.
 */
@Component({
  selector: 'app-composer-scope-picker',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CdkConnectedOverlay, CdkOverlayOrigin, NgIcon],
  providers: [provideIcons({ heroCheck, heroChevronDown, heroFolder, heroFolderPlus, heroPlus, heroRectangleStack, heroXMark })],
  host: { class: 'contents' },
  template: `
    @if (visible()) {
      <button
        type="button"
        cdkOverlayOrigin
        #origin="cdkOverlayOrigin"
        [id]="triggerId"
        (click)="toggle()"
        (pointerenter)="prefetch()"
        (focus)="prefetch()"
        [disabled]="disabled()"
        aria-haspopup="dialog"
        [attr.aria-expanded]="open()"
        [attr.aria-label]="selectedProject() ? 'Project: ' + selectedProject()!.name + '. Change project or add a folder' : null"
        class="relative -left-2 inline-flex min-w-0 max-w-full items-center gap-1 rounded-lg px-2 py-1 text-xs/5 text-gray-600 hover:bg-gray-100 hover:text-gray-900 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50 dark:text-gray-300 dark:hover:bg-white/10 dark:hover:text-white"
        data-testid="composer-scope-trigger"
      >
        @if (selectedProject(); as project) {
          <ng-icon name="heroRectangleStack" class="size-3.5 shrink-0" aria-hidden="true" />
          <span class="truncate">{{ project.name }}</span>
        } @else {
          <span class="truncate">{{ triggerLabel() }}</span>
        }
        <ng-icon name="heroChevronDown" class="size-3 shrink-0 transition-transform" [class.rotate-180]="open()" aria-hidden="true" />
      </button>

      <ng-template
        cdkConnectedOverlay
        [cdkConnectedOverlayOrigin]="origin"
        [cdkConnectedOverlayOpen]="open()"
        [cdkConnectedOverlayPositions]="positions"
        [cdkConnectedOverlayHasBackdrop]="true"
        cdkConnectedOverlayBackdropClass="cdk-overlay-transparent-backdrop"
        (backdropClick)="close(false)"
        (detach)="close(false)"
        (attach)="onAttached()"
        (overlayKeydown)="onOverlayKeydown($event)"
      >
        <div
          role="dialog"
          [attr.aria-label]="triggerLabel()"
          class="w-[min(26rem,calc(100vw-2rem))] overflow-hidden rounded-2xl border border-gray-200 bg-white shadow-lg dark:border-gray-700 dark:bg-gray-800"
          data-testid="composer-scope-panel"
        >
          <div class="border-b border-gray-200 p-2 dark:border-gray-700">
            <label [for]="filterId" class="sr-only">{{ filterLabel() }}</label>
            <input
              [id]="filterId"
              type="text"
              role="combobox"
              autocomplete="off"
              aria-autocomplete="list"
              aria-expanded="true"
              [attr.aria-controls]="listboxId"
              [attr.aria-activedescendant]="options().length ? optionId(active()) : null"
              [value]="query()"
              (input)="onInput($any($event.target).value)"
              (keydown)="onKeydown($event)"
              [placeholder]="filterLabel()"
              class="block w-full rounded-xl border-0 bg-transparent px-2 py-1.5 text-sm/6 text-gray-900 placeholder:text-gray-500 focus:outline-none focus-visible:ring-2 focus-visible:ring-primary-500 dark:text-white dark:placeholder:text-gray-400"
            />
          </div>
          <p class="sr-only" aria-live="polite">{{ matchAnnouncement() }}</p>
          <ul [id]="listboxId" role="listbox" [attr.aria-label]="triggerLabel()" class="max-h-80 overflow-y-auto p-1.5">
            @if (noMatch()) {
              <li role="presentation" class="px-2.5 py-2 text-sm/6 text-gray-600 dark:text-gray-400" data-testid="composer-scope-no-match">{{ noMatchLabel() }} “{{ query().trim() }}”.</li>
            }
            @for (option of options(); track option.key; let i = $index) {
              @if (separatorBefore(i)) {
                <li role="presentation" class="mx-2 my-1 border-t border-gray-200 dark:border-gray-700"></li>
              }
              <li
                [id]="optionId(i)"
                role="option"
                [attr.aria-selected]="option.kind === 'project' && option.project.projectId === selectedProject()?.projectId"
                (click)="pick(option)"
                (mousemove)="active.set(i)"
                class="flex cursor-pointer items-center gap-2.5 rounded-xl px-2.5 py-2 text-sm/6 text-gray-800 dark:text-gray-100"
                [class]="i === active() ? 'bg-gray-100 dark:bg-white/10' : ''"
                [attr.data-testid]="'composer-scope-option-' + option.kind"
              >
                @switch (option.kind) {
                  @case ('clear') {
                    <ng-icon name="heroXMark" class="size-4 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
                  }
                  @case ('project') {
                    <ng-icon name="heroRectangleStack" class="size-4 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
                  }
                  @case ('recent-folder') {
                    <ng-icon name="heroFolder" class="size-4 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
                  }
                  @case ('new-project') {
                    <ng-icon name="heroPlus" class="size-4 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
                  }
                  @case ('add-folder') {
                    <ng-icon name="heroFolderPlus" class="size-4 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
                  }
                }
                <span class="min-w-0 flex-1">
                  <span class="block truncate">{{ option.label }}</span>
                  @if ((option.kind === 'project' || option.kind === 'recent-folder') && option.detail) {
                    <span class="block truncate text-xs/5 text-gray-600 dark:text-gray-400">{{ option.detail }}</span>
                  }
                </span>
                @if (option.kind === 'project' && option.project.projectId === selectedProject()?.projectId) {
                  <ng-icon name="heroCheck" class="size-4 shrink-0 text-primary-600 dark:text-primary-400" aria-hidden="true" />
                }
              </li>
            }
          </ul>
        </div>
      </ng-template>
    }
  `,
})
export class ComposerScopePickerComponent {
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  private readonly dialog = inject(Dialog);
  private readonly injector = inject(Injector);
  private readonly features = inject(FEATURES);
  private readonly projectsService = inject(ProjectsService);
  private readonly binding = inject(ConversationBindingService);
  private readonly fileSourceService = inject(FileSourceService);
  private readonly folderAttachments = inject(FolderAttachmentService);

  /** How many more files the message can take, handed to the browser for its own file picks. */
  readonly remainingFiles = input<number>(0);
  /** How many more folders the message can take; zero hides the folder rows. */
  readonly remainingFolders = input<number>(0);
  /** The whole trigger, e.g. while dictating. */
  readonly disabled = input<boolean>(false);
  /**
   * Folders already on this message, as `folderKey`s, so a recent folder is
   * not offered a second time beside its own card.
   */
  readonly attachedFolderKeys = input<readonly string[]>([]);

  /** A folder chosen in the browser ("Attach this folder"), for the composer to attach. */
  readonly sourceFolderPicked = output<FolderAttachSelection>();
  /** Files ticked in the same browser instead, attached as the `+` menu's would be. */
  readonly sourceFilesPicked = output<AttachSelection>();

  private readonly uid = nextPickerId++;
  protected readonly triggerId = `composer-scope-trigger-${this.uid}`;
  protected readonly filterId = `composer-scope-filter-${this.uid}`;
  protected readonly listboxId = `composer-scope-options-${this.uid}`;

  protected readonly open = signal(false);
  protected readonly query = signal('');
  protected readonly active = signal(0);
  protected readonly fileSources = signal<FileSourceConnector[]>([]);
  private fileSourcesLoaded = false;
  protected readonly recentFolders = signal<RecentFolder[]>([]);
  private recentLoaded = false;

  /** The popover opens downward from the line beneath the composer, flipping up when cramped. */
  protected readonly positions: ConnectedPosition[] = [
    { originX: 'start', originY: 'bottom', overlayX: 'start', overlayY: 'top', offsetY: 6 },
    { originX: 'start', originY: 'top', overlayX: 'start', overlayY: 'bottom', offsetY: -6 },
  ];

  /** Projects are on in this build and the backend has not said otherwise. */
  private readonly projectsOn = computed(
    () => this.features.projects && this.projectsService.available$() !== false,
  );
  private readonly foldersOn = computed(() => this.remainingFolders() > 0);

  protected readonly visible = computed(() => this.projectsOn() || this.foldersOn());

  protected readonly triggerLabel = computed(() => {
    if (this.projectsOn() && this.foldersOn()) return 'Project or folder';
    return this.projectsOn() ? 'Project' : 'Folder';
  });

  protected readonly filterLabel = computed(() => {
    if (this.projectsOn() && this.foldersOn()) return 'Search projects and folders';
    return this.projectsOn() ? 'Search projects' : 'Search folders';
  });

  /**
   * The project the new conversation is bound to: the URL's `?project=` (or an
   * older link's harness `assistantId`), which the session page treats as the
   * single source of truth, as the model picker does. The name shown is the
   * project's, never the harness Agent's, which goes stale when a project is
   * renamed.
   */
  protected readonly selectedProject = computed<Project | null>(() =>
    this.projectsOn() ? this.binding.boundProject() : null,
  );

  /** Active projects only: a new conversation in an archived one is refused. */
  private readonly startableProjects = computed(() =>
    this.projectsService.projects$().filter(p => p.status === 'active'),
  );

  protected readonly options = computed<ScopeOption[]>(() => {
    const q = this.query().trim().toLowerCase();
    const rows: ScopeOption[] = [];
    if (this.projectsOn()) {
      if (this.selectedProject() && !q) rows.push({ kind: 'clear', key: 'clear', label: 'No project' });
      for (const project of this.startableProjects()) {
        if (q && !project.name.toLowerCase().includes(q)) continue;
        rows.push({
          kind: 'project',
          key: `project:${project.projectId}`,
          label: project.name,
          detail: project.role === 'owner' ? null : 'Shared with you',
          project,
        });
      }
    }
    if (this.foldersOn()) {
      for (const row of this.offeredRecentFolders()) {
        const haystack = `${row.folder.folderName} ${row.folder.folderPath ?? ''}`.toLowerCase();
        if (q && !haystack.includes(q)) continue;
        rows.push({
          kind: 'recent-folder',
          key: `recent:${folderKey(row.folder.connectorId, row.folder.providerFolderId)}`,
          label: row.folder.folderName,
          detail: row.folder.folderPath ?? row.folder.connectorName ?? row.source.displayName,
          folder: row.folder,
          source: row.source,
        });
      }
    }
    if (this.projectsOn()) rows.push({ kind: 'new-project', key: 'new-project', label: 'New project' });
    if (this.foldersOn()) {
      for (const source of this.fileSources()) {
        rows.push({
          kind: 'add-folder',
          key: `add-folder:${source.providerId}`,
          label: `Add folder from ${source.displayName}`,
          source,
        });
      }
    }
    return rows;
  });

  /**
   * Recent folders the composer can take now: through a file source the user
   * can still see (the list `GET /file-sources` gives, which also supplies the
   * connector the attach needs), and not already on this message.
   */
  private readonly offeredRecentFolders = computed(() => {
    const sources = new Map(this.fileSources().map(source => [source.providerId, source]));
    const attached = new Set(this.attachedFolderKeys());
    const offered: { folder: RecentFolder; source: FileSourceConnector }[] = [];
    for (const folder of this.recentFolders()) {
      const source = sources.get(folder.connectorId);
      if (!source || attached.has(folderKey(folder.connectorId, folder.providerFolderId))) continue;
      offered.push({ folder, source });
    }
    return offered;
  });

  /** How many filterable rows (projects, recent folders) the filter left. */
  private readonly matchCount = computed(
    () => this.options().filter(o => o.kind === 'project' || o.kind === 'recent-folder').length,
  );

  protected readonly noMatch = computed(
    () =>
      (this.projectsOn() || this.offeredRecentFolders().length > 0) &&
      this.query().trim() !== '' &&
      this.matchCount() === 0,
  );

  protected readonly noMatchLabel = computed(() => {
    const folders = this.offeredRecentFolders().length > 0;
    if (this.projectsOn() && folders) return 'No project or folder matches';
    return this.projectsOn() ? 'No project matches' : 'No folder matches';
  });

  protected readonly matchAnnouncement = computed(() => {
    if (!this.query().trim()) return '';
    const count = this.matchCount();
    return `${count} ${count === 1 ? 'match' : 'matches'}`;
  });

  constructor() {
    // A `?project=` resolves on its own (ConversationBindingService fetches
    // it). An older link naming an Agent can only be told to be a project's
    // harness by the list, which the sidebar loads only when some
    // conversation is in a project, so load it here when it has never been
    // loaded; otherwise wait for the popover.
    effect(() => {
      if (this.binding.queryAssistantId() && this.projectsOn()) this.ensureProjects();
    });
  }

  /** Hover or focus on the trigger: start the folder lists so their rows are there on open. */
  protected prefetch(): void {
    void this.loadFileSources();
    if (!this.recentLoaded) void this.loadRecentFolders();
  }

  protected toggle(): void {
    if (this.open()) {
      this.close(true);
      return;
    }
    this.query.set('');
    this.active.set(0);
    this.open.set(true);
    this.ensureProjects();
    void this.loadFileSources();
    // Every open, not once: a folder attached a moment ago belongs at the top.
    void this.loadRecentFolders();
  }

  /** Close the popover; `refocus` returns focus to the trigger (Escape, a pick). */
  protected close(refocus: boolean): void {
    if (!this.open()) return;
    this.open.set(false);
    if (refocus) document.getElementById(this.triggerId)?.focus();
  }

  protected onAttached(): void {
    afterNextRender(() => document.getElementById(this.filterId)?.focus(), { injector: this.injector });
  }

  protected optionId(i: number): string {
    return `composer-scope-option-${this.uid}-${i}`;
  }

  /** A rule between the projects and the actions beneath them (New project, Add folder). */
  protected separatorBefore(i: number): boolean {
    const rows = this.options();
    return i > 0 && sectionOf(rows[i]) !== sectionOf(rows[i - 1]);
  }

  /** Escape from anywhere in the panel, not only the filter. */
  protected onOverlayKeydown(event: KeyboardEvent): void {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    this.close(true);
  }

  protected onInput(value: string): void {
    this.query.set(value);
    this.active.set(0);
  }

  protected onKeydown(event: KeyboardEvent): void {
    const count = this.options().length;
    switch (event.key) {
      case 'ArrowDown':
        event.preventDefault();
        if (count) this.active.update(i => (i + 1) % count);
        break;
      case 'ArrowUp':
        event.preventDefault();
        if (count) this.active.update(i => (i - 1 + count) % count);
        break;
      case 'Home':
        event.preventDefault();
        this.active.set(0);
        break;
      case 'End':
        event.preventDefault();
        if (count) this.active.set(count - 1);
        break;
      case 'Enter': {
        event.preventDefault();
        const option = this.options()[this.active()];
        if (option) void this.pick(option);
        return;
      }
      case 'Escape':
        event.preventDefault();
        event.stopPropagation();
        this.close(true);
        return;
      case 'Tab':
        // The panel lives at the end of the document; Tab from it would land
        // nowhere useful, so it closes back onto the trigger.
        event.preventDefault();
        this.close(true);
        return;
      default:
        return;
    }
    document.getElementById(this.optionId(this.active()))?.scrollIntoView?.({ block: 'nearest' });
  }

  protected async pick(option: ScopeOption): Promise<void> {
    this.close(true);
    switch (option.kind) {
      case 'clear':
        this.navigateBinding({ project: null, assistantId: null });
        return;
      case 'project':
        this.bindProject(option.project);
        return;
      case 'new-project':
        await this.createProject();
        return;
      case 'recent-folder':
        this.sourceFolderPicked.emit({
          connector: option.source,
          folder: {
            folderId: option.folder.providerFolderId,
            folderName: option.folder.folderName,
            folderPath: option.folder.folderPath ?? option.folder.folderName,
          },
        });
        return;
      case 'add-folder':
        await this.addFolder(option.source);
        return;
    }
  }

  /**
   * Bind the new conversation to a project: `?project=` on the URL, which the
   * session page resolves to the project's harness Agent and sends as the
   * turn's Agent. Replaces any Agent already chosen. The page stays where it
   * is, so the draft and anything staged on the composer stay too.
   */
  private bindProject(project: Project): void {
    if (project.projectId === this.selectedProject()?.projectId) return;
    this.navigateBinding(this.binding.queryParamsFor(project.harnessAgentId, project.projectId));
  }

  /**
   * Binding or clearing swaps the empty state's layout (the greeting for the
   * launch card), which re-creates the composer, and the new composer focuses
   * its message box on first render. That is where the person goes next, and
   * the same landing an Agent launcher gives, so nothing here moves focus.
   */
  private navigateBinding(queryParams: BindingQueryParams): void {
    void this.router.navigate([], {
      relativeTo: this.route,
      queryParams,
      queryParamsHandling: 'merge',
    });
  }

  private async createProject(): Promise<void> {
    const ref = this.dialog.open<CreateProjectDialogResult>(CreateProjectDialogComponent);
    const project = await firstValueFrom(ref.closed);
    if (project) this.bindProject(project);
  }

  /**
   * The same browser the `+` menu opens, in its attach mode with "Attach this
   * folder" offered. Its result goes back to the composer unchanged.
   */
  private async addFolder(source: FileSourceConnector): Promise<void> {
    const ref = this.dialog.open<FileSourceBrowserDialogResult, FileSourceBrowserDialogData>(
      FileSourceBrowserDialogComponent,
      {
        data: {
          connector: source,
          mode: 'attach',
          maxFiles: this.remainingFiles(),
          allowFolder: this.remainingFolders() > 0,
        },
      },
    );
    const result = await firstValueFrom(ref.closed);
    void this.loadFileSources(true);
    if (isFolderAttachSelection(result)) {
      this.sourceFolderPicked.emit(result);
    } else if (result && 'files' in result && result.files.length > 0) {
      this.sourceFilesPicked.emit(result);
    }
  }

  private ensureProjects(): void {
    if (!this.projectsOn()) return;
    if (this.projectsService.available$() !== null || this.projectsService.loading$()) return;
    void this.projectsService.load();
  }

  /** One request at a time, like the file sources: hover and open both ask. */
  private recentInFlight: Promise<void> | null = null;

  private loadRecentFolders(): Promise<void> {
    if (!this.foldersOn()) return Promise.resolve();
    if (this.recentInFlight) return this.recentInFlight;
    this.recentInFlight = (async () => {
      try {
        this.recentFolders.set(await this.folderAttachments.listRecent());
        this.recentLoaded = true;
      } catch {
        // Recent folders are a shortcut; without them "Add folder" still works.
        this.recentFolders.set([]);
      } finally {
        this.recentInFlight = null;
      }
    })();
    return this.recentInFlight;
  }

  /** One request at a time: hover, focus and open all ask for the same list. */
  private fileSourcesInFlight: Promise<void> | null = null;

  private loadFileSources(refresh = false): Promise<void> {
    if (!this.foldersOn()) return Promise.resolve();
    if (this.fileSourcesLoaded && !refresh) return Promise.resolve();
    if (this.fileSourcesInFlight) return this.fileSourcesInFlight;
    this.fileSourcesInFlight = (async () => {
      try {
        this.fileSources.set(await this.fileSourceService.listFileSources());
        this.fileSourcesLoaded = true;
      } catch {
        // No folder row is the honest state when the catalog cannot be read;
        // the next open tries again.
        this.fileSources.set([]);
      } finally {
        this.fileSourcesInFlight = null;
      }
    })();
    return this.fileSourcesInFlight;
  }
}
