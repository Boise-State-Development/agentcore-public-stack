import { HttpClient } from '@angular/common/http';
import { Injectable, computed, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../services/config.service';

/**
 * Which mode an Agent's knowledge base runs in
 * (`GET /assistants/{id}/knowledge-base/mode`, app-api
 * `assistants/knowledge_mode.py`).
 *
 * - `whole`: its text documents go to the model in the system prompt.
 * - `searched`: each message retrieves the passages that match it.
 * - `empty`: no document is ready yet. `unknown`: could not be worked out.
 */
export type KnowledgeMode = 'whole' | 'searched' | 'empty' | 'unknown';

/** Why `searched`. Stable strings from the backend's `kb_full_context` `REASON_*`. */
export type KnowledgeSearchedReason =
  | 'full_context_off'
  | 'no_text_documents'
  | 'raw_bytes'
  | 'no_text_layer'
  | 'document_over_budget'
  | 'over_budget';

export interface KnowledgeModeStatus {
  mode: KnowledgeMode;
  reason: KnowledgeSearchedReason | null;
  /** Documents whose text is (or would be) sent whole. */
  documentCount: number;
  /**
   * Their text in estimated tokens; 0 when it was never read. For
   * `document_over_budget`, the one oversized document's alone.
   */
  estimatedTokens: number;
  budgetTokens: number;
  /** Spreadsheets and decks, which are searched in either mode. */
  skippedDocuments: string[];
}

/** What the client shows when the call fails: nothing. */
export const KNOWLEDGE_MODE_UNKNOWN: KnowledgeModeStatus = {
  mode: 'unknown',
  reason: null,
  documentCount: 0,
  estimatedTokens: 0,
  budgetTokens: 0,
  skippedDocuments: [],
};

@Injectable({ providedIn: 'root' })
export class KnowledgeModeService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/assistants`);

  /** Resolves, never rejects: a failure is `unknown`, which renders nothing. */
  async getMode(entityId: string): Promise<KnowledgeModeStatus> {
    try {
      const status = await firstValueFrom(
        this.http.get<KnowledgeModeStatus>(`${this.baseUrl()}/${entityId}/knowledge-base/mode`),
      );
      return { ...KNOWLEDGE_MODE_UNKNOWN, ...status };
    } catch {
      return KNOWLEDGE_MODE_UNKNOWN;
    }
  }
}
