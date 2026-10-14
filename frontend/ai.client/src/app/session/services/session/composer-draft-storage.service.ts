import { Injectable } from '@angular/core';

/**
 * Everything a user has composed but not sent, remembered per conversation
 * across navigation and reloads: the text, the `@`-mention bound to it, the
 * follow-ups queued behind a streaming turn, and the files attached to it.
 *
 * Distinct from {@link ComposerDraftService}, which is a one-shot *hand-off*:
 * a feature pushes text at the composer for the user to edit. This is the
 * opposite direction — the composer parks what the user built and takes it
 * back when they return to that conversation.
 *
 * `localStorage`, not memory: the losses worth preventing are the ones the
 * user did not choose — a reload, a closed tab, a session expiry bouncing
 * through Cognito — and an in-memory map survives none of them. The key for
 * the new-conversation composer is a constant ({@link NEW_CONVERSATION_DRAFT_KEY}),
 * so staging a file upload (which mints a session id before the first send)
 * does not move the draft out from under it.
 *
 * One `localStorage` entry per conversation rather than one JSON file of all
 * of them: two tabs sitting in different conversations then never overwrite
 * each other's drafts, and no write has to parse every other draft first.
 * Two tabs in the *same* conversation still race, last write winning — which
 * is the behaviour a single draft slot can offer, and each tab's own composer
 * is untouched while it is open.
 *
 * **Nothing stored here is trusted.** Attachment metadata is a display cache
 * so the cards paint without waiting on a round trip; the upload ids are
 * pointers the server re-checks against the signed-in user on both the
 * reconcile (`GET /files?sessionId=`) and the send (`FileResolver` keys on
 * `USER#{userId}`), so a tampered entry resolves to no file.
 *
 * Storage is best-effort throughout. A blocked or full `localStorage` (private
 * windows, a storage-disabled browser, quota) loses the draft and nothing
 * else — every call swallows its own failure rather than taking the composer
 * down with it.
 */

/** Prefix for every stored draft; also what the prune scan matches on. */
const KEY_PREFIX = 'composer-draft:';

/** Records which account the stored drafts belong to. See {@link ComposerDraftStorageService.adopt}. */
const OWNER_KEY = 'composer-draft-owner';

/**
 * Drafts older than this are dropped on the next prune. A month is long past
 * the point where reinstating text reads as helpful rather than surprising.
 */
const DRAFT_TTL_MS = 30 * 24 * 60 * 60 * 1000;

/**
 * Most drafts kept. Well past any plausible number of conversations someone
 * has half-written text in, and low enough that the whole set stays small
 * next to a 5MB origin quota.
 */
const MAX_DRAFTS = 50;

/**
 * Longest serialized draft stored. A composer holds prose and a handful of
 * file *pointers*, never file bytes, so this is generous; past it the text is
 * dropped and the rest of the draft kept, because the oversized thing is
 * always the paste and dropping the attachments with it would lose what the
 * user cannot retype.
 */
const MAX_DRAFT_CHARS = 32_000;

/** The draft key for the composer on a conversation that has not been sent yet. */
export const NEW_CONVERSATION_DRAFT_KEY = 'new';

/**
 * A file the user attached but has not sent. Metadata only — enough to paint
 * the card straight from storage, re-checked against the server on restore.
 */
export interface StoredAttachment {
  uploadId: string;
  filename: string;
  mimeType: string;
  sizeBytes: number;
}

/**
 * A Drive folder attached to the composer and not sent yet (drive-folder spec
 * §4.7). The id is all the send needs; the name and source paint the card on
 * first frame, and the composer re-reads the row to bring its status current.
 */
export interface StoredFolder {
  folderAttachId: string;
  name: string;
  source: string;
}

/** A composer's unsent state for one conversation. */
export interface StoredComposerDraft {
  text: string;
  /** The Agent an `@`-mention bound to the next turn, by id (D11). */
  mentionAgentId?: string;
  /**
   * Follow-ups the user queued behind a streaming turn and that were never
   * confirmed as delivered. Text only — on restore they fold back into the
   * composer rather than re-arming, so nothing here carries a queue's promise.
   */
  queued?: string[];
  /** Files attached to the composer or to a queued follow-up, not yet sent. */
  attachments?: StoredAttachment[];
  /** Folders attached to the composer or to a queued follow-up, not yet sent. */
  folders?: StoredFolder[];
  /**
   * The conversation those uploads were filed under. For a sent conversation
   * this is its own id; for one still unsent it is the client-minted staged id,
   * which the page re-adopts so a later attachment joins the same conversation.
   */
  attachmentSessionId?: string;
}

/** The on-disk row: a draft plus the timestamp that drives TTL and pruning. */
interface StoredDraftRow extends StoredComposerDraft {
  updatedAt: number;
}

/** An empty draft, for callers that would otherwise null-check every field. */
export const EMPTY_DRAFT: StoredComposerDraft = { text: '' };

/** Whether a draft holds anything worth coming back to. */
export function isEmptyDraft(draft: StoredComposerDraft): boolean {
  return (
    !draft.text.trim() &&
    (draft.queued?.length ?? 0) === 0 &&
    (draft.attachments?.length ?? 0) === 0 &&
    (draft.folders?.length ?? 0) === 0
  );
}

/** Whether a parsed value is shaped like a stored folder. */
function isStoredFolder(value: unknown): value is StoredFolder {
  const candidate = value as Partial<StoredFolder> | null;
  return (
    typeof candidate?.folderAttachId === 'string' &&
    typeof candidate.name === 'string' &&
    typeof candidate.source === 'string'
  );
}

/** Whether a parsed value is shaped like a stored attachment. */
function isStoredAttachment(value: unknown): value is StoredAttachment {
  const candidate = value as Partial<StoredAttachment> | null;
  return (
    typeof candidate?.uploadId === 'string' &&
    typeof candidate.filename === 'string' &&
    typeof candidate.mimeType === 'string' &&
    typeof candidate.sizeBytes === 'number'
  );
}

@Injectable({ providedIn: 'root' })
export class ComposerDraftStorageService {
  constructor() {
    this.prune();
  }

  /** The remembered draft for a conversation; {@link EMPTY_DRAFT} when there is none. */
  read(key: string): StoredComposerDraft {
    const raw = this.storage()?.getItem(KEY_PREFIX + key);
    if (!raw) return EMPTY_DRAFT;
    const row = this.parse(raw);
    if (!row) return EMPTY_DRAFT;
    if (this.isExpired(row.updatedAt)) {
      this.write(key, EMPTY_DRAFT);
      return EMPTY_DRAFT;
    }
    const { updatedAt: _dropped, ...draft } = row;
    return draft;
  }

  /**
   * Remember a conversation's unsent composer. An empty draft (no text, no
   * queued follow-ups, no attachments) removes the entry rather than storing a
   * blank one — that is what makes sending, queueing or clearing the composer
   * forget the draft without any of those paths having to say so.
   */
  write(key: string, draft: StoredComposerDraft): void {
    const storage = this.storage();
    if (!storage) return;
    const storageKey = KEY_PREFIX + key;
    try {
      if (isEmptyDraft(draft)) {
        storage.removeItem(storageKey);
        return;
      }
      const existed = storage.getItem(storageKey) !== null;
      const payload = this.serialize({ ...draft, updatedAt: Date.now() });
      if (payload === null) {
        storage.removeItem(storageKey);
        return;
      }
      storage.setItem(storageKey, payload);
      // Only a brand-new conversation can push the set over the cap, so the
      // scan runs there and not on every keystroke of an existing draft.
      if (!existed) this.prune();
    } catch {
      // Quota or a blocked store — the draft simply isn't remembered.
    }
  }

  /**
   * Claim the stored drafts for `userId`, discarding them if they belong to
   * someone else.
   *
   * Logout clears drafts outright, but a shared machine where the previous
   * person closed the browser instead never ran that path. Stamping the owner
   * on session bootstrap means the next sign-in collects what logout missed.
   */
  adopt(userId: string): void {
    const storage = this.storage();
    if (!storage) return;
    try {
      if (storage.getItem(OWNER_KEY) !== userId) {
        this.clear();
        storage.setItem(OWNER_KEY, userId);
      }
    } catch {
      // Best-effort: an unreadable owner stamp is not worth failing bootstrap.
    }
  }

  /** Drop every stored draft (logout, or a change of account). */
  clear(): void {
    const storage = this.storage();
    if (!storage) return;
    try {
      for (const key of this.draftKeys(storage)) {
        storage.removeItem(key);
      }
      storage.removeItem(OWNER_KEY);
    } catch {
      // Nothing useful to do if the store refuses us.
    }
  }

  /**
   * Serialize a row, dropping the text if the result is oversized.
   *
   * Returns `null` when even the text-less draft will not fit, which can only
   * happen with an absurd number of attachments — the caller removes the entry
   * rather than storing half of one.
   */
  private serialize(row: StoredDraftRow): string | null {
    const full = JSON.stringify(row);
    if (full.length <= MAX_DRAFT_CHARS) return full;
    const withoutText = JSON.stringify({ ...row, text: '' });
    return withoutText.length <= MAX_DRAFT_CHARS ? withoutText : null;
  }

  /** Drop expired drafts, then the oldest of whatever is left over the cap. */
  private prune(): void {
    const storage = this.storage();
    if (!storage) return;
    try {
      const live: { key: string; updatedAt: number }[] = [];
      for (const key of this.draftKeys(storage)) {
        const row = this.parse(storage.getItem(key));
        if (!row || this.isExpired(row.updatedAt)) {
          storage.removeItem(key);
          continue;
        }
        live.push({ key, updatedAt: row.updatedAt });
      }
      if (live.length <= MAX_DRAFTS) return;
      live.sort((a, b) => b.updatedAt - a.updatedAt);
      for (const stale of live.slice(MAX_DRAFTS)) {
        storage.removeItem(stale.key);
      }
    } catch {
      // A store we cannot enumerate is a store we cannot prune.
    }
  }

  /**
   * Parse one stored row, rejecting anything that is not shaped like a draft.
   *
   * Field-by-field rather than a cast: the row survives a deploy that changed
   * the shape, and it is the one place hand-edited `localStorage` reaches the
   * app, so a malformed entry has to read as "no draft" and not as a draft
   * with undefined innards.
   */
  private parse(raw: string | null): StoredDraftRow | null {
    if (!raw) return null;
    try {
      const value = JSON.parse(raw) as Partial<StoredDraftRow>;
      if (typeof value?.text !== 'string') return null;
      if (typeof value.updatedAt !== 'number') return null;
      return {
        text: value.text,
        updatedAt: value.updatedAt,
        mentionAgentId:
          typeof value.mentionAgentId === 'string' ? value.mentionAgentId : undefined,
        queued: Array.isArray(value.queued)
          ? value.queued.filter((entry): entry is string => typeof entry === 'string')
          : undefined,
        attachments: Array.isArray(value.attachments)
          ? value.attachments.filter(isStoredAttachment)
          : undefined,
        folders: Array.isArray(value.folders) ? value.folders.filter(isStoredFolder) : undefined,
        attachmentSessionId:
          typeof value.attachmentSessionId === 'string'
            ? value.attachmentSessionId
            : undefined,
      };
    } catch {
      // Unparseable entry from an older shape or a truncated write.
      return null;
    }
  }

  /** Snapshot of the draft keys, taken before any removal shifts the indices. */
  private draftKeys(storage: Storage): string[] {
    const keys: string[] = [];
    for (let i = 0; i < storage.length; i++) {
      const key = storage.key(i);
      if (key?.startsWith(KEY_PREFIX)) keys.push(key);
    }
    return keys;
  }

  private isExpired(updatedAt: number): boolean {
    return Date.now() - updatedAt > DRAFT_TTL_MS;
  }

  /** `null` during SSR, and in a browser that refuses storage outright. */
  private storage(): Storage | null {
    try {
      return typeof localStorage === 'undefined' ? null : localStorage;
    } catch {
      return null;
    }
  }
}
