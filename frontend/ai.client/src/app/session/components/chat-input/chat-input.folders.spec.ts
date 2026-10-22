import { NO_ERRORS_SCHEMA, computed, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { Router } from '@angular/router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentMentionService, MentionableAgent } from '../../../agents/services/agent-mention.service';
import { SkillCommand, SkillCommandService } from '../../../services/skill/skill-command.service';
import { FileUploadService } from '../../../services/file-upload';
import { FEATURES } from '../../../services/features';
import {
  FolderAttachment,
  FolderAttachmentError,
  FolderAttachmentService,
  FolderRefreshService,
} from '../../../services/folder-attachments';
import { UserConnectorsService } from '../../../settings/connectors/services/user-connectors.service';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { SystemPromptsService } from '../../../services/system-prompts/system-prompts.service';
import { ToastService } from '../../../services/toast/toast.service';
import { ToolService } from '../../../services/tool/tool.service';
import { ModelService } from '../../services/model/model.service';
import { VoiceChatService } from '../../services/voice';
import { SteeringService } from '../../services/chat/steering.service';
import { FolderAttachSelection } from '../../../assistants/components/file-source-browser-dialog.component';
import { ChatInputComponent } from './chat-input.component';
import type { ComposerFolder } from './folder-card.component';

/**
 * Drive folders on the composer (docs/specs/drive-folder-attachments.md §4.7):
 * the slot each takes, attaching and polling, Remove, the send, the queue and
 * the draft. Services are DI stand-ins, per the house rule.
 */

class SteeringServiceStub {
  readonly applied = signal<string[]>([]);
  readonly armCalls: string[] = [];
  canSteer(): boolean { return true; }
  markToolUsed(): void {}
  startTurn(): void {}
  async arm(_s: string, entryId: string): Promise<boolean> { this.armCalls.push(entryId); return true; }
  async withdraw(): Promise<void> {}
  shouldHoldQueue(): boolean { return false; }
  publishQueue(): void {}
  carriedFor(): { id: string; text: string }[] { return []; }
  recordApplied(): void {}
  consumeApplied(): void {}
  reset(): void {}
}
class SkillCommandServiceStub {
  readonly commands = signal<SkillCommand[]>([]);
  readonly loading = signal(false);
  readonly agentLocked = signal(false);
  async load(): Promise<void> {}
  search(): SkillCommand[] { return []; }
  bySlugOrUndefined(): SkillCommand | undefined { return undefined; }
}
class ModelServiceStub {
  readonly selectedModel = signal({ modelId: 'm', inputModalities: ['TEXT'] });
}
class MentionServiceStub {
  readonly mentionable = signal<MentionableAgent[]>([]);
  readonly loading = signal(false);
  async load(): Promise<void> {}
  search(): MentionableAgent[] { return []; }
}

const CONNECTOR = { providerId: 'google-drive', displayName: 'Google Drive', iconName: 'heroCloud', connected: true };
const PICK: FolderAttachSelection = {
  connector: CONNECTOR,
  folder: { folderId: 'drive-f', folderName: 'Course', folderPath: 'My Drive/Course' },
};

function row(over: Partial<FolderAttachment> = {}): FolderAttachment {
  return {
    folderAttachId: 'fa-1',
    sessionId: 'sess-1',
    connectorId: 'google-drive',
    connectorName: 'Google Drive for Employees',
    providerFolderId: 'drive-f',
    folderName: 'Course',
    folderPath: 'My Drive/Course',
    status: 'expanding',
    error: null,
    counts: { listed: 0, attached: 0, skippedType: 0, skippedSize: 0, failed: 0, truncated: false },
    files: [],
    snapshotAt: null,
    createdAt: '',
    updatedAt: '',
    stale: false,
    ...over,
  };
}

const READY_ROW = row({
  status: 'ready',
  counts: { listed: 2, attached: 2, skippedType: 0, skippedSize: 0, failed: 0, truncated: false },
  files: [
    { providerFileId: 'd1', name: 'a.pdf', relativePath: 'a.pdf', mimeType: 'application/pdf', sizeBytes: 10, uploadId: 'u1', status: 'attached', error: null },
    { providerFileId: 'd2', name: 'b.md', relativePath: 'w1/b.md', mimeType: 'text/markdown', sizeBytes: 10, uploadId: 'u2', status: 'attached', error: null },
  ],
});

describe('ChatInputComponent — Drive folders', () => {
  let fixture: ComponentFixture<ChatInputComponent>;
  let component: ChatInputComponent;
  let folders: {
    attach: ReturnType<typeof vi.fn>;
    get: ReturnType<typeof vi.fn>;
    remove: ReturnType<typeof vi.fn>;
    resume: ReturnType<typeof vi.fn>;
  };
  let steering: SteeringServiceStub;
  let submitted: { content: string; folders?: { folderAttachId?: string; fileCount: number }[] }[];
  let features: { folderAttachments: boolean; largeDocumentAttachments: boolean; projects: boolean };

  async function build(flag = true, allow = true, projects = false): Promise<void> {
    features = { folderAttachments: flag, largeDocumentAttachments: false, projects };
    folders = {
      attach: vi.fn().mockResolvedValue(row()),
      get: vi.fn().mockResolvedValue(READY_ROW),
      remove: vi.fn().mockResolvedValue(undefined),
      resume: vi.fn().mockResolvedValue(row()),
    };
    await TestBed.configureTestingModule({
      imports: [ChatInputComponent],
      providers: [
        { provide: AgentMentionService, useClass: MentionServiceStub },
        { provide: SkillCommandService, useClass: SkillCommandServiceStub },
        {
          provide: FileUploadService,
          useValue: {
            pendingUploadsList: signal([]),
            hasActivePendingUploads: signal(false),
            readyUploadIds: signal([]),
            readyUploads: computed(() => []),
            clearReadyUploads: () => undefined,
            clearPendingUpload: () => undefined,
            listSessionFiles: async () => [],
            registerKnownFile: () => undefined,
          },
        },
        { provide: FolderAttachmentService, useValue: folders },
        { provide: FEATURES, useValue: features },
        { provide: ToastService, useValue: { error: vi.fn(), warning: vi.fn(), info: vi.fn() } },
        { provide: ToolService, useValue: {} },
        { provide: ModelService, useClass: ModelServiceStub },
        { provide: VoiceChatService, useValue: { status: signal('idle'), isVoiceActive: signal(false), agentTranscript: signal('') } },
        { provide: SystemPromptsService, useValue: { activePrompt: signal(null) } },
        { provide: Router, useValue: { navigate: () => Promise.resolve(true) } },
        { provide: SteeringService, useClass: SteeringServiceStub },
      ],
    })
      .overrideComponent(ChatInputComponent, { set: { imports: [], schemas: [NO_ERRORS_SCHEMA] } })
      .compileComponents();

    fixture = TestBed.createComponent(ChatInputComponent);
    component = fixture.componentInstance;
    steering = TestBed.inject(SteeringService) as unknown as SteeringServiceStub;
    fixture.componentRef.setInput('showFileControls', false);
    fixture.componentRef.setInput('showVoiceControl', false);
    fixture.componentRef.setInput('autoFocus', false);
    fixture.componentRef.setInput('sessionId', 'sess-1');
    fixture.componentRef.setInput('allowFolderAttachments', allow);
    submitted = [];
    component.messageSubmitted.subscribe((m) => submitted.push(m as never));
    fixture.detectChanges();
  }

  afterEach(() => {
    vi.useRealTimers();
    TestBed.resetTestingModule();
    localStorage.clear();
  });

  const cards = (): ComposerFolder[] => component.attachedFolders();

  async function attach(): Promise<void> {
    component.onSourceFolderPicked(PICK);
    await vi.waitFor(() => expect(cards()[0]?.phase).toBe('attached'));
  }

  describe('the "Project or folder" picker beneath the composer', () => {
    const picker = (): HTMLElement | null =>
      fixture.nativeElement.querySelector('app-composer-scope-picker');

    async function rebuild(flag: boolean, projects: boolean, show: boolean, compact = false): Promise<void> {
      TestBed.resetTestingModule();
      await build(flag, true, projects);
      fixture.componentRef.setInput('showScopePicker', show);
      fixture.componentRef.setInput('compact', compact);
      fixture.detectChanges();
    }

    it('sits on the line beneath a new conversation when its page asks for it', async () => {
      await rebuild(true, false, true);
      expect(picker()).not.toBeNull();
    });

    it('shows with projects alone, folders off', async () => {
      await rebuild(false, true, true);
      expect(picker()).not.toBeNull();
    });

    it('hands the picker the folders already on the message, so it offers none twice', async () => {
      await rebuild(true, false, true);
      const keys = () => (component as unknown as { attachedFolderKeys: () => string[] }).attachedFolderKeys();
      expect(keys()).toEqual([]);
      await attach();
      expect(keys()).toEqual(['google-drive:drive-f']);
    });

    it('is not drawn when the page does not ask, in a thread, or with nothing to offer', async () => {
      await rebuild(true, true, false);
      expect(picker()).toBeNull();
      await rebuild(true, true, true, true);
      expect(picker()).toBeNull();
      await rebuild(false, false, true);
      expect(picker()).toBeNull();
    });
  });

  describe('the slot a folder takes', () => {
    it('offers no folder while the flag is off or the parent does not send them', async () => {
      await build(false, true);
      expect(component.remainingFolderSlots()).toBe(0);
      TestBed.resetTestingModule();
      await build(true, false);
      expect(component.remainingFolderSlots()).toBe(0);
    });

    it('offers two, and each takes one of the five attachment slots', async () => {
      await build();
      expect(component.remainingFolderSlots()).toBe(2);
      await attach();
      expect(component.remainingFolderSlots()).toBe(1);
      expect(component.remainingFileSlots()).toBe(4);
    });
  });

  describe('attaching and polling', () => {
    beforeEach(async () => build());

    it('attaches to this conversation with the folder, then polls until settled', async () => {
      vi.useFakeTimers();
      let answer!: (r: FolderAttachment) => void;
      folders.attach.mockReturnValue(new Promise((r) => (answer = r)));
      component.onSourceFolderPicked(PICK);
      // One tick for the parent to mint the conversation id, as for an upload.
      await vi.advanceTimersByTimeAsync(1);
      expect(cards()[0].phase).toBe('attaching');
      expect(component.hasActivePendingUploads()).toBe(true);
      answer(row());
      await vi.advanceTimersByTimeAsync(1);
      expect(folders.attach).toHaveBeenCalledWith('sess-1', {
        connectorId: 'google-drive',
        folderId: 'drive-f',
        folderName: 'Course',
        folderPath: 'My Drive/Course',
      });
      expect(cards()[0]).toMatchObject({ phase: 'attached', folderAttachId: 'fa-1', source: 'Google Drive for Employees' });
      expect(component.hasActivePendingUploads()).toBe(false);

      await vi.advanceTimersByTimeAsync(2_000);
      expect(folders.get).toHaveBeenCalledWith('sess-1', 'fa-1');
      expect(cards()[0].row?.status).toBe('ready');

      // Settled: no more reads.
      await vi.advanceTimersByTimeAsync(60_000);
      expect(folders.get).toHaveBeenCalledTimes(1);
    });

    it('resumes a stale row once', async () => {
      vi.useFakeTimers();
      folders.get.mockResolvedValue(row({ stale: true }));
      component.onSourceFolderPicked(PICK);
      await vi.advanceTimersByTimeAsync(1);
      await vi.advanceTimersByTimeAsync(2_000);
      expect(folders.resume).toHaveBeenCalledTimes(1);
      await vi.advanceTimersByTimeAsync(3_000);
      expect(folders.resume).toHaveBeenCalledTimes(1);
    });

    it('drops a card whose row is gone', async () => {
      vi.useFakeTimers();
      folders.get.mockRejectedValue(new FolderAttachmentError('Folder attachment not found', 404));
      component.onSourceFolderPicked(PICK);
      await vi.advanceTimersByTimeAsync(1);
      await vi.advanceTimersByTimeAsync(2_000);
      expect(cards()).toEqual([]);
    });

    it('keeps a failed attach as an error card that Remove clears without a delete', async () => {
      folders.attach.mockRejectedValue(new FolderAttachmentError('Connect Google Drive to attach this folder.', 409));
      component.onSourceFolderPicked(PICK);
      await vi.waitFor(() => expect(cards()[0]?.phase).toBe('error'));
      expect(cards()[0].error).toBe('Connect Google Drive to attach this folder.');
      component.onFolderRemove(cards()[0].localId);
      expect(cards()).toEqual([]);
      expect(folders.remove).not.toHaveBeenCalled();
    });

    it('Remove deletes the folder and its files', async () => {
      await attach();
      component.onFolderRemove(cards()[0].localId);
      expect(cards()).toEqual([]);
      expect(folders.remove).toHaveBeenCalledWith('sess-1', 'fa-1');
    });

    it('a folder removed while attaching is taken back server-side', async () => {
      let resolve!: (r: FolderAttachment) => void;
      folders.attach.mockReturnValue(new Promise((r) => (resolve = r)));
      component.onSourceFolderPicked(PICK);
      await vi.waitFor(() => expect(cards()).toHaveLength(1));
      component.onFolderRemove(cards()[0].localId);
      resolve(row());
      await vi.waitFor(() => expect(folders.remove).toHaveBeenCalledWith('sess-1', 'fa-1'));
      expect(cards()).toEqual([]);
    });
  });

  describe('sending', () => {
    beforeEach(async () => build());

    it('sends a folder that is still expanding, as one item, without deleting it', async () => {
      await attach();
      expect(component.canSubmit()).toBe(true);
      component.submitChatRequest();
      expect(submitted).toHaveLength(1);
      expect(submitted[0].folders).toEqual([
        expect.objectContaining({ folderAttachId: 'fa-1', name: 'Course', status: 'expanding', fileCount: 0 }),
      ]);
      expect(cards()).toEqual([]);
      expect(folders.remove).not.toHaveBeenCalled();
    });

    it('a queued follow-up carries its folders and is never armed for steering', async () => {
      fixture.componentRef.setInput('isChatLoading', true);
      fixture.detectChanges();
      await attach();
      component.onSubmit();
      await Promise.resolve();
      expect(steering.armCalls).toEqual([]);
      expect(cards()).toEqual([]);

      fixture.componentRef.setInput('isChatLoading', false);
      fixture.detectChanges();
      await vi.waitFor(() => expect(submitted).toHaveLength(1));
      expect(submitted[0].folders?.[0]).toMatchObject({ folderAttachId: 'fa-1' });
    });
  });

  describe('drafts', () => {
    beforeEach(async () => build());

    it('stores the folder and brings it back, re-reading its row', async () => {
      await attach();
      const snapshot = (component as unknown as { composerDraftSnapshot(): { folders?: unknown[] } }).composerDraftSnapshot();
      expect(snapshot.folders).toEqual([{ folderAttachId: 'fa-1', name: 'Course', source: 'Google Drive for Employees' }]);

      folders.get.mockClear();
      (component as unknown as { adoptDraft(d: unknown): void }).adoptDraft({
        text: '',
        folders: snapshot.folders,
        attachmentSessionId: 'sess-1',
      });
      expect(cards()).toEqual([expect.objectContaining({ folderAttachId: 'fa-1', phase: 'attached', row: null })]);
      await vi.waitFor(() => expect(cards()[0].row?.status).toBe('ready'));
      expect(folders.get).toHaveBeenCalledWith('sess-1', 'fa-1');
    });

    it('drops a restored folder the server no longer has', async () => {
      folders.get.mockRejectedValue(new FolderAttachmentError('gone', 404));
      (component as unknown as { adoptDraft(d: unknown): void }).adoptDraft({
        text: '',
        folders: [{ folderAttachId: 'fa-9', name: 'Old', source: 'Drive' }],
        attachmentSessionId: 'sess-1',
      });
      await vi.waitFor(() => expect(cards()).toEqual([]));
    });
  });
});

describe('ChatInputComponent — a refreshed folder and Connect', () => {
  let fixture: ComponentFixture<ChatInputComponent>;
  let component: ChatInputComponent;
  let refresh: { handoff: ReturnType<typeof signal<unknown>>; take: () => void };
  let connectors: { initiateConsent: ReturnType<typeof vi.fn> };
  let consent: { completion: ReturnType<typeof signal<unknown>>; startUserConsent: ReturnType<typeof vi.fn>; acknowledgeCompletion: ReturnType<typeof vi.fn> };
  let folders: { get: ReturnType<typeof vi.fn>; resume: ReturnType<typeof vi.fn>; attach: ReturnType<typeof vi.fn>; remove: ReturnType<typeof vi.fn> };

  beforeEach(async () => {
    const handoff = signal<unknown>(null);
    refresh = { handoff, take: () => handoff.set(null) };
    connectors = { initiateConsent: vi.fn() };
    consent = { completion: signal<unknown>(null), startUserConsent: vi.fn().mockResolvedValue(true), acknowledgeCompletion: vi.fn() };
    folders = {
      get: vi.fn().mockResolvedValue(READY_ROW),
      resume: vi.fn().mockResolvedValue(row()),
      attach: vi.fn(),
      remove: vi.fn(),
    };
    await TestBed.configureTestingModule({
      imports: [ChatInputComponent],
      providers: [
        { provide: AgentMentionService, useClass: MentionServiceStub },
        { provide: SkillCommandService, useClass: SkillCommandServiceStub },
        {
          provide: FileUploadService,
          useValue: {
            pendingUploadsList: signal([]), hasActivePendingUploads: signal(false), readyUploadIds: signal([]),
            readyUploads: computed(() => []), clearReadyUploads: () => undefined, clearPendingUpload: () => undefined,
            listSessionFiles: async () => [], registerKnownFile: () => undefined,
          },
        },
        { provide: FolderAttachmentService, useValue: folders },
        { provide: FolderRefreshService, useValue: refresh },
        { provide: UserConnectorsService, useValue: connectors },
        { provide: OAuthConsentService, useValue: consent },
        { provide: FEATURES, useValue: { folderAttachments: true, largeDocumentAttachments: false } },
        { provide: ToastService, useValue: { error: vi.fn(), warning: vi.fn(), info: vi.fn() } },
        { provide: ToolService, useValue: {} },
        { provide: ModelService, useClass: ModelServiceStub },
        { provide: VoiceChatService, useValue: { status: signal('idle'), isVoiceActive: signal(false), agentTranscript: signal('') } },
        { provide: SystemPromptsService, useValue: { activePrompt: signal(null) } },
        { provide: Router, useValue: { navigate: () => Promise.resolve(true) } },
        { provide: SteeringService, useClass: SteeringServiceStub },
      ],
    })
      .overrideComponent(ChatInputComponent, { set: { imports: [], schemas: [NO_ERRORS_SCHEMA] } })
      .compileComponents();
    fixture = TestBed.createComponent(ChatInputComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('showFileControls', false);
    fixture.componentRef.setInput('showVoiceControl', false);
    fixture.componentRef.setInput('autoFocus', false);
    fixture.componentRef.setInput('sessionId', 'sess-1');
    fixture.componentRef.setInput('allowFolderAttachments', true);
    fixture.detectChanges();
  });

  afterEach(() => {
    TestBed.resetTestingModule();
    localStorage.clear();
  });

  it('adopts a folder refreshed in this conversation as a card, and polls it', async () => {
    refresh.handoff.set({ sessionId: 'sess-1', row: row({ status: 'expanding', refreshing: true }) });
    fixture.detectChanges();
    expect(component.attachedFolders()).toEqual([
      expect.objectContaining({ folderAttachId: 'fa-1', phase: 'attached', name: 'Course' }),
    ]);
    expect(refresh.handoff()).toBeNull();
    await vi.waitFor(() => expect(folders.get).toHaveBeenCalledWith('sess-1', 'fa-1'), { timeout: 4000 });
  });

  it('ignores a refresh from another conversation', () => {
    refresh.handoff.set({ sessionId: 'other', row: row() });
    fixture.detectChanges();
    expect(component.attachedFolders()).toEqual([]);
    expect(refresh.handoff()).not.toBeNull();
  });

  it('Connect runs the consent popup and resumes the folder when it succeeds', async () => {
    refresh.handoff.set({ sessionId: 'sess-1', row: row({ status: 'failed', error: 'needs_consent' }) });
    fixture.detectChanges();
    const card = component.attachedFolders()[0];
    connectors.initiateConsent.mockResolvedValue({ connected: false, authorizationUrl: 'https://auth.example/x' });

    await component.onFolderConnect(card.localId);
    expect(connectors.initiateConsent).toHaveBeenCalledWith('google-drive');
    expect(consent.startUserConsent).toHaveBeenCalledWith('google-drive', 'https://auth.example/x');
    expect(folders.resume).not.toHaveBeenCalled();

    consent.completion.set({ providerId: 'google-drive', status: 'success' });
    fixture.detectChanges();
    await vi.waitFor(() => expect(folders.resume).toHaveBeenCalledWith('sess-1', 'fa-1'));
    expect(consent.acknowledgeCompletion).toHaveBeenCalled();
  });

  it('Connect resumes at once when the vault already has a token', async () => {
    refresh.handoff.set({ sessionId: 'sess-1', row: row({ status: 'failed', error: 'needs_consent' }) });
    fixture.detectChanges();
    connectors.initiateConsent.mockResolvedValue({ connected: true });
    await component.onFolderConnect(component.attachedFolders()[0].localId);
    expect(consent.startUserConsent).not.toHaveBeenCalled();
    expect(folders.resume).toHaveBeenCalledWith('sess-1', 'fa-1');
  });
});
