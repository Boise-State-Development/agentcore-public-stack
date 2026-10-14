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

  interface BuildOptions {
    projectsFlag?: boolean;
    remainingFolders?: number;
    assistantId?: string | null;
    available?: boolean | null;
    list?: Project[];
  }

  async function build(opts: BuildOptions = {}): Promise<void> {
    const {
      projectsFlag = true,
      remainingFolders = 2,
      assistantId = null,
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
    queryParams = new BehaviorSubject(convertToParamMap(assistantId ? { assistantId } : {}));

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
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(ComposerScopePickerComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('remainingFiles', 5);
    fixture.componentRef.setInput('remainingFolders', remainingFolders);
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
    it('binds a project through its harness Agent on the current URL', async () => {
      await build();
      await openPanel();
      key('ArrowDown');
      key('Enter');
      expect(navigate).toHaveBeenCalledWith([], expect.objectContaining({
        queryParams: { assistantId: 'harness-2' },
        queryParamsHandling: 'merge',
      }));
    });

    it('clears the project with No project', async () => {
      await build({ assistantId: 'harness-1' });
      await openPanel();
      optionEls()[0].click();
      expect(navigate).toHaveBeenCalledWith([], expect.objectContaining({ queryParams: { assistantId: null } }));
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
      expect(navigate).toHaveBeenCalledWith([], expect.objectContaining({ queryParams: { assistantId: 'harness-9' } }));
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
});
