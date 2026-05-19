import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { SessionService as BffSessionService } from '../../../auth/session.service';
import { ToolService } from '../../../services/tool/tool.service';
import { SessionService } from '../session/session.service';
import { ModelService } from '../model/model.service';

/** Shape returned to the bridge → forwarded to the App as the JSON-RPC
 *  `tools/call` result (a minimal MCP `CallToolResult`). */
export interface ProxyToolCallResult {
  content: unknown[];
  isError: boolean;
}

/**
 * App-initiated `tools/call` proxy (MCP Apps PR #5).
 *
 * Centralizes the authenticated POST to app-api `/mcp-apps/proxy-call`
 * (session cookie + CSRF, same pattern as the chat send path) and the
 * conversation-binding assembly (sessionId + the conversation's enabled
 * tools + model). Kept as an Angular service so the bridge stays
 * framework-free: the component injects this and hands the bridge a plain
 * `proxyToolCall` callback.
 */
@Injectable({ providedIn: 'root' })
export class McpAppProxyService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly bffSession = inject(BffSessionService);
  private readonly toolService = inject(ToolService);
  private readonly sessionService = inject(SessionService);
  private readonly modelService = inject(ModelService);

  /**
   * Relay one App-initiated tool call. Resolves with the CallToolResult;
   * rejects with an Error (message safe to surface to the App) on a
   * non-2xx response or transport failure.
   */
  async proxyToolCall(
    toolUseId: string,
    toolName: string,
    args: Record<string, unknown>,
  ): Promise<ProxyToolCallResult> {
    const appApiUrl = this.config.appApiUrl();
    const sessionId = this.sessionService.currentSession().sessionId;
    if (!appApiUrl || !sessionId) {
      throw new Error('No active conversation for the tool call');
    }

    const usingDefault = this.modelService.isUsingDefaultModel();
    const selected = this.modelService.selectedModel();
    const body = {
      sessionId,
      toolUseId,
      toolName,
      arguments: args ?? {},
      enabledTools: this.toolService.getEnabledToolIds(),
      modelId: usingDefault ? null : selected?.modelId ?? null,
    };

    try {
      const resp = await firstValueFrom(
        this.http.post<{ toolUseId: string; result: ProxyToolCallResult }>(
          `${appApiUrl}/mcp-apps/proxy-call`,
          body,
          { headers: this.bffSession.csrfHeaders(), withCredentials: true },
        ),
      );
      return resp.result;
    } catch (err) {
      const message =
        err instanceof HttpErrorResponse
          ? (err.error?.error ?? `Tool call failed (${err.status})`)
          : 'Tool call failed';
      throw new Error(message);
    }
  }
}
