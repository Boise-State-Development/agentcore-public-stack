import { Injectable, inject, signal } from '@angular/core';
import { ChatStateService } from '../../session/services/chat/chat-state.service';
import type { FolderAttachmentData } from '../../session/services/models/message.model';
import {
  FolderAttachment,
  FolderAttachmentError,
  FolderAttachmentService,
} from './folder-attachment.service';

/** A refreshed folder on its way from a sent chip to the composer. */
export interface FolderHandoff {
  sessionId: string;
  row: FolderAttachment;
}

/**
 * Refresh from a sent folder chip (drive-folder spec §4.6, §4.7).
 *
 * The chip sits in the message list and the composer is a sibling, so this
 * root service carries the refreshed row across: the chip starts the refresh,
 * and the composer picks the folder up as a card, so the next message carries
 * a manifest of the folder as it is now. The earlier message's manifest is
 * never rewritten (the prompt-cache contract).
 */
@Injectable({ providedIn: 'root' })
export class FolderRefreshService {
  private readonly folders = inject(FolderAttachmentService);
  private readonly chatState = inject(ChatStateService);

  /** The latest refreshed folder for the composer to adopt; it clears it once taken. */
  readonly handoff = signal<FolderHandoff | null>(null);

  /**
   * Refresh the chip's folder in the conversation on screen. A chip rebuilt
   * from history knows the folder's name but not its id, so the id comes from
   * the conversation's folder list (newest first).
   *
   * @throws FolderAttachmentError with the server's reason (409 consent or
   *   already running, 404 when the folder is no longer attached)
   */
  async refresh(folder: FolderAttachmentData): Promise<FolderAttachment> {
    const { sessionId, folderAttachId } = await this.locate(folder);
    const row = await this.folders.refresh(sessionId, folderAttachId);
    this.handoff.set({ sessionId, row });
    return row;
  }

  /**
   * The conversation on screen and the folder row a sent chip stands for. A
   * chip rebuilt from history knows the folder's name but not its id, so the
   * id comes from the conversation's folder list (newest first).
   *
   * @throws FolderAttachmentError (0 with no conversation open, 404 when the
   *   folder is no longer attached)
   */
  async locate(folder: FolderAttachmentData): Promise<{ sessionId: string; folderAttachId: string }> {
    const sessionId = this.chatState.viewedSessionId();
    if (!sessionId) {
      throw new FolderAttachmentError('Open the conversation to use its folder.', 0);
    }
    if (folder.folderAttachId) return { sessionId, folderAttachId: folder.folderAttachId };
    const match = (await this.folders.list(sessionId)).find((row) => row.folderName === folder.name);
    if (!match) {
      throw new FolderAttachmentError('This folder is no longer attached to the conversation.', 404);
    }
    return { sessionId, folderAttachId: match.folderAttachId };
  }

  /** The composer took the handoff. */
  take(): void {
    this.handoff.set(null);
  }
}
