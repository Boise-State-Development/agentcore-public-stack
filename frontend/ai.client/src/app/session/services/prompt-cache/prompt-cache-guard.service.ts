import { Injectable, inject } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { ManagedModel } from '../../../admin/manage-models/models/managed-model.model';
import {
  CacheResetDialogComponent,
  CacheResetDialogData,
  CacheResetDialogResult,
} from '../../../components/cache-reset-dialog/cache-reset-dialog.component';
import { LocalSettingsService } from '../../../services/local-settings.service';
import { ChatStateService } from '../chat/chat-state.service';
import { SessionService } from '../session/session.service';
import { MessageMapService } from '../session/message-map.service';

/**
 * A change that makes the open conversation's next message miss the prompt
 * cache. Bedrock caches per model, and the effort level is part of the
 * request the cache is keyed on, so either one re-processes the whole
 * conversation at the cache-write rate (1.25x input) instead of reading it at
 * the cache-read rate (0.1x).
 */
export type CacheResetChange =
  | { kind: 'model'; from: ManagedModel; to: ManagedModel }
  | { kind: 'effort'; model: ManagedModel; fromLabel: string; toLabel: string; toLevel: string };

/** What the conversation's cache was last written with: the model and effort of its last message. */
interface CachedWith {
  modelId: string;
  /** Null when unknown (a conversation opened from the list, not sent from this tab). */
  effort: string | null;
}

/**
 * How long a conversation's history stays cached after its last model call.
 * Bedrock's default TTL; the tools and system prompt may be held for an hour,
 * but they are a small share of a conversation worth warning about. Past this
 * the next message re-writes the history whether or not anything changed, so
 * a switch costs nothing extra and there is nothing to warn about.
 */
export const CACHE_WARM_MS = 5 * 60 * 1000;

/** Below this the extra cost is not worth interrupting anyone for. */
export const MIN_WARN_USD = 0.01;

/** Fallback multipliers on the input rate when a catalog row has no cache prices. */
const CACHE_WRITE_MULTIPLIER = 1.25;
const CACHE_READ_MULTIPLIER = 0.1;

/** What the next message costs to re-process `tokens` of conversation on `model`, per token. */
function writeRate(model: ManagedModel): number {
  if (model.cacheWritePricePerMillionTokens != null) return model.cacheWritePricePerMillionTokens;
  return model.supportsCaching ? model.inputPricePerMillionTokens * CACHE_WRITE_MULTIPLIER : model.inputPricePerMillionTokens;
}

function readRate(model: ManagedModel): number {
  return model.cacheReadPricePerMillionTokens ?? model.inputPricePerMillionTokens * CACHE_READ_MULTIPLIER;
}

/**
 * Extra dollars the change adds to the next message, against reading the same
 * conversation from cache on the current model and effort. `null` when the
 * conversation's size is unknown.
 */
export function estimateCacheResetUsd(change: CacheResetChange, contextTokens: number): number | null {
  if (contextTokens <= 0) return null;
  const [from, to] = change.kind === 'model' ? [change.from, change.to] : [change.model, change.model];
  return (contextTokens * (writeRate(to) - readRate(from))) / 1_000_000;
}

function formatTokens(tokens: number): string {
  return tokens >= 1000 ? `${Math.round(tokens / 1000)}K` : String(tokens);
}

function costClause(tokens: number, usd: number | null): string {
  if (usd === null) return 'which uses more of your limit';
  return `about ${formatTokens(tokens)} tokens, which uses more of your limit (roughly $${usd.toFixed(2)} more)`;
}

function dialogData(change: CacheResetChange, tokens: number, usd: number | null): CacheResetDialogData {
  const cost = costClause(tokens, usd);
  if (change.kind === 'model') {
    return {
      title: 'Switch model?',
      message:
        `This conversation is cached for ${change.from.modelName}. Switching to ${change.to.modelName} ` +
        `means your next message processes the whole conversation again, ${cost}.`,
      confirmText: 'Switch model',
    };
  }
  return {
    title: 'Change effort?',
    message:
      `This conversation is cached at ${change.fromLabel} effort. Changing to ${change.toLabel} ` +
      `means your next message processes the whole conversation again, ${cost}.`,
    confirmText: 'Change effort',
  };
}

/**
 * Asks before a change that throws away the open conversation's prompt cache,
 * and only when doing so actually costs something: the conversation has
 * messages, its cache is still warm, and the re-write is worth more than a
 * cent. Everything else goes through without a prompt.
 */
@Injectable({ providedIn: 'root' })
export class PromptCacheGuardService {
  private readonly dialog = inject(Dialog);
  private readonly chatState = inject(ChatStateService);
  private readonly sessionService = inject(SessionService);
  private readonly messageMap = inject(MessageMapService);
  private readonly localSettings = inject(LocalSettingsService);

  /** Per session, the model and effort the last message in this tab was sent with. */
  private readonly sentWith = new Map<string, CachedWith>();

  /**
   * Record what a message is being sent with. Called as each chat request is
   * built, so the guard knows what the cache holds rather than what the
   * picker shows: a switch the user has picked but not sent yet has not
   * touched the cache.
   */
  recordSend(sessionId: string, modelId: string, effort: string | null): void {
    this.sentWith.set(sessionId, { modelId, effort });
  }

  /** Resolves true when the change should go ahead. */
  async confirm(change: CacheResetChange): Promise<boolean> {
    if (!this.localSettings.confirmCacheReset()) return true;
    const cacheModel = change.kind === 'model' ? change.from : change.model;
    if (!cacheModel.supportsCaching) return true;

    const sessionId = this.sessionService.currentSession().sessionId;
    if (!sessionId || !this.isCacheWarm(sessionId)) return true;
    if (!this.changesCachedPrefix(sessionId, change)) return true;

    const tokens = this.chatState.contextTokensFor(sessionId);
    const usd = estimateCacheResetUsd(change, tokens);
    if (usd !== null && usd < MIN_WARN_USD) return true;

    const ref = this.dialog.open<CacheResetDialogResult>(CacheResetDialogComponent, {
      data: dialogData(change, tokens, usd),
    });
    const result = await firstValueFrom(ref.closed);
    if (!result) return false;
    if (result.dontAskAgain) this.localSettings.setConfirmCacheReset(false);
    return true;
  }

  /**
   * Whether the change moves the next message away from what the cache was
   * written with. Not when the selection already differs from it, because a
   * switch picked but not sent yet already pays the re-write, and changing
   * effort on top of it (or picking a third model) adds nothing. Not when it
   * goes back to what the cache holds, because that is a cache hit.
   */
  private changesCachedPrefix(sessionId: string, change: CacheResetChange): boolean {
    const cached = this.cachedWith(sessionId);
    if (!cached) return true;
    if (change.kind === 'model') {
      return change.from.modelId === cached.modelId && change.to.modelId !== cached.modelId;
    }
    if (change.model.modelId !== cached.modelId) return false;
    return cached.effort === null || change.toLevel !== cached.effort;
  }

  private cachedWith(sessionId: string): CachedWith | null {
    const sent = this.sentWith.get(sessionId);
    if (sent) return sent;
    const lastModel = this.sessionService.currentSession().preferences?.lastModel;
    return lastModel ? { modelId: lastModel, effort: null } : null;
  }

  /**
   * Whether the conversation's last model call was recent enough that its
   * cache is still live. A conversation with no messages has no cache; one
   * streaming right now is warm by definition.
   */
  private isCacheWarm(sessionId: string): boolean {
    const messages = this.messageMap.getMessagesForSession(sessionId)();
    if (messages.length === 0) return false;
    if (this.chatState.isSessionLoading(sessionId)) return true;

    const createdAt = Date.parse(messages[messages.length - 1].createdAt ?? '');
    const turnEnded = this.chatState.lastTurnEndedAt(sessionId);
    const lastActivity = Math.max(Number.isNaN(createdAt) ? 0 : createdAt, turnEnded ?? 0);
    // No timestamp at all: assume warm, since asking is the safe side.
    if (lastActivity === 0) return true;
    return Date.now() - lastActivity < CACHE_WARM_MS;
  }
}
