import { signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { ActivatedRoute, Router, convertToParamMap } from '@angular/router';
import { BehaviorSubject, of } from 'rxjs';
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  AttachSelection,
  FolderAttachSelection,
} from '../../../assistants/components/file-source-browser-dialog.component';
import { FileSourceConnector } from '../../../assistants/models/file-source.model';
import { FileSourceService } from '../../../assistants/services/file-source.service';
import { CreateProjectDialogComponent } from '../../../projects/components/create-project-dialog.component';
import { Project } from '../../../projects/models/project.model';
import { ProjectsService } from '../../../projects/services/projects.service';
import { FEATURES } from '../../../services/features';
import { FolderAttachmentService, RecentFolder } from '../../../services/folder-attachments';
import { ComposerScopePickerComponent } from './composer-scope-picker.component';

const DRIVE: FileSourceConnector = {
  providerId: 'google',
  displayName: 'Google Drive',
  iconName: 'heroCloud',
  connected: true,
};

function project(over: Partial<Project>): Project {
  return {
    projectId: 'p1',
    name: 'Course redesign',
    description: '',
    ownerEmail: 'owner@example.edu',
    ownerName: null,
    role: 'owner',
    status: 'active',
    editorsManageMembers: false,
    memberCount: 0,
    harnessAgentId: 'harness-1',
    createdAt: '2026-10-01T00:00:00Z',
    updatedAt: '2026-10-01T00:00:00Z',
    ...over,
  };
}

function recent(over: Partial<RecentFolder>): RecentFolder {
  return {
    connectorId: 'google',
    connectorName: 'Google Drive',
    providerFolderId: 'drive-a',
    folderName: 'Syllabi',
    folderPath: 'My Drive/Syllabi',
    lastAttachedAt: '2026-10-14T00:00:00Z',
    ...over,
  };
}

const PROJECTS: Project[] = [
  project({}),
  project({ projectId: 'p2', name: 'Grant proposal', harnessAgentId: 'harness-2', role: 'editor' }),
  project({ projectId: 'p3', name: 'Old archive', harnessAgentId: 'harness-3', status: 'archived' }),
];

describe('ComposerScopePickerComponent', () => {
  let fixture: ComponentFixture<ComposerScopePickerComponent>;
  let component: ComposerScopePickerComponent;
  let navigate: ReturnType<typeof vi.fn>;
  let dialogOpen: ReturnType<typeof vi.fn>;
  let dialogResult: unknown;
  let load: ReturnType<typeof vi.fn>;
  let listFileSources: ReturnType<typeof vi.fn>;
  let queryParams: BehaviorSubject<ReturnType<typeof convertToParamMap>>;
  let projects: ReturnType<typeof signal<Project[]>>;
  let available: ReturnType<typeof signal<boolean | null>>;
  let listRecent: ReturnType<typeof vi.fn>;

  interface BuildOptions {
    projectsFlag?: boolean;
    remainingFolders?: number;
    /** An older link's harness Agent on the URL. */
    assistantId?: string | null;
    /** The URL's `?project=`. */
    projectId?: string | null;
    available?: boolean | null;
    list?: Project[];
    recentFolders?: RecentFolder[] | Error;
    attachedFolderKeys?: string[];
  }

  async function build(opts: BuildOptions = {}): Promise<void> {
    const {
      projectsFlag = true,
      remainingFolders = 2,
      assistantId = null,
      projectId = null,
      list = PROJECTS,
    } = opts;
    navigate = vi.fn(async () => true);
    dialogResult = undefined;
    dialogOpen = vi.fn(() => ({ closed: of(dialogResult) }));
    projects = signal<Project[]>(list);
    available = signal<boolean | null>(opts.available === undefined ? true : opts.available);
    load = vi.fn(async () => {
      projects.set(list);
      available.set(true);
    });
    listFileSources = vi.fn(async () => [DRIVE]);
    const recentAnswer = opts.recentFolders ?? [];
    listRecent = vi.fn(async () => {
      if (recentAnswer instanceof Error) throw recentAnswer;
      return recentAnswer;
    });
    queryParams = new BehaviorSubject(
      convertToParamMap({
        ...(assistantId ? { assistantId } : {}),
        ...(projectId ? { project: projectId } : {}),
      }),
    );

    await TestBed.configureTestingModule({
      imports: [ComposerScopePickerComponent],
      providers: [
        { provide: FEATURES, useValue: { projects: projectsFlag } },
        { provide: Router, useValue: { navigate } },
        { provide: ActivatedRoute, useValue: { queryParamMap: queryParams.asObservable() } },
        { provide: Dialog, useValue: { open: dialogOpen } },
        {
          provide: ProjectsService,
          useValue: {
            projects$: projects.asReadonly(),
            available$: available.asReadonly(),
            loading$: signal(false).asReadonly(),
            load,
          },
        },
        { provide: FileSourceService, useValue: { listFileSources } },
        { provide: FolderAttachmentService, useValue: { listRecent } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(ComposerScopePickerComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('remainingFiles', 5);
    fixture.componentRef.setInput('remainingFolders', remainingFolders);
    fixture.componentRef.setInput('attachedFolderKeys', opts.attachedFolderKeys ?? []);
    fixture.detectChanges();
  }

  afterEach(() => {
    TestBed.resetTestingModule();
    document.querySelectorAll('.cdk-overlay-container').forEach(el => (el.innerHTML = ''));
  });

  const trigger = (): HTMLButtonElement | null =>
    fixture.nativeElement.querySelector('[data-testid="composer-scope-trigger"]');
  const filter = (): HTMLInputElement =>
    document.querySelector('[data-testid="composer-scope-panel"] input[role="combobox"]') as HTMLInputElement;
  const optionEls = (): HTMLElement[] =>
    Array.from(document.querySelectorAll('[data-testid="composer-scope-panel"] [role="option"]'));
  const optionLabels = (): string[] => optionEls().map(el => el.textContent?.trim() ?? '');

  async function openPanel(): Promise<void> {
    trigger()!.click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  function type(text: string): void {
    const input = filter();
    input.value = text;
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  }

  function key(name: string): void {
    filter().dispatchEvent(new KeyboardEvent('keydown', { key: name, bubbles: true }));
    fixture.detectChanges();
  }

  describe('the trigger', () => {
    it('is absent when neither projects nor folders have anything to offer', async () => {
      await build({ projectsFlag: false, remainingFolders: 0 });
      expect(trigger()).toBeNull();
    });

    it('is absent while the backend says projects are off and the message has no folder room', async () => {
      await build({ available: false, remainingFolders: 0 });
      expect(trigger()).toBeNull();
    });

    it('names what it offers', async () => {
      await build();
      expect(trigger()!.textContent).toContain('Project or folder');
      TestBed.resetTestingModule();
      await build({ remainingFolders: 0 });
      expect(trigger()!.textContent).toContain('Project');
      expect(trigger()!.textContent).not.toContain('folder');
      TestBed.resetTestingModule();
      await build({ projectsFlag: false });
      expect(trigger()!.textContent).toContain('Folder');
    });

    it("shows the chosen project's name, from the project list", async () => {
      await build({ projectId: 'p2' });
      expect(trigger()!.textContent).toContain('Grant proposal');
      expect(trigger()!.getAttribute('aria-label')).toContain('Project: Grant proposal');
    });

    it('still recognises an older link that names the harness Agent', async () => {
      await build({ assistantId: 'harness-2' });
      expect(trigger()!.textContent).toContain('Grant proposal');
      expect(trigger()!.getAttribute('aria-label')).toContain('Project: Grant proposal');
    });

    it('loads the project list when a project is on the URL and the list was never loaded', async () => {
      await build({ assistantId: 'harness-1', available: null, list: PROJECTS });
      expect(load).toHaveBeenCalledTimes(1);
      fixture.detectChanges();
      expect(trigger()!.textContent).toContain('Course redesign');
    });

    it('does not load the list for a plain new conversation until it is opened', async () => {
      await build({ available: null });
      expect(load).not.toHaveBeenCalled();
      await openPanel();
      expect(load).toHaveBeenCalledTimes(1);
    });
  });

  describe('the popover', () => {
    it('lists active projects, then New project, then a folder row per file source', async () => {
      await build();
      await openPanel();
      await vi.waitFor(() => expect(listFileSources).toHaveBeenCalled());
      fixture.detectChanges();
      expect(optionLabels()).toEqual([
        'Course redesign',
        'Grant proposalShared with you',
        'New project',
        'Add folder from Google Drive',
      ]);
      expect(trigger()!.getAttribute('aria-expanded')).toBe('true');
    });

    it('focuses the filter, a combobox pointing at the active option', async () => {
      await build();
      await openPanel();
      await vi.waitFor(() => expect(document.activeElement).toBe(filter()));
      const active = filter().getAttribute('aria-activedescendant');
      expect(active).toBe(optionEls()[0].id);
    });

    it('filters projects by name and says when none match', async () => {
      await build();
      await openPanel();
      type('grant');
      expect(optionLabels()[0]).toContain('Grant proposal');
      expect(optionLabels().filter(l => l.startsWith('Course'))).toHaveLength(0);
      type('zzz');
      expect(document.querySelector('[data-testid="composer-scope-no-match"]')?.textContent).toContain('zzz');
      expect(optionLabels()).toContain('New project');
    });

    it('offers no folder row when the message has no room for a folder', async () => {
      await build({ remainingFolders: 0 });
      await openPanel();
      expect(listFileSources).not.toHaveBeenCalled();
      expect(optionLabels().some(l => l.startsWith('Add folder'))).toBe(false);
    });

    it('marks the chosen project selected and offers No project first', async () => {
      await build({ assistantId: 'harness-1' });
      await openPanel();
      expect(optionLabels()[0]).toBe('No project');
      const chosen = optionEls().find(el => el.textContent?.includes('Course redesign'));
      expect(chosen?.getAttribute('aria-selected')).toBe('true');
      expect(chosen?.querySelector('ng-icon[name="heroCheck"]')).not.toBeNull();
      const other = optionEls().find(el => el.textContent?.includes('Grant proposal'));
      expect(other?.querySelector('ng-icon[name="heroCheck"]')).toBeNull();
    });

    it('starts the file-source list on hover, once, so the folder row is there on open', async () => {
      await build();
      trigger()!.dispatchEvent(new Event('pointerenter'));
      trigger()!.dispatchEvent(new Event('focus'));
      await openPanel();
      await vi.waitFor(() => expect(optionLabels()).toContain('Add folder from Google Drive'));
      expect(listFileSources).toHaveBeenCalledTimes(1);
    });

    it('closes on Escape and returns focus to the trigger', async () => {
      await build();
      await openPanel();
      key('Escape');
      expect(document.querySelector('[data-testid="composer-scope-panel"]')).toBeNull();
      expect(document.activeElement).toBe(trigger());
    });
  });

  describe('picking', () => {
    it('binds a project by its id on the current URL, never its harness Agent', async () => {
      await build();
      await openPanel();
      key('ArrowDown');
      key('Enter');
      // `assistantId: null` too, so a merge drops an Agent chosen before the project.
      expect(navigate).toHaveBeenCalledWith([], expect.objectContaining({
        queryParams: { project: 'p2', assistantId: null },
        queryParamsHandling: 'merge',
      }));
    });

    it('clears the project with No project', async () => {
      await build({ projectId: 'p1' });
      await openPanel();
      optionEls()[0].click();
      expect(navigate).toHaveBeenCalledWith([], expect.objectContaining({ queryParams: { project: null, assistantId: null } }));
    });

    it('does nothing when the chosen project is picked again', async () => {
      await build({ assistantId: 'harness-1' });
      await openPanel();
      optionEls().find(el => el.textContent?.includes('Course redesign'))!.click();
      expect(navigate).not.toHaveBeenCalled();
    });

    it('creates a project and binds the conversation to it', async () => {
      await build();
      await openPanel();
      dialogResult = project({ projectId: 'p9', name: 'Fresh', harnessAgentId: 'harness-9' });
      optionEls().find(el => el.textContent?.includes('New project'))!.click();
      await vi.waitFor(() => expect(navigate).toHaveBeenCalled());
      expect(dialogOpen.mock.calls[0][0]).toBe(CreateProjectDialogComponent);
      expect(navigate).toHaveBeenCalledWith([], expect.objectContaining({ queryParams: { project: 'p9', assistantId: null } }));
    });

    it('binds nothing when the create dialog is cancelled', async () => {
      await build();
      await openPanel();
      optionEls().find(el => el.textContent?.includes('New project'))!.click();
      await fixture.whenStable();
      expect(navigate).not.toHaveBeenCalled();
    });

    it('hands a folder from the browser to the composer', async () => {
      await build();
      await openPanel();
      await vi.waitFor(() => expect(optionLabels()).toContain('Add folder from Google Drive'));
      const picked: FolderAttachSelection[] = [];
      component.sourceFolderPicked.subscribe(s => picked.push(s));
      const selection: FolderAttachSelection = {
        connector: DRIVE,
        folder: { folderId: 'f1', folderName: 'Syllabi', folderPath: 'My Drive / Syllabi' },
      };
      dialogResult = selection;
      optionEls().find(el => el.textContent?.includes('Add folder'))!.click();
      await vi.waitFor(() => expect(picked).toEqual([selection]));
      expect(dialogOpen.mock.calls[0][1].data).toEqual({
        connector: DRIVE,
        mode: 'attach',
        maxFiles: 5,
        allowFolder: true,
      });
    });

    it('hands files ticked in the same browser to the composer', async () => {
      await build();
      await openPanel();
      await vi.waitFor(() => expect(optionLabels()).toContain('Add folder from Google Drive'));
      const picked: AttachSelection[] = [];
      component.sourceFilesPicked.subscribe(s => picked.push(s));
      const selection: AttachSelection = { connector: DRIVE, files: [{ fileId: 'a', name: 'a.pdf' }] };
      dialogResult = selection;
      optionEls().find(el => el.textContent?.includes('Add folder'))!.click();
      await vi.waitFor(() => expect(picked).toEqual([selection]));
    });
  });

  describe('recent folders', () => {
    const SYLLABI = recent({});
    const CONTRACTS = recent({ providerFolderId: 'drive-b', folderName: 'Contracts', folderPath: null });
    const OTHER_SOURCE = recent({ connectorId: 'sharepoint', providerFolderId: 'sp-1', folderName: 'Hidden' });

    async function openWithRecent(opts: BuildOptions = {}): Promise<void> {
      await build({ recentFolders: [SYLLABI, CONTRACTS, OTHER_SOURCE], ...opts });
      await openPanel();
      await vi.waitFor(() => expect(listRecent).toHaveBeenCalled());
      await vi.waitFor(() => expect(optionLabels().some(l => l.startsWith('Add folder'))).toBe(true));
      fixture.detectChanges();
    }

    it('sit between the projects and the actions, through sources the user can still see', async () => {
      await openWithRecent();
      expect(optionLabels()).toEqual([
        'Course redesign',
        'Grant proposalShared with you',
        'SyllabiMy Drive/Syllabi',
        'ContractsGoogle Drive',
        'New project',
        'Add folder from Google Drive',
      ]);
      expect(listRecent).toHaveBeenCalledTimes(1);
    });

    it('leave out a folder already on this message', async () => {
      await openWithRecent({ attachedFolderKeys: ['google:drive-a'] });
      expect(optionLabels().some(l => l.startsWith('Syllabi'))).toBe(false);
      expect(optionLabels().some(l => l.startsWith('Contracts'))).toBe(true);
    });

    it('filter with the projects, by name or path', async () => {
      await openWithRecent();
      type('my drive');
      expect(optionLabels()).toEqual(['SyllabiMy Drive/Syllabi', 'New project', 'Add folder from Google Drive']);
      type('nothing here');
      expect(document.querySelector('[data-testid="composer-scope-no-match"]')?.textContent).toContain('No project or folder matches');
      expect(document.querySelector('[data-testid="composer-scope-panel"] [aria-live]')?.textContent).toContain('0 matches');
    });

    it('attach the folder afresh when picked', async () => {
      await openWithRecent();
      const picked: FolderAttachSelection[] = [];
      component.sourceFolderPicked.subscribe(s => picked.push(s));
      optionEls().find(el => el.textContent?.startsWith('Contracts'))!.click();
      expect(picked).toEqual([
        { connector: DRIVE, folder: { folderId: 'drive-b', folderName: 'Contracts', folderPath: 'Contracts' } },
      ]);
      expect(dialogOpen).not.toHaveBeenCalled();
    });

    it('are read again on every open, so a folder just attached comes first', async () => {
      await openWithRecent();
      key('Escape');
      await openPanel();
      await vi.waitFor(() => expect(listRecent).toHaveBeenCalledTimes(2));
    });

    it('cost nothing on hover once read, and nothing at all while folders are off', async () => {
      await build({ recentFolders: [SYLLABI] });
      trigger()!.dispatchEvent(new Event('pointerenter'));
      await vi.waitFor(() => expect(listRecent).toHaveBeenCalledTimes(1));
      trigger()!.dispatchEvent(new Event('pointerenter'));
      await fixture.whenStable();
      expect(listRecent).toHaveBeenCalledTimes(1);
      TestBed.resetTestingModule();
      await build({ remainingFolders: 0, recentFolders: [SYLLABI] });
      trigger()!.dispatchEvent(new Event('pointerenter'));
      await openPanel();
      expect(listRecent).not.toHaveBeenCalled();
    });

    it('leave Add folder working when the list cannot be read', async () => {
      await build({ recentFolders: new Error('boom') });
      await openPanel();
      await vi.waitFor(() => expect(optionLabels()).toContain('Add folder from Google Drive'));
      expect(optionEls().some(el => el.getAttribute('data-testid') === 'composer-scope-option-recent-folder')).toBe(false);
    });
  });
});
