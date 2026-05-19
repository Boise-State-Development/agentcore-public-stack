import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { SessionService as BffSessionService } from '../../../auth/session.service';
import { ToolService } from '../../../services/tool/tool.service';
import { SessionService } from '../session/session.service';
import { ModelService } from '../model/model.service';
import type { UpdateModelContextParams } from './mcp-app-protocol';

/**
 * App-pushed `ui/update-model-context` relay (MCP Apps PR #6).
 *
 * Structural sibling of {@link McpAppProxyService}: the authenticated POST
 * to app-api `/mcp-apps/update-context` (session cookie + CSRF) plus the
 * conversation-binding assembly so inference-api rebuilds the same cached
 * agent and stashes the payload on its Strands state for the next turn.
 * Kept as a service so the bridge stays framework-free — the component
 * injects this and hands the bridge a plain `updateModelContext` callback.
 *
 * `ui/message` is intentionally NOT here: it is "treated identically to a
 * typed message", so it goes through the normal chat send path
 * (`ChatRequestService.submitChatRequest`), starting a real streaming turn.
 */
@Injectable({ providedIn: 'root' })
export class McpAppMessageService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly bffSession = inject(BffSessionService);
  private readonly toolService = inject(ToolService);
  private readonly sessionService = inject(SessionService);
  private readonly modelService = inject(ModelService);

  /**
   * Relay one model-context update. Resolves on acceptance; rejects with
   * an Error whose message is safe to surface to the App.
   *
   * @param resourceUri the bound App resource (`ui://…`) — the host's
   *   per-App dedupe key (last-write-wins between turns).
   */
  async updateModelContext(
    resourceUri: string,
    payload: UpdateModelContextParams,
  ): Promise<void> {
    const appApiUrl = this.config.appApiUrl();
    const sessionId = this.sessionService.currentSession().sessionId;
    if (!appApiUrl || !sessionId) {
      throw new Error('No active conversation for the context update');
    }

    const usingDefault = this.modelService.isUsingDefaultModel();
    const selected = this.modelService.selectedModel();
    const body = {
      sessionId,
      resourceUri,
      content: payload.content ?? null,
      structuredContent: payload.structuredContent ?? null,
      enabledTools: this.toolService.getEnabledToolIds(),
      modelId: usingDefault ? null : selected?.modelId ?? null,
    };

    try {
      await firstValueFrom(
        this.http.post(`${appApiUrl}/mcp-apps/update-context`, body, {
          headers: this.bffSession.csrfHeaders(),
          withCredentials: true,
        }),
      );
    } catch (err) {
      const message =
        err instanceof HttpErrorResponse
          ? (err.error?.error ?? `Context update failed (${err.status})`)
          : 'Context update failed';
      throw new Error(message);
    }
  }
}
