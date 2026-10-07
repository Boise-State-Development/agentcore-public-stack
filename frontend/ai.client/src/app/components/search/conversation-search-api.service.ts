import { HttpClient, HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable } from 'rxjs';
import { ConfigService } from '../../services/config.service';

/**
 * How a conversation matched (`docs/specs/conversation-search.md` §5): by its
 * title, by its opening prompt, or by the full text of one of its turns.
 */
export type ConversationMatchKind = 'title' | 'prompt' | 'text';

/**
 * `lexical` searches titles and opening prompts only (DynamoDB, every keystroke);
 * `all` adds the full text of every turn (on Enter or a pause, ~700 ms).
 */
export type ConversationSearchMode = 'lexical' | 'all';

/** One matching conversation. Mirrors the backend's `ConversationSearchResult`. */
export interface ConversationSearchResult {
  sessionId: string;
  title: string;
  lastMessageAt: string;
  projectId?: string | null;
  assistantId?: string | null;
  archived: boolean;
  /** The matching turn's user message (`msg-{sessionId}-{index}`): where the conversation opens. */
  messageId?: string | null;
  /** Up to two more matching turns in the same conversation. */
  alsoMatched: string[];
  matchKind: ConversationMatchKind;
  /** Plain text around the match, at most 240 characters. Empty for a bare title match. */
  snippet: string;
  score?: number | null;
}

/** Mirrors the backend's `ConversationSearchResponse`. */
export interface ConversationSearchResponse {
  results: ConversationSearchResult[];
  /** False when only title and opening-prompt matches could be returned. */
  textSearchAvailable: boolean;
}

/** `GET /sessions/search`. Cookie-authenticated like every app-api call. */
@Injectable({ providedIn: 'root' })
export class ConversationSearchApiService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);

  search(query: string, mode: ConversationSearchMode, limit = 20): Observable<ConversationSearchResponse> {
    const params = new HttpParams().set('q', query).set('mode', mode).set('limit', limit);
    return this.http.get<ConversationSearchResponse>(`${this.config.appApiUrl()}/sessions/search`, { params });
  }
}
