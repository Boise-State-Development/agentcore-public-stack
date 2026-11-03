import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { of, throwError } from 'rxjs';
import {
  ProjectFileReadinessComponent,
  READINESS_POLL_MAX_MS,
  READINESS_POLL_START_MS,
  READY_FLASH_MS,
} from './project-file-readiness.component';
import { ProjectApiService } from '../../../projects/services/project-api.service';
import { ProjectDocument } from '../../../projects/models/project.model';
import { DocumentStatus } from '../../../assistants/models/document.model';

function doc(id: string, status: DocumentStatus = 'complete'): ProjectDocument {
  return {
    documentId: id,
    assistantId: 'ast-1',
    filename: `${id}.pdf`,
    contentType: 'application/pdf',
    sizeBytes: 100,
    status,
    createdAt: '2026-11-03T15:04:00Z',
    updatedAt: '2026-11-03T15:04:00Z',
    addedByEmail: null,
    addedByName: null,
  } as ProjectDocument;
}

describe('ProjectFileReadinessComponent', () => {
  let files: ReturnType<typeof vi.fn>;
  let visibility: DocumentVisibilityState;

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.useFakeTimers();
    visibility = 'visible';
    vi.spyOn(document, 'visibilityState', 'get').mockImplementation(() => visibility);
    files = vi.fn();
    TestBed.configureTestingModule({
      imports: [ProjectFileReadinessComponent],
      providers: [provideRouter([]), { provide: ProjectApiService, useValue: { files } }],
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
  });

  function respond(...documents: ProjectDocument[]): void {
    files.mockReturnValue(of({ documents, nextToken: null, canEdit: true }));
  }

  async function mount(projectId = 'prj_1'): Promise<{ fixture: ComponentFixture<ProjectFileReadinessComponent>; el: HTMLElement }> {
    const fixture = TestBed.createComponent(ProjectFileReadinessComponent);
    fixture.componentRef.setInput('projectId', projectId);
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(0);
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement };
  }

  async function tick(fixture: ComponentFixture<ProjectFileReadinessComponent>, ms: number): Promise<void> {
    await vi.advanceTimersByTimeAsync(ms);
    fixture.detectChanges();
  }

  it('shows nothing, and stops asking, when every file is ready', async () => {
    respond(doc('a'), doc('b'));
    const { fixture, el } = await mount();

    expect(el.textContent?.trim()).toBe('');
    await tick(fixture, READINESS_POLL_MAX_MS * 4);
    expect(files).toHaveBeenCalledTimes(1);
  });

  it('says how many files are still preparing, and links to the Files page', async () => {
    respond(doc('a'), doc('b', 'provisioning'), doc('c', 'embedding'));
    const { el } = await mount();

    const status = el.querySelector('[role=status]')!;
    expect(status.textContent).toContain('Preparing 2 of 3 project files.');
    expect(status.textContent).toContain("The assistant can't use them until they are ready");
    expect(el.querySelector('a')?.getAttribute('href')).toBe('/projects/prj_1/files');
  });

  it('polls while files prepare, backing off, then says they are ready and goes', async () => {
    respond(doc('a', 'provisioning'), doc('b', 'uploading'));
    const { fixture, el } = await mount();
    expect(files).toHaveBeenCalledTimes(1);

    await tick(fixture, READINESS_POLL_START_MS);
    expect(files).toHaveBeenCalledTimes(2);
    // The next check backs off.
    await tick(fixture, READINESS_POLL_START_MS);
    expect(files).toHaveBeenCalledTimes(2);

    respond(doc('a'), doc('b'));
    await tick(fixture, READINESS_POLL_START_MS * 0.5);
    expect(files).toHaveBeenCalledTimes(3);
    expect(el.textContent).toContain('All 2 project files are ready.');

    await tick(fixture, READY_FLASH_MS);
    expect(el.textContent?.trim()).toBe('');
    await tick(fixture, READINESS_POLL_MAX_MS * 4);
    expect(files).toHaveBeenCalledTimes(3);
  });

  it('never says "all ready" when one of them failed', async () => {
    // Dev, 2026-11-03: a project with one file ready and one failed flashed
    // "All 2 project files are ready."
    respond(doc('a', 'embedding'), doc('b', 'failed'));
    const { fixture, el } = await mount();
    expect(el.textContent).toContain("Preparing 1 of 2 project files.");

    respond(doc('a'), doc('b', 'failed'));
    await tick(fixture, READINESS_POLL_START_MS);
    expect(el.textContent).toContain("1 of 2 project files is ready. 1 couldn't be added.");
    expect(el.textContent).not.toContain('All 2');

    await tick(fixture, READY_FLASH_MS);
    expect(el.textContent).toContain("1 project file couldn't be added");
  });

  it('keeps its links readable in dark mode', async () => {
    respond(doc('a', 'provisioning'));
    const { el } = await mount();
    expect(el.querySelector('a')?.className).toContain('dark:text-primary-accessible-dark');
  });

  it('names files that could not be added, and can be dismissed', async () => {
    respond(doc('a'), doc('b', 'failed'));
    const { fixture, el } = await mount();

    expect(el.textContent).toContain("1 project file couldn't be added");
    expect(el.querySelector('a')?.textContent).toContain('Upload again from Files');
    el.querySelector<HTMLButtonElement>('button[aria-label="Dismiss notice"]')!.click();
    fixture.detectChanges();
    expect(el.textContent?.trim()).toBe('');
  });

  it('asks nothing while the tab is hidden, and re-checks when it is back', async () => {
    respond(doc('a', 'provisioning'));
    const { fixture, el } = await mount();

    visibility = 'hidden';
    document.dispatchEvent(new Event('visibilitychange'));
    await tick(fixture, READINESS_POLL_MAX_MS * 10);
    expect(files).toHaveBeenCalledTimes(1);

    respond(doc('a'));
    visibility = 'visible';
    document.dispatchEvent(new Event('visibilitychange'));
    await tick(fixture, 0);
    expect(files).toHaveBeenCalledTimes(2);
    expect(el.textContent).toContain('The project file is ready.');
  });

  it('stays quiet when the list cannot be read', async () => {
    files.mockReturnValue(throwError(() => new Error('offline')));
    const { el } = await mount();
    expect(el.textContent?.trim()).toBe('');
  });

  it('starts over for another project', async () => {
    respond(doc('a', 'provisioning'));
    const { fixture, el } = await mount('prj_1');
    expect(el.textContent).toContain('Preparing 1 of 1 project file.');

    respond(doc('x'));
    fixture.componentRef.setInput('projectId', 'prj_2');
    fixture.detectChanges();
    await tick(fixture, 0);
    expect(files).toHaveBeenLastCalledWith('prj_2', 100, null);
    // A new project that is ready shows nothing (no "ready" flash for a switch).
    expect(el.textContent?.trim()).toBe('');
  });
});
