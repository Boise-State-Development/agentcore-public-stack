import { Injectable, inject } from '@angular/core';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { ManagedModel, resolveEffortControl } from '../../../admin/manage-models/models/managed-model.model';
import {
  CacheResetDialogComponent,
  CacheResetDialogData,
  CacheResetDialogResult,
} from '../../../components/cache-reset-dialog/cache-reset-dialog.component';
import { LocalSettingsService } from '../../../services/local-settings.service';
import { ChatStateService } from '../chat/chat-state.service';
import { ModelService } from '../model/model.service';
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

/**
 * A prompt-cache entry this conversation wrote: the model and effort of a
 * model call, and when it was last touched. Bedrock keeps one entry per model
 * and effort, so an entry outlives a switch away from it: a conversation that
 * goes Haiku → GPT → Haiku inside Haiku's lifetime reads Haiku's entry back.
 */
interface CacheEntry {
  modelId: string;
  /**
   * The effort it was sent with (null: none). Undefined when not known: the
   * history the server returns records each reply's model but not its effort.
   */
  effort?: string | null;
  /**
   * Epoch ms of the last call on it. Null for the conversation's latest
   * entry, which is timed from the conversation's own last activity (and is
   * warm while a turn streams).
   */
  at: number | null;
}

/** What the next message would be sent with: a model and its effort level. */
interface Selection {
  model: ManagedModel;
  effort: string | null;
}

/**
 * How long a conversation's history stays cached after its last model call,
 * mirroring the backend's `cache_ttl_seconds_for`
 * (`apis/shared/observability/prompt_cache.py`), which the cost classifier
 * measures every call against. Past this the next message re-writes the
 * history whether or not anything changed, so a switch costs nothing extra and
 * there is nothing to warn about.
 *
 * Bedrock's default is about 5 minutes. A deployment can hold the tools and
 * system prompt for an hour, but they are a small share of a conversation
 * worth warning about.
 */
export const DEFAULT_CACHE_TTL_MS = 5 * 60 * 1000;

/**
 * The OpenAI Responses API on bedrock-runtime keeps entries for 30 minutes.
 * Only the `bedrock-responses` provider, not the whole OpenAI family: `mantle`
 * serves `openai.gpt-5.4` with implicit caching whose retention AWS does not
 * document, and the backend deliberately does not guess there either.
 */
export const RESPONSES_CACHE_TTL_MS = 30 * 60 * 1000;

const RESPONSES_TTL_PROVIDERS: ReadonlySet<ManagedModel['provider']> = new Set(['bedrock-responses']);

/**
 * The cache lifetime for the model a conversation's cache was written with.
 * The backend's model-id fallback is only for rows written before the
 * provider was recorded; a catalog model always carries its provider.
 */
export function cacheTtlMsFor(model: Pick<ManagedModel, 'provider'>): number {
  return RESPONSES_TTL_PROVIDERS.has(model.provider)
    ? RESPONSES_CACHE_TTL_MS
    : DEFAULT_CACHE_TTL_MS;
}

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
 * messages, what the next message would be sent with is still cached, what it
 * would be sent with after the change is not, and the re-write is worth more
 * than a cent. Everything else goes through without a prompt.
 */
@Injectable({ providedIn: 'root' })
export class PromptCacheGuardService {
  private readonly dialog = inject(Dialog);
  private readonly chatState = inject(ChatStateService);
  private readonly modelService = inject(ModelService);
  private readonly sessionService = inject(SessionService);
  private readonly messageMap = inject(MessageMapService);
  private readonly localSettings = inject(LocalSettingsService);

  /** Per session, the entries messages sent from this tab wrote, oldest first. */
  private readonly sent = new Map<string, CacheEntry[]>();

  /**
   * Record what a message is being sent with. Called as each chat request is
   * built, so the guard knows what the cache holds rather than what the
   * picker shows: a switch the user has picked but not sent yet has not
   * touched the cache.
   */
  recordSend(sessionId: string, modelId: string, effort: string | null): void {
    const entries = this.sent.get(sessionId) ?? [];
    // A send only follows a finished turn, so the previous entry was last
    // touched when that turn ended.
    const previous = entries[entries.length - 1];
    if (previous) previous.at = this.chatState.lastTurnEndedAt(sessionId) ?? Date.now();
    const next = entries.filter(e => e.modelId !== modelId || e.effort !== effort);
    next.push({ modelId, effort, at: null });
    this.sent.set(sessionId, next);
  }

  /** Resolves true when the change should go ahead. */
  async confirm(change: CacheResetChange): Promise<boolean> {
    if (!this.localSettings.confirmCacheReset()) return true;
    const cacheModel = change.kind === 'model' ? change.from : change.model;
    if (!cacheModel.supportsCaching) return true;

    const sessionId = this.sessionService.currentSession().sessionId;
    if (!sessionId) return true;
    if (this.messageMap.getMessagesForSession(sessionId)().length === 0) return true;

    const from: Selection = { model: cacheModel, effort: this.modelService.selectedEffort() };
    const to: Selection =
      change.kind === 'model'
        ? { model: change.to, effort: this.modelService.effortFor(change.to) }
        : { model: change.model, effort: change.toLevel };
    // Not when the next message already misses (the cache expired, or a
    // switch picked but not sent already pays the re-write), and not when the
    // change lands on an entry the conversation still has, because that is a
    // cache hit. Unknowns lean towards asking on both sides.
    const entries = this.cacheEntries(sessionId, cacheModel);
    if (!this.isWarm(sessionId, entries, from, true)) return true;
    if (this.isWarm(sessionId, entries, to, false)) return true;

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
   * The cache entries this conversation may still hold, latest last. Sends
   * from this tab know their effort. A conversation opened from the list has
   * only its history: each reply's `modelInfo`, else the session's
   * `lastModel`, else nothing, and then the selected model is assumed to have
   * written the cache, since asking is the safe side.
   */
  private cacheEntries(sessionId: string, selected: ManagedModel): CacheEntry[] {
    const history: CacheEntry[] = [];
    for (const message of this.messageMap.getMessagesForSession(sessionId)()) {
      if (message.role !== 'assistant') continue;
      const modelId = (message.metadata?.['modelInfo'] as { modelId?: unknown } | undefined)?.modelId;
      const at = Date.parse(message.createdAt ?? '');
      if (typeof modelId === 'string' && !Number.isNaN(at)) history.push({ modelId, at });
    }
    history.sort((a, b) => (a.at ?? 0) - (b.at ?? 0));

    const sent = this.sent.get(sessionId) ?? [];
    if (sent.length > 0) return [...history, ...sent];
    const latest = history.pop();
    if (latest) return [...history, { modelId: latest.modelId, at: null }];
    const lastModel = this.sessionService.currentSession().preferences?.lastModel;
    return [{ modelId: lastModel ?? selected.modelId, at: null }];
  }

  /**
   * Whether the conversation holds a live cache entry for `selection`.
   * `assumeUnknownEffort` decides an entry whose effort was not recorded; the
   * one exception is a model with no effort control, whose requests never
   * carry one, so its effort cannot differ.
   */
  private isWarm(
    sessionId: string,
    entries: CacheEntry[],
    selection: Selection,
    assumeUnknownEffort: boolean,
  ): boolean {
    const ttl = cacheTtlMsFor(selection.model);
    const effortless = selection.effort === null && resolveEffortControl(selection.model) === null;
    return entries.some(entry => {
      if (entry.modelId !== selection.model.modelId) return false;
      const sameEffort =
        entry.effort === undefined ? assumeUnknownEffort || effortless : entry.effort === selection.effort;
      if (!sameEffort) return false;
      if (entry.at !== null) return Date.now() - entry.at < ttl;
      return this.isLatestWarm(sessionId, ttl);
    });
  }

  /**
   * Whether the conversation's last model call was recent enough that the
   * entry it touched is still live. One streaming right now is warm by
   * definition.
   */
  private isLatestWarm(sessionId: string, ttl: number): boolean {
    if (this.chatState.isSessionLoading(sessionId)) return true;
    const messages = this.messageMap.getMessagesForSession(sessionId)();
    const createdAt = Date.parse(messages[messages.length - 1]?.createdAt ?? '');
    const turnEnded = this.chatState.lastTurnEndedAt(sessionId);
    const lastActivity = Math.max(Number.isNaN(createdAt) ? 0 : createdAt, turnEnded ?? 0);
    // No timestamp at all: assume warm, since asking is the safe side.
    if (lastActivity === 0) return true;
    return Date.now() - lastActivity < ttl;
  }
}
