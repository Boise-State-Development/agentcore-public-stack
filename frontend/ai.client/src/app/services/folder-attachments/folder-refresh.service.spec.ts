import { describe, expect, it, vi } from 'vitest';
import { TestBed } from '@angular/core/testing';
import { signal } from '@angular/core';
import { FolderRefreshService } from './folder-refresh.service';
import { FolderAttachment, FolderAttachmentError, FolderAttachmentService } from './folder-attachment.service';
import { ChatStateService } from '../../session/services/chat/chat-state.service';

const ROW = { folderAttachId: 'fa-1', sessionId: 's-1', folderName: 'Course', status: 'expanding', refreshing: true } as unknown as FolderAttachment;
const CHIP = { name: 'Course', source: 'Drive', status: 'ready', fileCount: 2, files: [], moreCount: 0 };

function setup(viewed: string | null = 's-1') {
  const folders = {
    list: vi.fn().mockResolvedValue([{ ...ROW, folderAttachId: 'fa-new', folderName: 'Course' }, { ...ROW, folderAttachId: 'fa-x', folderName: 'Other' }]),
    refresh: vi.fn().mockResolvedValue(ROW),
  };
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [
      { provide: FolderAttachmentService, useValue: folders },
      { provide: ChatStateService, useValue: { viewedSessionId: signal(viewed) } },
    ],
  });
  return { service: TestBed.inject(FolderRefreshService), folders };
}

describe('FolderRefreshService', () => {
  it('refreshes by id when the chip knows it, and hands the row to the composer', async () => {
    const { service, folders } = setup();
    await service.refresh({ ...CHIP, folderAttachId: 'fa-1' });
    expect(folders.list).not.toHaveBeenCalled();
    expect(folders.refresh).toHaveBeenCalledWith('s-1', 'fa-1');
    expect(service.handoff()).toEqual({ sessionId: 's-1', row: ROW });
    service.take();
    expect(service.handoff()).toBeNull();
  });

  it('finds a reloaded chip\'s folder by name, newest first', async () => {
    const { service, folders } = setup();
    await service.refresh(CHIP);
    expect(folders.list).toHaveBeenCalledWith('s-1');
    expect(folders.refresh).toHaveBeenCalledWith('s-1', 'fa-new');
  });

  it('says so when the folder is no longer attached', async () => {
    const { service, folders } = setup();
    const error = await service.refresh({ ...CHIP, name: 'Gone' }).catch((e) => e);
    expect(error).toBeInstanceOf(FolderAttachmentError);
    expect(error.status).toBe(404);
    expect(folders.refresh).not.toHaveBeenCalled();
    expect(service.handoff()).toBeNull();
  });

  it('needs a conversation on screen', async () => {
    const { service } = setup(null);
    await expect(service.refresh(CHIP)).rejects.toBeInstanceOf(FolderAttachmentError);
  });
});
