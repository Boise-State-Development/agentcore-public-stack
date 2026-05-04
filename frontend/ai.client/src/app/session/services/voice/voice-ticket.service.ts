import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import { ConfigService } from '../../../services/config.service';

export interface VoiceTicketResponse {
  ticket: string;
  expires_in: number;
}

/**
 * Mints a single-use ticket for the voice WebSocket upgrade.
 *
 * The voice transport is BFF-proxied (issue #211): the SPA never holds a
 * Cognito access token, so it can't authenticate the WS upgrade directly
 * against the AgentCore Runtime. Instead, the SPA POSTs to the BFF
 * (`/api/voice/ticket`) — that call rides the session cookie and CSRF
 * header automatically via the existing interceptor — and gets back a
 * short-lived HMAC-signed ticket. The ticket is then passed as a query
 * param on `wss://.../api/voice/stream`; app-api validates it on upgrade
 * and opens the upstream WS itself using the BFF-stored token.
 */
@Injectable({ providedIn: 'root' })
export class VoiceTicketService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);

  async issue(sessionId: string): Promise<VoiceTicketResponse> {
    return firstValueFrom(
      this.http.post<VoiceTicketResponse>(
        `${this.config.appApiUrl()}/voice/ticket`,
        { session_id: sessionId },
      ),
    );
  }
}
