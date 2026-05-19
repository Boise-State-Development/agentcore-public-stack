import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import type { McpAppCard } from './mcp-app-card-state.service';

interface McpAppCardDto {
  cardId: string;
  toolUseId: string;
  toolName: string;
  arguments?: Record<string, unknown>;
  content?: Array<Record<string, unknown>>;
  isError?: boolean;
  createdAt: string;
  producedByMessageIndex?: number | null;
}

interface McpAppCardListDto {
  cards: McpAppCardDto[];
}

/**
 * app-api client for Option A reload hydration (MCP Apps PR #6). Auth
 * rides the BFF session cookie via the HttpClient pipeline (GET is a safe
 * method — no CSRF needed), same as `ArtifactHttpService.listSessionArtifacts`.
 */
@Injectable({ providedIn: 'root' })
export class McpAppCardHttpService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);

  /** List this user's persisted app-initiated tool cards for a session. */
  async listSessionCards(sessionId: string): Promise<McpAppCard[]> {
    const res = await firstValueFrom(
      this.http.get<McpAppCardListDto>(
        `${this.config.appApiUrl()}/mcp-apps/cards`,
        { params: { session_id: sessionId } },
      ),
    );
    return (res.cards ?? []).map((c) => ({
      cardId: c.cardId,
      toolUseId: c.toolUseId,
      toolName: c.toolName,
      arguments: c.arguments ?? {},
      content: c.content ?? [],
      isError: !!c.isError,
      createdAt: c.createdAt,
      producedByMessageIndex: c.producedByMessageIndex ?? null,
    }));
  }
}
