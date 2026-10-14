import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { FileSourceService } from '../../assistants/services/file-source.service';
import { FolderAttachment, FolderFileEntry } from '../../services/folder-attachments';
import { Project } from '../models/project.model';
import { ProjectsService } from './projects.service';
import {
  FOLDER_IMPORT_BATCH_SIZE,
  SaveFolderAsProjectService,
  folderProjectDescription,
  importableFolderFiles,
} from './save-folder-as-project.service';

function entry(over: Partial<FolderFileEntry>): FolderFileEntry {
  return {
    providerFileId: 'd1',
    name: 'a.pdf',
    relativePath: 'a.pdf',
    mimeType: 'application/pdf',
    sizeBytes: 10,
    uploadId: 'u1',
    status: 'attached',
    error: null,
    ...over,
  };
}

function row(files: FolderFileEntry[], over: Partial<FolderAttachment> = {}): FolderAttachment {
  return {
    folderAttachId: 'fa-1',
    sessionId: 's-1',
    connectorId: 'google-drive',
    connectorName: 'Google Drive',
    providerFolderId: 'drive-f',
    folderName: 'Syllabi',
    folderPath: 'My Drive/Syllabi',
    status: 'ready',
    error: null,
    counts: { listed: files.length, attached: files.length, skippedType: 0, skippedSize: 0, failed: 0, truncated: false },
    files,
    snapshotAt: null,
    createdAt: '2026-10-14T00:00:00Z',
    updatedAt: '2026-10-14T00:00:00Z',
    stale: false,
    ...over,
  };
}

const PROJECT = { projectId: 'p-1', name: 'Syllabi', harnessAgentId: 'h-1' } as Project;

describe('importableFolderFiles', () => {
  it('keeps attached files still in the folder, once each, by provider id and download name', () => {
    const files = importableFolderFiles(
      row([
        entry({ providerFileId: 'd1', name: 'syllabus.pdf' }),
        entry({ providerFileId: 'd2', name: 'Rubric.docx', relativePath: 'week 1/Rubric.docx' }),
        entry({ providerFileId: 'd1', name: 'syllabus.pdf' }),
        entry({ providerFileId: 'd3', status: 'skipped_size' }),
        entry({ providerFileId: 'd4', status: 'failed' }),
        entry({ providerFileId: 'd5', status: 'removed' }),
        entry({ providerFileId: 'd6', removedFromSource: true }),
      ]),
    );
    expect(files).toEqual([
      { fileId: 'd1', name: 'syllabus.pdf' },
      { fileId: 'd2', name: 'Rubric.docx' },
    ]);
  });

  it('describes where the files came from', () => {
    expect(folderProjectDescription(row([]))).toBe('Files from the folder “My Drive/Syllabi” in Google Drive.');
    expect(folderProjectDescription(row([], { folderPath: null, connectorName: null }))).toBe('Files from the folder “Syllabi”.');
  });
});

describe('SaveFolderAsProjectService', () => {
  let create: ReturnType<typeof vi.fn>;
  let importDocuments: ReturnType<typeof vi.fn>;
  let service: SaveFolderAsProjectService;

  beforeEach(() => {
    create = vi.fn().mockResolvedValue(PROJECT);
    importDocuments = vi.fn().mockResolvedValue({ documents: [] });
    TestBed.configureTestingModule({
      providers: [
        { provide: ProjectsService, useValue: { create } },
        { provide: FileSourceService, useValue: { importDocuments } },
      ],
    });
    service = TestBed.inject(SaveFolderAsProjectService);
  });
  afterEach(() => TestBed.resetTestingModule());

  const many = (n: number) => Array.from({ length: n }, (_, i) => entry({ providerFileId: `d${i}`, name: `f${i}.pdf` }));

  it('creates the project, then imports into it in batches the route accepts', async () => {
    const result = await service.save(row(many(120)), 'Syllabi', 'desc');

    expect(create).toHaveBeenCalledWith('Syllabi', 'desc');
    expect(importDocuments.mock.calls.map((call) => call[2].length)).toEqual([FOLDER_IMPORT_BATCH_SIZE, FOLDER_IMPORT_BATCH_SIZE, 20]);
    for (const call of importDocuments.mock.calls) {
      expect(call[0]).toEqual({ projectId: 'p-1' });
      expect(call[1]).toBe('google-drive');
    }
    expect(result).toEqual({ project: PROJECT, imported: 120, notImported: 0, error: null });
  });

  it('throws and imports nothing when the project cannot be created', async () => {
    create.mockRejectedValue(new Error('no'));
    await expect(service.save(row(many(3)), 'Syllabi', '')).rejects.toThrow('no');
    expect(importDocuments).not.toHaveBeenCalled();
  });

  it('reports a failed batch without throwing, since the project exists', async () => {
    importDocuments.mockResolvedValueOnce({ documents: [] }).mockRejectedValueOnce(new Error('Reconnect Google Drive'));
    const result = await service.save(row(many(70)), 'Syllabi', '');
    expect(result).toEqual({ project: PROJECT, imported: 50, notImported: 20, error: 'Reconnect Google Drive' });
  });
});
