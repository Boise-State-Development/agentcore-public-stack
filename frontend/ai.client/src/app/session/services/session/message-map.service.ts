// services/message-map.service.ts (updated integration)
import { EffectRef, Injectable, Injector, Signal, WritableSignal, signal, effect, inject } from '@angular/core';
import { ContentBlock, FileAttachmentData, Message } from '../models/message.model';
import { StreamParserService } from '../chat/stream-parser.service';
import { PendingInterrupt, SessionService } from './session.service';
import { FileUploadService, FileMetadata } from '../../../services/file-upload';
import { OAuthConsentService } from '../../../services/oauth-consent/oauth-consent.service';
import { ToolApprovalService } from '../../../services/tool-approval/tool-approval.service';
import { McpAppStateService } from '../mcp-apps/mcp-app-state.service';
import { normalizeSteeringMessages } from '../chat/steering';

/** Regex to match file attachment marker in message text: [Attached files: file1.pdf, file2.png] */
const ATTACHED_FILES_PATTERN = /\n\n\[Attached files: ([^\]]+)\]$/;

interface MessageMap {
  [sessionId: string]: WritableSignal<Message[]>;
}

@Injectable({
  providedIn: 'root'
})
export class MessageMapService {
  private messageMap = signal<MessageMap>({});

  /**
   * Sessions with an in-flight stream. Multiple conversations can stream
   * concurrently — each one syncs its own parser state into its own map
   * entry via a dedicated effect (see {@link createSyncEffect}).
   */
  private activeStreamSessions = signal<ReadonlySet<string>>(new Set());

  /** One live sync effect per streaming session, torn down on endStreaming. */
  private syncEffects = new Map<string, EffectRef>();

  /**
   * Continuation-after-max_tokens state, per session. A "Continue" turn has
   * NO new user message, so the normal sync (which truncates back to the
   * last user message and replaces everything after it) would discard the
   * truncated partial + error bubbles and show only the continuation. In
   * continuation mode we instead pin a stable prefix (the messages that
   * existed when the continuation started) and append the continuation
   * stream after it.
   */
  private continuationPrefixes = new Map<string, Message[]>();

  /**
   * Tracks which session is currently loading messages from the API.
   * Used to show skeleton loading state when navigating to a session.
   */
  private _isLoadingSession = signal<string | null>(null);

  /**
   * Public readonly signal indicating the session ID currently loading,
   * or null if no session is loading.
   */
  readonly isLoadingSession = this._isLoadingSession.asReadonly();

  /**
   * Set the loading session state.
   * Call this before loadMessagesForSession to show skeleton immediately on route change.
   */
  setLoadingSession(sessionId: string | null): void {
    this._isLoadingSession.set(sessionId);
  }

  private streamParser = inject(StreamParserService);
  private sessionService = inject(SessionService);
  private fileUploadService = inject(FileUploadService);
  private oauthConsentService = inject(OAuthConsentService);
  private toolApprovalService = inject(ToolApprovalService);
  private mcpAppState = inject(McpAppStateService);
  private injector = inject(Injector);

  /**
   * Start streaming for a session.
   * Call this before beginning to parse SSE events.
   */
  startStreaming(sessionId: string): void {
    // A normal turn uses the standard (truncate-to-last-user) sync.
    this.continuationPrefixes.delete(sessionId);

    // Get current message count for this session to enable predictable ID generation
    const currentMessages = this.messageMap()[sessionId]?.() ?? [];
    const messageCount = currentMessages.length;

    // Reset stream parser with session context for predictable message IDs
    this.streamParser.reset(sessionId, messageCount);

    this.beginStreamTracking(sessionId);
  }

  /**
   * Start a "Continue" stream after a max_tokens truncation. Unlike
   * {@link startStreaming}, the messages that already exist (including the
   * truncated partial and the error bubble) are pinned as a stable prefix
   * and the continuation stream is appended after them — nothing is
   * truncated away. The parser is reset with the current message count so
   * the continuation's message IDs follow the prefix instead of colliding.
   */
  beginContinuationStreaming(sessionId: string): void {
    const currentMessages = this.messageMap()[sessionId]?.() ?? [];
    this.continuationPrefixes.set(sessionId, [...currentMessages]);

    this.streamParser.reset(sessionId, currentMessages.length);

    this.beginStreamTracking(sessionId);
  }

  /**
   * End streaming for a session.
   * Finalizes its messages and clears its streaming state. No-op when the
   * session has no active stream (e.g. a superseded stream's late cleanup),
   * so it can never tear down a newer stream for the same session.
   */
  endStreaming(sessionId: string): void {
    if (!this.activeStreamSessions().has(sessionId)) {
      return;
    }

    // Ensure final messages are synced (continuation merge still active
    // here so the final flush keeps the pinned prefix).
    const finalMessages = this.streamParser.allMessagesFor(sessionId)();
    if (finalMessages.length > 0) {
      this.syncStreamingMessages(sessionId, finalMessages);
    }

    this.syncEffects.get(sessionId)?.destroy();
    this.syncEffects.delete(sessionId);
    this.continuationPrefixes.delete(sessionId);
    this.activeStreamSessions.update(set => {
      const next = new Set(set);
      next.delete(sessionId);
      return next;
    });
  }

  /** Whether a session currently has an in-flight stream. */
  isStreaming(sessionId: string): boolean {
    return this.activeStreamSessions().has(sessionId);
  }

  /**
   * Mark a session as streaming, ensure its map entry exists, and attach
   * its live parser→map sync effect. Idempotent per session.
   */
  private beginStreamTracking(sessionId: string): void {
    this.activeStreamSessions.update(set => {
      if (set.has(sessionId)) return set;
      const next = new Set(set);
      next.add(sessionId);
      return next;
    });

    // Ensure the session exists in the map
    if (!this.messageMap()[sessionId]) {
      this.messageMap.update(map => ({
        ...map,
        [sessionId]: signal<Message[]>([])
      }));
    }

    // Reactive per-session effect: sync this stream's messages into this
    // session's map entry. Each concurrent stream gets its own effect, so
    // conversation A keeps updating while the user views conversation B.
    if (!this.syncEffects.has(sessionId)) {
      const ref = effect(() => {
        const streamMessages = this.streamParser.allMessagesFor(sessionId)();
        if (streamMessages.length > 0) {
          this.syncStreamingMessages(sessionId, streamMessages);
        }
      }, { injector: this.injector });
      this.syncEffects.set(sessionId, ref);
    }
  }

  /**
   * Get the messages signal for a session.
   */
  getMessagesForSession(sessionId: string): Signal<Message[]> {
    const existing = this.messageMap()[sessionId];
    if (existing) {
      return existing;
    }

    const newSignal = signal<Message[]>([]);
    this.messageMap.update(map => ({
      ...map,
      [sessionId]: newSignal
    }));

    return newSignal;
  }

  /**
   * Add a user message to a session (before streaming begins).
   * Generates a predictable message ID based on session ID and message count.
   *
   * @param sessionId - The session ID to add the message to
   * @param content - The text content of the message
   * @param fileAttachments - Optional array of file attachment metadata
   */
  addUserMessage(sessionId: string, content: string, fileAttachments?: FileAttachmentData[]): Message {
    // Get current message count for this session to compute ID
    const currentMessages = this.messageMap()[sessionId]?.() ?? [];
    const messageIndex = currentMessages.length;

    // Compute predictable message ID: msg-{sessionId}-{index}
    const messageId = `msg-${sessionId}-${messageIndex}`;

    // Build content blocks - file attachments first, then text
    const contentBlocks: ContentBlock[] = [];

    // Add file attachment blocks
    if (fileAttachments && fileAttachments.length > 0) {
      for (const attachment of fileAttachments) {
        contentBlocks.push({
          type: 'fileAttachment',
          fileAttachment: attachment
        });
      }
    }

    // Add text content block
    contentBlocks.push({ type: 'text', text: content });

    const message: Message = {
      id: messageId,
      role: 'user',
      content: contentBlocks
    };

    this.messageMap.update(map => {
      const updated = { ...map };

      if (!updated[sessionId]) {
        updated[sessionId] = signal([message]);
      } else {
        updated[sessionId].update(msgs => [...msgs, message]);
      }

      return updated;
    });

    return message;
  }

  /**
   * Add a voice transcript message (from VoiceChatService) to the session.
   * Used when the voice agent completes a response and the transcript is finalized.
   *
   * @param sessionId - The session ID to add the message to
   * @param role - Message role (user or assistant)
   * @param text - Message text content
   * @param metadata - Optional metadata (token usage, cost) for badge display
   */
  addVoiceMessage(sessionId: string, role: 'user' | 'assistant', text: string, metadata?: Record<string, unknown>): Message {
    const currentMessages = this.messageMap()[sessionId]?.() ?? [];
    const messageIndex = currentMessages.length;
    const messageId = `msg-${sessionId}-${messageIndex}`;

    const message: Message = {
      id: messageId,
      role,
      content: [{ type: 'text', text }],
      ...(metadata ? { metadata } : {}),
    };

    this.messageMap.update(map => {
      const updated = { ...map };
      if (!updated[sessionId]) {
        updated[sessionId] = signal([message]);
      } else {
        updated[sessionId].update(msgs => [...msgs, message]);
      }
      return updated;
    });

    return message;
  }

  /**
   * Sync streaming messages to the message map.
   * Handles the case where we're appending to existing messages.
   * Preserves all previous complete messages and only replaces the currently streaming assistant response.
   */
  private syncStreamingMessages(sessionId: string, streamMessages: Message[]): void {
    const sessionSignal = this.messageMap()[sessionId];
    if (!sessionSignal) return;

    sessionSignal.update(existingMessages => {
      // Continuation-after-max_tokens: there is no new user message to
      // truncate back to. Pin the prefix captured when the continuation
      // started (history + truncated partial + error bubble) and append the
      // continuation stream after it. Rebuilt from the stable prefix every
      // tick so repeated syncs stay idempotent.
      const continuationPrefix = this.continuationPrefixes.get(sessionId);
      if (continuationPrefix) {
        return [...continuationPrefix, ...streamMessages];
      }

      // Find the index of the last TURN-STARTING user message.
      //
      // A mid-turn steer is a user message that does not start a turn — it is
      // part of the response already streaming, and the parser is still
      // emitting it in `streamMessages` every tick. Treating it as the
      // truncation point moves that point forward past itself, so the stream's
      // copy is appended again on the next tick, and the next, and the next:
      // one bubble per sync tick (observed live in dev as ~36 copies of one
      // follow-up). Skipping it keeps the truncation anchored on the user
      // message that actually opened the turn, which is what makes repeated
      // syncs idempotent. Same predicate turn grouping uses.
      let lastUserMessageIndex = -1;
      for (let i = existingMessages.length - 1; i >= 0; i--) {
        if (existingMessages[i].role === 'user' && !existingMessages[i].steering) {
          lastUserMessageIndex = i;
          break;
        }
      }

      // If no user message found, just return the stream messages
      if (lastUserMessageIndex === -1) {
        return streamMessages;
      }

      // Keep all messages up to and including the last user message
      // Then append the streaming messages (replacing any partial assistant messages after the last user message)
      const preservedMessages = existingMessages.slice(0, lastUserMessageIndex + 1);
      return [...preservedMessages, ...streamMessages];
    });
  }

  /**
   * Load messages for a session from the API.
   * If messages already exist in the map, they won't be reloaded.
   *
   * @param sessionId - The session ID to load messages for
   * @returns Promise that resolves when messages are loaded
   */
  async loadMessagesForSession(sessionId: string): Promise<void> {
    // Check if messages already exist
    const existingMessages = this.messageMap()[sessionId];
    if (existingMessages && existingMessages().length > 0) {
      // Messages already loaded, skip API call
      return;
    }

    try {
      await this.fetchAndApplyMessages(sessionId, /* showLoading */ true);
    } catch (error) {
      console.error('Failed to load messages for session:', sessionId, error);
      throw error;
    }
  }

  /**
   * Force a re-fetch of a session's messages from the server, replacing the
   * in-memory copy with the authoritative persisted state.
   *
   * Unlike {@link loadMessagesForSession} this bypasses the
   * "already loaded" guard and does NOT flip the skeleton loading state, so
   * it can reconcile a live thread without a visible flash. It is used after
   * an interrupt resume (OAuth consent / tool approval): the resumed stream
   * carries only the `tool_result` + the final assistant text — Strands does
   * not replay the interrupted `tool_use` block — so the live parser can't
   * attach the result to the paused tool card. Re-reading persisted memory
   * (where the backend stored both the `tool_use` and its `tool_result`)
   * flips the card from "Running…" to its completed result.
   *
   * Best-effort: a failure leaves the live-streamed state untouched rather
   * than disrupting the UI.
   */
  async reloadMessagesForSession(sessionId: string): Promise<void> {
    try {
      await this.fetchAndApplyMessages(sessionId, /* showLoading */ false);
    } catch (error) {
      console.error('Failed to reload messages for session:', sessionId, error);
    }
  }

  /**
   * Fetch a session's messages + file metadata, reconstruct tool results and
   * file attachments, and replace the message map entry. Shared by the
   * initial load and the post-resume reconcile.
   */
  private async fetchAndApplyMessages(sessionId: string, showLoading: boolean): Promise<void> {
    if (showLoading) {
      this._isLoadingSession.set(sessionId);
    }

    try {
      // Fetch messages and file metadata in parallel
      const [messagesResponse, sessionFiles] = await Promise.all([
        this.sessionService.getMessages(sessionId),
        this.fetchSessionFiles(sessionId)
      ]);

      // Create a lookup map for files by filename
      const filesByName = new Map<string, FileMetadata>();
      for (const file of sessionFiles) {
        filesByName.set(file.filename, file);
      }

      // Process messages to match tool results and restore file attachments
      let processedMessages = this.matchToolResultsToToolUses(messagesResponse.messages);
      // Mid-turn steering rides inside the tool-result message, so a reload
      // hands back a user message of `[toolResult…, text]` whose text is still
      // wrapped in the tags the model reads. Unwrap it, drop the results
      // (already folded into their toolUse above) and mark it as steering so
      // turn grouping doesn't split the response it interrupted. Runs after
      // the fold and before file restoration, both of which key on shapes it
      // leaves intact. See docs/specs/mid-turn-steering.md.
      processedMessages = normalizeSteeringMessages(processedMessages);
      processedMessages = this.restoreFileAttachments(processedMessages, filesByName);

      // Update the message map with loaded messages
      this.messageMap.update(map => {
        const updated = { ...map };

        if (!updated[sessionId]) {
          updated[sessionId] = signal(processedMessages);
        } else {
          updated[sessionId].set(processedMessages);
        }

        return updated;
      });

      // Hydrate OAuth consent service from any persisted pending interrupts
      // so a reload restores the consent prompt anchored to the right turn.
      // Anchor falls back to the most recent assistant message in history,
      // matching how the live SSE flow already attaches prompts. Authorization
      // URL is intentionally omitted — fresh URL is fetched lazily on Connect.
      this.hydratePendingInterrupts(sessionId, messagesResponse.pendingInterrupts, processedMessages);

      // MCP Apps (SEP-1865): re-seed the App registry from the persisted
      // resources the backend replays on this response. The inline
      // `ui_resource` event never re-streams, so without this the
      // `mcp-app-frame` falls back to a plain tool card after a refresh.
      // session.page resets McpAppStateService before this load, so the
      // non-clobbering seed lands cleanly.
      this.mcpAppState.seedFromHydration(messagesResponse.uiResources ?? []);
    } finally {
      if (showLoading) {
        this._isLoadingSession.set(null);
      }
    }
  }

  /**
   * Replay persisted pending interrupts into the matching service so the
   * inline prompt re-renders. Branches on `kind` (defaults to "oauth" for
   * legacy rows written before per-tool approval shipped). If the backend
   * recorded a triggering message id we use it directly; otherwise we
   * anchor to the most recent assistant message, mirroring the live-stream
   * behavior in stream-parser.service.ts.
   */
  private hydratePendingInterrupts(
    sessionId: string,
    interrupts: PendingInterrupt[] | undefined,
    messages: Message[],
  ): void {
    if (!interrupts || interrupts.length === 0) {
      return;
    }
    let lastAssistantId: string | undefined;
    for (let i = messages.length - 1; i >= 0; i--) {
      if (messages[i].role === 'assistant') {
        lastAssistantId = messages[i].id;
        break;
      }
    }
    for (const interrupt of interrupts) {
      const messageId = interrupt.triggeringMessageId ?? lastAssistantId;
      const kind = interrupt.kind ?? 'oauth';

      if (kind === 'tool_approval') {
        if (!interrupt.toolName) {
          console.warn(
            'Skipping tool_approval interrupt with no toolName',
            interrupt.interruptId,
          );
          continue;
        }
        this.toolApprovalService.requestApproval({
          interruptId: interrupt.interruptId,
          toolUseId: interrupt.toolUseId ?? '',
          toolName: interrupt.toolName,
          toolInput: interrupt.toolInput ?? undefined,
          message: interrupt.message ?? '',
          messageId,
          sessionId,
        });
        continue;
      }

      // OAuth (default for legacy rows without `kind`)
      if (!interrupt.providerId) {
        console.warn(
          'Skipping oauth interrupt with no providerId',
          interrupt.interruptId,
        );
        continue;
      }
      this.oauthConsentService.requestConsent(
        interrupt.providerId,
        undefined, // URL is fetched lazily on Connect — stored URLs go stale
        interrupt.interruptId,
        messageId,
        sessionId,
      );
    }
  }

  /**
   * Fetch file metadata for a session from the backend.
   * Returns empty array on error to allow graceful degradation.
   */
  private async fetchSessionFiles(sessionId: string): Promise<FileMetadata[]> {
    try {
      return await this.fileUploadService.listSessionFiles(sessionId);
    } catch (error) {
      console.warn('Failed to fetch session files, file attachments will not be displayed:', error);
      return [];
    }
  }

  /**
   * Restore file attachment blocks in user messages.
   * Parses the [Attached files: ...] marker in text and creates fileAttachment blocks.
   */
  private restoreFileAttachments(
    messages: Message[],
    filesByName: Map<string, FileMetadata>
  ): Message[] {
    return messages.map(message => {
      if (message.role !== 'user') {
        return message;
      }

      // Process each content block
      const newContent: ContentBlock[] = [];
      const fileAttachmentBlocks: ContentBlock[] = [];

      for (const block of message.content) {
        if (block.type === 'text' && block.text) {
          // Check for attached files marker
          const match = block.text.match(ATTACHED_FILES_PATTERN);
          if (match) {
            // Extract filenames and create file attachment blocks
            const fileNames = match[1].split(', ').map(name => name.trim());
            for (const filename of fileNames) {
              const fileMeta = filesByName.get(filename);
              if (fileMeta) {
                fileAttachmentBlocks.push({
                  type: 'fileAttachment',
                  fileAttachment: {
                    uploadId: fileMeta.uploadId,
                    filename: fileMeta.filename,
                    mimeType: fileMeta.mimeType,
                    sizeBytes: fileMeta.sizeBytes
                  }
                });
              }
            }

            // Remove the marker from the text
            const cleanText = block.text.replace(ATTACHED_FILES_PATTERN, '');
            if (cleanText.trim()) {
              newContent.push({ type: 'text', text: cleanText });
            }
          } else {
            newContent.push(block);
          }
        } else {
          newContent.push(block);
        }
      }

      // Return message with file attachments first, then other content
      if (fileAttachmentBlocks.length > 0) {
        return {
          ...message,
          content: [...fileAttachmentBlocks, ...newContent]
        };
      }

      return message;
    });
  }

  /**
   * Match tool results from user messages to their corresponding tool uses in assistant messages.
   * This reconstructs the tool state as it appears during streaming.
   *
   * The backend stores tool_use and tool_result as separate content blocks in separate messages:
   * - Assistant message: contains toolUse blocks
   * - User message: contains toolResult blocks
   *
   * This method merges the results into the toolUse blocks for proper UI display.
   *
   * @param messages - Array of messages from the API
   * @returns Processed messages with tool results embedded in tool uses
   */
  private matchToolResultsToToolUses(messages: Message[]): Message[] {
    // Create a map of toolUseId -> tool result for quick lookup
    const toolResultMap = new Map<string, { content: any[]; status: 'success' | 'error' }>();

    // First pass: collect all tool results from user messages
    // Note: Tool results are now embedded in toolUse.result, but we still check
    // for legacy toolResult blocks for backwards compatibility
    for (const message of messages) {
      if (message.role === 'user') {
        for (const contentBlock of message.content) {
          // Check for legacy toolResult blocks (deprecated format)
          if (contentBlock.type === 'toolResult' && contentBlock.toolResult) {
            const toolResult = contentBlock.toolResult as any;
            const toolUseId = toolResult.toolUseId;
            if (toolUseId) {
              // Determine status from explicit status field or by inspecting content
              let status: 'success' | 'error' = toolResult.status || 'success';

              // Check if content indicates an error (for backwards compatibility with old format)
              if (status === 'success' && toolResult.content && Array.isArray(toolResult.content)) {
                for (const contentItem of toolResult.content) {
                  // Check if JSON content has success: false
                  if (contentItem.json && typeof contentItem.json === 'object') {
                    if (contentItem.json.success === false || contentItem.json.error) {
                      status = 'error';
                      break;
                    }
                  }
                  // Check if text content indicates error
                  if (contentItem.text && typeof contentItem.text === 'string') {
                    try {
                      const parsed = JSON.parse(contentItem.text);
                      if (parsed.success === false || parsed.error) {
                        status = 'error';
                        break;
                      }
                    } catch {
                      // Not JSON, ignore
                    }
                  }
                }
              }

              toolResultMap.set(toolUseId, {
                content: toolResult.content || [],
                status: status
              });
            }
          }
        }
      }
    }

    // Second pass: match results to tool uses
    return messages.map(message => {
      if (message.role === 'assistant') {
        // Process content blocks to add results to tool uses
        const updatedContent = message.content.map(contentBlock => {
          if ((contentBlock.type === 'toolUse' || contentBlock.type === 'tool_use') && contentBlock.toolUse) {
            const toolUse = contentBlock.toolUse as any;
            const toolUseId = toolUse.toolUseId;

            // Check if we have a result for this tool use
            if (toolUseId && toolResultMap.has(toolUseId)) {
              const result = toolResultMap.get(toolUseId)!;

              // Create updated toolUse with embedded result
              return {
                ...contentBlock,
                toolUse: {
                  ...toolUse,
                  result: result,
                  status: result.status === 'error' ? 'error' : 'complete'
                }
              };
            }
          }
          return contentBlock;
        });

        return {
          ...message,
          content: updatedContent
        };
      }
      return message;
    });
  }

  /**
   * Clear all data for a session, including any in-flight stream sync and
   * its parser state.
   */
  clearSession(sessionId: string): void {
    this.endStreaming(sessionId);
    this.streamParser.clearSession(sessionId);
    this.messageMap.update(map => {
      const { [sessionId]: _, ...rest } = map;
      return rest;
    });
  }

}
