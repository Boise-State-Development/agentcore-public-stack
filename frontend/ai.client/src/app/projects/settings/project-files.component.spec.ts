import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { Dialog } from '@angular/cdk/dialog';
import { signal } from '@angular/core';
import { of, throwError } from 'rxjs';
import { ProjectFilesComponent, formatBytes } from './project-files.component';
import { ProjectApiService } from '../services/project-api.service';
import { DocumentService, DocumentUploadError } from '../../assistants/services/document.service';
import { Project, ProjectDocument } from '../models/project.model';
import { FileSourceService } from '../../assistants/services/file-source.service';
import { WebSourceService } from '../../assistants/services/web-source.service';
import { SyncPolicyService } from '../../assistants/services/sync-policy.service';
import { UserConnectorsService } from '../../settings/connectors/services/user-connectors.service';
import { OAuthConsentService } from '../../services/oauth-consent/oauth-consent.service';
import { ToastService } from '../../services/toast/toast.service';
import { WebSourceDialogComponent } from '../../assistants/components/web-source-dialog.component';
import { FileSourceBrowserDialogComponent } from '../../assistants/components/file-source-browser-dialog.component';
import { ExtractedContentDialogComponent } from '../../assistants/components/extracted-content-dialog.component';
import { SyncPolicy } from '../../assistants/models/sync-policy.model';
import { CrawlJob } from '../../assistants/models/web-source.model';

const PROJECT: Project = {
  projectId: 'prj_1',
  name: 'Enrollment Sync',
  description: '',
  ownerEmail: 'o@x.edu',
  ownerName: null,
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
    addedByEmail: 'ann@x.edu', addedByName: null, ...over,
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
  const fileSources = { listFileSources: vi.fn() };
  const webSources = { listCrawls: vi.fn(), listActiveCrawls: vi.fn(), deleteCrawl: vi.fn() };
  const syncPolicies = { listPolicies: vi.fn(), createPolicy: vi.fn(), updatePolicy: vi.fn(), deletePolicy: vi.fn(), runNow: vi.fn() };
  const connectors = { initiateConsent: vi.fn() };
  const consent = {
    completion: signal(null),
    inFlightProviders: signal(new Set<string>()),
    acknowledgeCompletion: vi.fn(),
    startUserConsent: vi.fn(),
  };
  const toast = { success: vi.fn(), error: vi.fn() };

  beforeEach(() => {
    TestBed.resetTestingModule();
    vi.clearAllMocks();
    api.files.mockReturnValue(of({ documents: [doc('a'), doc('b', { addedByEmail: null })], nextToken: null, canEdit: true }));
    fileSources.listFileSources.mockResolvedValue([]);
    webSources.listCrawls.mockResolvedValue([]);
    webSources.listActiveCrawls.mockResolvedValue([]);
    syncPolicies.listPolicies.mockResolvedValue([]);
    TestBed.configureTestingModule({
      imports: [ProjectFilesComponent],
      providers: [
        { provide: ProjectApiService, useValue: api },
        { provide: DocumentService, useValue: documents },
        { provide: Dialog, useValue: dialog },
        { provide: FileSourceService, useValue: fileSources },
        { provide: WebSourceService, useValue: webSources },
        { provide: SyncPolicyService, useValue: syncPolicies },
        { provide: UserConnectorsService, useValue: connectors },
        { provide: OAuthConsentService, useValue: consent },
        { provide: ToastService, useValue: toast },
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
    // An editor's connectors, web sources and schedules load once the list says they edit.
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

  it('names who added a file when the directory knows them, with the email on hover', async () => {
    api.files.mockReturnValue(of({ documents: [doc('a', { addedByName: 'Ann Lee' })], nextToken: null, canEdit: true }));
    const { el } = await render();
    expect(el.textContent?.replace(/\s+/g, ' ')).toContain('Added by Ann Lee');
    expect(el.querySelector('span[title="ann@x.edu"]')?.textContent).toBe('Ann Lee');
  });

  it('gives a viewer download but no add or delete', async () => {
    api.files.mockReturnValue(of({ documents: [doc('a')], nextToken: null, canEdit: false }));
    const { el } = await render({ ...PROJECT, role: 'viewer' });
    expect(el.querySelector('button[aria-label="Download a.pdf"]')).not.toBeNull();
    expect(el.querySelector('button[aria-label="Delete a.pdf"]')).toBeNull();
    expect(el.textContent).not.toContain('Add files');
    expect(el.textContent).not.toContain('Add web content');
    expect(el.querySelector('button[aria-label^="View the content extracted"]')).toBeNull();
    expect(el.textContent).toContain('Only editors can add or remove files.');
    // Connectors, web sources and schedules are editor-only routes: a viewer never asks.
    expect(fileSources.listFileSources).not.toHaveBeenCalled();
    expect(webSources.listCrawls).not.toHaveBeenCalled();
    expect(syncPolicies.listPolicies).not.toHaveBeenCalled();
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

  // ---- web content, file sources and sync (the knowledge base's sources) ----

  const PROJECT_TARGET = { projectId: 'prj_1' };

  function crawl(over: Partial<CrawlJob> = {}): CrawlJob {
    return {
      crawlId: 'crawl-1', assistantId: 'ast-1', rootUrl: 'https://catalog.x.edu/', status: 'complete',
      settings: { maxDepth: 1, maxPages: 10, concurrency: 1, minDelay: 1, maxDelay: 2, sameDomainOnly: true },
      discoveredCount: 2, fetchedCount: 2, failedCount: 0, startedAt: '2026-09-23T00:00:00Z',
      completedAt: '2026-09-23T00:05:00Z', startedByUserId: 'u1', ...over,
    };
  }

  function policy(over: Partial<SyncPolicy> = {}): SyncPolicy {
    return {
      policyId: 'syn-1', assistantId: 'ast-1', sourceType: 'drive_file', sourceRef: 'g', interval: 'weekly',
      state: 'active', createdAt: '2026-09-23T00:00:00Z', updatedAt: '2026-09-23T00:00:00Z', ...over,
    };
  }

  const drive = doc('g', { filename: 'Roster', sourceConnectorId: 'gdrive', sourceFileId: 'f-1', sourceAdapterKey: 'google' });

  it('offers editors web content and their connected file sources, aimed at the project', async () => {
    fileSources.listFileSources.mockResolvedValue([
      { providerId: 'gdrive', displayName: 'Google Drive', iconName: 'drive', connected: true },
    ]);
    dialog.open.mockReturnValue({ closed: of(undefined) });
    const { fixture, el } = await render();
    expect(el.textContent).toContain('Add web content');

    const buttons = Array.from(el.querySelectorAll('button'));
    buttons.find(b => b.textContent?.includes('Google Drive'))!.click();
    await fixture.whenStable();
    expect(dialog.open).toHaveBeenCalledWith(FileSourceBrowserDialogComponent, expect.objectContaining({
      data: { target: PROJECT_TARGET, connector: expect.objectContaining({ providerId: 'gdrive' }) },
    }));

    buttons.find(b => b.textContent?.includes('Add web content'))!.click();
    await fixture.whenStable();
    expect(dialog.open).toHaveBeenCalledWith(WebSourceDialogComponent, expect.objectContaining({ data: { target: PROJECT_TARGET } }));
  });

  it('connects a file source that isn’t connected yet before browsing it', async () => {
    fileSources.listFileSources.mockResolvedValue([
      { providerId: 'gdrive', displayName: 'Google Drive', iconName: 'drive', connected: false },
    ]);
    connectors.initiateConsent.mockResolvedValue({ connected: false, authorizationUrl: 'https://accounts/consent' });
    const { fixture, el } = await render();
    const button = Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Connect to Google Drive'))!;
    button.click();
    await fixture.whenStable();
    expect(consent.startUserConsent).toHaveBeenCalledWith('gdrive', 'https://accounts/consent');
    expect(dialog.open).not.toHaveBeenCalled();
  });

  it('refreshes the files after an import', async () => {
    fileSources.listFileSources.mockResolvedValue([
      { providerId: 'gdrive', displayName: 'Google Drive', iconName: 'drive', connected: true },
    ]);
    dialog.open.mockReturnValue({ closed: of([drive]) });
    const { fixture, el } = await render();
    api.files.mockReturnValue(of({ documents: [drive, doc('a')], nextToken: null, canEdit: true }));
    Array.from(el.querySelectorAll('button')).find(b => b.textContent?.includes('Google Drive'))!.click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(toast.success).toHaveBeenCalledWith('Importing 1 file…');
    expect(el.textContent).toContain('Roster');
  });

  it('lists web sources and removes one with its pages', async () => {
    const page = doc('pg', { filename: 'Catalog', sourceConnectorId: 'web', sourceFileId: 'https://catalog.x.edu/courses' });
    api.files.mockReturnValue(of({ documents: [page, doc('a')], nextToken: null, canEdit: true }));
    webSources.listCrawls.mockResolvedValue([crawl()]);
    webSources.deleteCrawl.mockResolvedValue(undefined);
    dialog.open.mockReturnValue({ closed: of(true) });
    const { fixture, el } = await render();
    expect(el.textContent).toContain('Web sources');
    expect(el.textContent).toContain('https://catalog.x.edu/');

    el.querySelector<HTMLButtonElement>('button[aria-label="Remove https://catalog.x.edu/"]')!.click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(webSources.deleteCrawl).toHaveBeenCalledWith(PROJECT_TARGET, 'crawl-1');
    expect(el.textContent).not.toContain('Web sources');
    expect(el.textContent).not.toContain('Catalog');
    expect(el.textContent).toContain('a.pdf');
  });

  it('keeps refreshing while a crawl runs, then stops', async () => {
    vi.useFakeTimers();
    webSources.listCrawls.mockResolvedValue([crawl({ status: 'running' })]);
    webSources.listActiveCrawls.mockResolvedValueOnce([crawl({ status: 'running' })]).mockResolvedValue([]);
    const fixture = TestBed.createComponent(ProjectFilesComponent);
    fixture.componentRef.setInput('project', PROJECT);
    fixture.detectChanges();
    await vi.advanceTimersByTimeAsync(0);
    fixture.detectChanges();
    expect((fixture.nativeElement as HTMLElement).textContent).toContain('Crawling…');
    const loads = api.files.mock.calls.length;

    webSources.listCrawls.mockResolvedValue([crawl()]);
    await vi.advanceTimersByTimeAsync(5000);
    await vi.advanceTimersByTimeAsync(5000);
    fixture.detectChanges();
    expect(api.files.mock.calls.length).toBe(loads + 2);
    expect((fixture.nativeElement as HTMLElement).textContent).not.toContain('Crawling…');

    await vi.advanceTimersByTimeAsync(15000);
    expect(api.files.mock.calls.length).toBe(loads + 2);
    expect(webSources.listActiveCrawls).toHaveBeenCalledWith(PROJECT_TARGET);
  });

  it('schedules a sync for an imported file through the project', async () => {
    api.files.mockReturnValue(of({ documents: [drive], nextToken: null, canEdit: true }));
    syncPolicies.createPolicy.mockResolvedValue(policy());
    const { component } = await render();
    expect(syncPolicies.listPolicies).toHaveBeenCalledWith(PROJECT_TARGET);

    await component.onSyncIntervalSelected('drive_file', 'g', 'weekly');
    expect(syncPolicies.createPolicy).toHaveBeenCalledWith(PROJECT_TARGET, { sourceType: 'drive_file', sourceRef: 'g', interval: 'weekly' });
    expect(component.syncPolicyFor('g')?.policyId).toBe('syn-1');

    syncPolicies.updatePolicy.mockResolvedValue(policy({ state: 'paused_user' }));
    await component.onSyncPause('g');
    expect(syncPolicies.updatePolicy).toHaveBeenCalledWith(PROJECT_TARGET, 'syn-1', { state: 'paused_user' });
    expect(component.syncPolicyFor('g')?.state).toBe('paused_user');

    syncPolicies.runNow.mockRejectedValue(new Error('A manual sync was already requested recently'));
    await component.onSyncRunNow('g');
    expect(toast.error).toHaveBeenCalledWith('A manual sync was already requested recently');

    syncPolicies.deletePolicy.mockResolvedValue(undefined);
    await component.onSyncIntervalSelected('drive_file', 'g', 'manual');
    expect(syncPolicies.deletePolicy).toHaveBeenCalledWith(PROJECT_TARGET, 'syn-1');
    expect(component.syncPolicyFor('g')).toBeNull();
  });

  it('shows sync controls only on files with an original to sync from', async () => {
    api.files.mockReturnValue(of({ documents: [drive, doc('a')], nextToken: null, canEdit: true }));
    const { el } = await render();
    expect(el.querySelectorAll('app-sync-policy-control').length).toBe(1);
  });

  it('opens the extracted content through the project', async () => {
    const { fixture, el } = await render();
    el.querySelector<HTMLButtonElement>('button[aria-label="View the content extracted from a.pdf"]')!.click();
    await fixture.whenStable();
    expect(dialog.open).toHaveBeenCalledWith(ExtractedContentDialogComponent, expect.objectContaining({
      data: { target: PROJECT_TARGET, documentId: 'a', filename: 'a.pdf' },
    }));
  });

  it('formats sizes', () => {
    expect(formatBytes(0)).toBe('0 B');
    expect(formatBytes(2048)).toBe('2 KB');
    expect(formatBytes(10 * 1024 * 1024)).toBe('10 MB');
  });
});
