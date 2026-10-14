import { TestBed } from '@angular/core/testing';
import { Dialog } from '@angular/cdk/dialog';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { expectNamedDialog, openInCdkDialog } from '../../../testing/cdk-dialog';
import {
  FolderAttachment,
  FolderAttachmentService,
  FolderFileEntry,
  FolderRefreshService,
} from '../../services/folder-attachments';
import type { FolderAttachmentData } from '../../session/services/models/message.model';
import { Project } from '../models/project.model';
import { SaveFolderAsProjectService } from '../services/save-folder-as-project.service';
import {
  SaveFolderAsProjectDialogComponent,
  SaveFolderAsProjectDialogData,
  SaveFolderAsProjectDialogResult,
} from './save-folder-as-project-dialog.component';

const CHIP: FolderAttachmentData = { name: 'Syllabi', source: 'Google Drive', status: 'ready', fileCount: 2, files: [], moreCount: 0 };
const PROJECT = { projectId: 'p-1', name: 'Syllabi' } as Project;

function entry(id: string): FolderFileEntry {
  return { providerFileId: id, name: `${id}.pdf`, relativePath: `${id}.pdf`, mimeType: 'application/pdf', sizeBytes: 1, uploadId: `u-${id}`, status: 'attached', error: null };
}

function row(files: FolderFileEntry[]): FolderAttachment {
  return {
    folderAttachId: 'fa-1', sessionId: 's-1', connectorId: 'google-drive', connectorName: 'Google Drive', providerFolderId: 'drive-f',
    folderName: 'Syllabi', folderPath: 'My Drive/Syllabi', status: 'ready', error: null,
    counts: { listed: files.length, attached: files.length, skippedType: 0, skippedSize: 0, failed: 0, truncated: false },
    files, snapshotAt: null, createdAt: '', updatedAt: '', stale: false,
  };
}

describe('SaveFolderAsProjectDialogComponent', () => {
  let locate: ReturnType<typeof vi.fn>;
  let get: ReturnType<typeof vi.fn>;
  let save: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    locate = vi.fn().mockResolvedValue({ sessionId: 's-1', folderAttachId: 'fa-1' });
    get = vi.fn().mockResolvedValue(row([entry('a'), entry('b')]));
    save = vi.fn().mockResolvedValue({ project: PROJECT, imported: 2, notImported: 0, error: null });
    TestBed.configureTestingModule({
      providers: [
        { provide: FolderRefreshService, useValue: { locate } },
        { provide: FolderAttachmentService, useValue: { get } },
        { provide: SaveFolderAsProjectService, useValue: { save } },
      ],
    });
  });
  afterEach(() => {
    TestBed.inject(Dialog).closeAll();
    TestBed.resetTestingModule();
  });

  async function open() {
    const opened = await openInCdkDialog<SaveFolderAsProjectDialogComponent, SaveFolderAsProjectDialogData, SaveFolderAsProjectDialogResult>(
      SaveFolderAsProjectDialogComponent,
      { data: { folder: CHIP } },
    );
    await vi.waitFor(() => expect(get).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    opened.ref.componentRef?.changeDetectorRef.detectChanges();
    const q = <T extends Element>(sel: string) => opened.container.querySelector<T>(sel);
    const refresh = () => opened.ref.componentRef?.changeDetectorRef.detectChanges();
    return { ...opened, q, refresh };
  }

  it('is a named dialog that says what it will add and that it is a copy', async () => {
    const { container, q } = await open();
    expectNamedDialog(container, { name: 'Save as project' });
    expect(locate).toHaveBeenCalledWith(CHIP);
    expect(get).toHaveBeenCalledWith('s-1', 'fa-1');
    expect(q<HTMLInputElement>('#save-folder-project-name')!.value).toBe('Syllabi');
    expect(q('[data-testid="save-folder-summary"]')!.textContent).toContain("2 files from Google Drive will be added to the project's Files.");
    expect(container.textContent).toContain('not picked up');
  });

  it('saves with the trimmed name and the folder as the description, then closes with the result', async () => {
    const { ref, q, refresh } = await open();
    const closed: SaveFolderAsProjectDialogResult[] = [];
    ref.closed.subscribe((r) => closed.push(r));
    const input = q<HTMLInputElement>('#save-folder-project-name')!;
    input.value = '  BIO 101 syllabi  ';
    input.dispatchEvent(new Event('input'));
    refresh();
    q<HTMLButtonElement>('[data-testid="save-folder-submit"]')!.click();
    await vi.waitFor(() => expect(closed).toHaveLength(1));
    expect(save.mock.calls[0][1]).toBe('BIO 101 syllabi');
    expect(save.mock.calls[0][2]).toBe('Files from the folder “My Drive/Syllabi” in Google Drive.');
    expect(closed[0]).toEqual({ project: PROJECT, imported: 2, notImported: 0, error: null });
  });

  it('cannot save a folder with no readable files', async () => {
    get.mockResolvedValue(row([]));
    const { q } = await open();
    expect(q('[data-testid="save-folder-summary"]')!.textContent).toContain('no readable files');
    expect(q<HTMLButtonElement>('[data-testid="save-folder-submit"]')!.disabled).toBe(true);
  });

  it('says why when the folder is gone from the conversation', async () => {
    locate.mockRejectedValue(new Error('This folder is no longer attached to the conversation.'));
    const opened = await openInCdkDialog(SaveFolderAsProjectDialogComponent, { data: { folder: CHIP } });
    await new Promise((resolve) => setTimeout(resolve, 0));
    opened.ref.componentRef?.changeDetectorRef.detectChanges();
    expect(opened.container.querySelector('[role="alert"]')!.textContent).toContain('no longer attached');
    expect(opened.container.querySelector<HTMLButtonElement>('[data-testid="save-folder-submit"]')!.disabled).toBe(true);
  });

  it('after a partial import, offers to open the project instead of saving again', async () => {
    save.mockResolvedValue({ project: PROJECT, imported: 50, notImported: 20, error: 'Reconnect Google Drive' });
    const { ref, q, refresh } = await open();
    const closed: SaveFolderAsProjectDialogResult[] = [];
    ref.closed.subscribe((r) => closed.push(r));
    q<HTMLButtonElement>('[data-testid="save-folder-submit"]')!.click();
    await vi.waitFor(() => expect(save).toHaveBeenCalled());
    await new Promise((resolve) => setTimeout(resolve, 0));
    refresh();
    expect(q('[role="alert"]')!.textContent).toContain('20 of 70 files were not added: Reconnect Google Drive');
    expect(q('[data-testid="save-folder-submit"]')).toBeNull();
    q<HTMLButtonElement>('[data-testid="save-folder-open-partial"]')!.click();
    expect(closed[0]?.error).toBe('Reconnect Google Drive');
  });
});
