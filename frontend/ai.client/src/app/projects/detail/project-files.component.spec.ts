import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { Dialog } from '@angular/cdk/dialog';
import { of, throwError } from 'rxjs';
import { ProjectFilesComponent, formatBytes } from './project-files.component';
import { ProjectApiService } from '../services/project-api.service';
import { DocumentService, DocumentUploadError } from '../../assistants/services/document.service';
import { Project, ProjectDocument } from '../models/project.model';

const PROJECT: Project = {
  projectId: 'prj_1',
  name: 'Enrollment Sync',
  description: '',
  ownerEmail: 'o@x.edu',
  role: 'editor',
  status: 'active',
  editorsManageMembers: true,
  memberCount: 2,
  harnessAgentId: 'ast-1',
  createdAt: '2026-09-24T00:00:00Z',
  updatedAt: '2026-09-24T00:00:00Z',
};

function doc(id: string, over: Partial<ProjectDocument> = {}): ProjectDocument {
  return {
    documentId: id, assistantId: 'ast-1', filename: `${id}.pdf`, contentType: 'application/pdf',
    sizeBytes: 2048, status: 'complete', createdAt: '2026-09-23T00:00:00Z', updatedAt: '2026-09-23T00:00:00Z',
    addedByEmail: 'ann@x.edu', ...over,
  };
}

const NOTICE = 'Everyone in Enrollment Sync (3 people) can open this file, and the project\'s agent can use it to answer anyone in the project.';

describe('ProjectFilesComponent', () => {
  const api = {
    files: vi.fn(),
    file: vi.fn(),
    fileDownloadUrl: vi.fn(),
    fileUploadUrl: vi.fn(),
    reportFileUploadFailure: vi.fn(),
    deleteFile: vi.fn(),
  };
  const documents = { uploadToS3: vi.fn() };
  const dialog = { open: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.files.mockReturnValue(of({ documents: [doc('a'), doc('b', { addedByEmail: null })], nextToken: null, canEdit: true }));
    TestBed.configureTestingModule({
      imports: [ProjectFilesComponent],
      providers: [
        { provide: ProjectApiService, useValue: api },
        { provide: DocumentService, useValue: documents },
        { provide: Dialog, useValue: dialog },
      ],
    });
  });

  afterEach(() => vi.useRealTimers());

  async function render(project: Project = PROJECT) {
    const fixture = TestBed.createComponent(ProjectFilesComponent);
    fixture.componentRef.setInput('project', project);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return { fixture, el: fixture.nativeElement as HTMLElement, component: fixture.componentInstance as any };
  }

  function pick(component: any, files: File[]): Promise<void> {
    return component.onFilesPicked({ target: { files, value: 'x' } } as unknown as Event);
  }

  it('lists files with who added them, "Unknown" when the server doesn’t know', async () => {
    const { el } = await render();
    expect(api.files).toHaveBeenCalledWith('prj_1', 100, null);
    expect(el.textContent).toContain('Added by ann@x.edu');
    expect(el.textContent).toContain('Added by Unknown');
  });

  it('gives a viewer download but no add or delete', async () => {
    api.files.mockReturnValue(of({ documents: [doc('a')], nextToken: null, canEdit: false }));
    const { el } = await render({ ...PROJECT, role: 'viewer' });
    expect(el.querySelector('button[aria-label="Download a.pdf"]')).not.toBeNull();
    expect(el.querySelector('button[aria-label="Delete a.pdf"]')).toBeNull();
    expect(el.textContent).not.toContain('Add files');
    expect(el.textContent).toContain('Only editors can add or remove files.');
  });

  it('downloads through the project route', async () => {
    api.fileDownloadUrl.mockReturnValue(of({ downloadUrl: 'https://s3/x', filename: 'a.pdf', expiresIn: 60 }));
    const open = vi.spyOn(window, 'open').mockReturnValue(null);
    const { fixture, el } = await render();
    el.querySelector<HTMLButtonElement>('button[aria-label="Download a.pdf"]')!.click();
    await fixture.whenStable();
    expect(api.fileDownloadUrl).toHaveBeenCalledWith('prj_1', 'a');
    expect(open).toHaveBeenCalledWith('https://s3/x', '_blank', 'noopener,noreferrer');
  });

  it('uploads through the presigned URL and then shows the server’s notice', async () => {
    api.fileUploadUrl.mockReturnValue(of({ documentId: 'new', uploadUrl: 'https://s3/put', expiresIn: 60, notice: NOTICE }));
    documents.uploadToS3.mockResolvedValue(undefined);
    const { fixture, el, component } = await render();
    expect(el.textContent).toContain('Everyone in Enrollment Sync can open these files');

    api.files.mockReturnValue(of({ documents: [doc('new', { status: 'complete' }), doc('a')], nextToken: null, canEdit: true }));
    const file = new File(['abc'], 'notes.txt', { type: 'text/plain' });
    await pick(component, [file]);
    fixture.detectChanges();

    expect(api.fileUploadUrl).toHaveBeenCalledWith('prj_1', { filename: 'notes.txt', contentType: 'text/plain', sizeBytes: 3 });
    expect(documents.uploadToS3).toHaveBeenCalledWith('https://s3/put', file, expect.any(Function));
    expect(el.textContent).toContain(NOTICE);
    expect(el.textContent).toContain('new.pdf');
  });

  it('reports an S3 failure so the file doesn’t sit in "uploading"', async () => {
    api.fileUploadUrl.mockReturnValue(of({ documentId: 'new', uploadUrl: 'https://s3/put', expiresIn: 60, notice: NOTICE }));
    documents.uploadToS3.mockRejectedValue(new DocumentUploadError('S3 upload failed: 403', 'S3_UPLOAD_FAILED', { status: 403 }));
    api.reportFileUploadFailure.mockReturnValue(of(doc('new', { status: 'failed' })));
    const { fixture, el, component } = await render();
    await pick(component, [new File(['abc'], 'notes.txt')]);
    fixture.detectChanges();
    expect(api.reportFileUploadFailure).toHaveBeenCalledWith('prj_1', 'new', expect.any(String), expect.any(String));
    expect(el.querySelector('[role=status]')?.textContent).toContain('Upload failed');
  });

  it('refuses a file over the limit before asking the server', async () => {
    const { fixture, el, component } = await render();
    const big = new File(['x'], 'huge.pdf');
    Object.defineProperty(big, 'size', { value: 11 * 1024 * 1024 });
    await pick(component, [big]);
    fixture.detectChanges();
    expect(api.fileUploadUrl).not.toHaveBeenCalled();
    expect(el.textContent).toContain('Files can be up to 10 MB');
  });

  it('shows the API’s sentence when the upload is refused (archived)', async () => {
    api.fileUploadUrl.mockReturnValue(
      throwError(() => new HttpErrorResponse({ status: 409, error: { detail: 'This project is archived. Restore it to make changes.' } })),
    );
    const { fixture, el, component } = await render();
    await pick(component, [new File(['abc'], 'notes.txt')]);
    fixture.detectChanges();
    expect(el.textContent).toContain('This project is archived. Restore it to make changes.');
    expect(api.reportFileUploadFailure).not.toHaveBeenCalled();
  });

  it('polls a processing file until it is ready', async () => {
    vi.useFakeTimers();
    api.files.mockReturnValue(of({ documents: [doc('p', { status: 'chunking' })], nextToken: null, canEdit: true }));
    api.file.mockReturnValueOnce(of(doc('p', { status: 'embedding' }))).mockReturnValueOnce(of(doc('p', { status: 'complete' })));
    const fixture = TestBed.createComponent(ProjectFilesComponent);
    fixture.componentRef.setInput('project', PROJECT);
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(0);
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;
    expect(el.textContent).toContain('Processing');

    await vi.advanceTimersByTimeAsync(1000);
    fixture.detectChanges();
    expect(el.textContent).toContain('Indexing');

    await vi.advanceTimersByTimeAsync(1500);
    fixture.detectChanges();
    expect(el.textContent).toContain('Ready');
    expect(api.file).toHaveBeenCalledTimes(2);
    expect(api.file).toHaveBeenCalledWith('prj_1', 'p');
  });

  it('deletes after confirmation', async () => {
    dialog.open.mockReturnValue({ closed: of(true) });
    api.deleteFile.mockReturnValue(of(undefined));
    const { fixture, el } = await render();
    el.querySelector<HTMLButtonElement>('button[aria-label="Delete a.pdf"]')!.click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(api.deleteFile).toHaveBeenCalledWith('prj_1', 'a');
    expect(el.textContent).not.toContain('a.pdf');
  });

  it('formats sizes', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(2048)).toBe('2 KB');
    expect(formatBytes(10 * 1024 * 1024)).toBe('10 MB');
  });
});
