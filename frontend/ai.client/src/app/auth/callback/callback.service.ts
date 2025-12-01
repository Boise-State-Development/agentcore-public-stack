import { inject, Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { AuthService } from '../auth.service';
import { environment } from '../../../environments/environment';
export interface TokenExchangeRequest {
  code: string;
  state: string;
  redirect_uri?: string;
}

export interface TokenExchangeResponse {
  access_token: string;
  refresh_token: string;
  id_token?: string;
  token_type: string;
  expires_in: number;
  scope: string;
}

@Injectable({
  providedIn: 'root'
})
export class CallbackService {
  private http = inject(HttpClient);
  private authService = inject(AuthService);

  async exchangeCodeForTokens(code: string, state: string, redirectUri?: string): Promise<TokenExchangeResponse> {
    const request: TokenExchangeRequest = {
      code,
      state,
      ...(redirectUri && { redirect_uri: redirectUri })
    };

    try {
      const response = await firstValueFrom(
        this.http.post<TokenExchangeResponse>(`${environment.chatApiUrl}/auth/token`, request)
      );

      if (!response || !response.access_token) {
        throw new Error('Invalid token response');
      }

      // Store tokens using AuthService
      this.authService.storeTokens(response);

      return response;
    } catch (error) {
      throw error;
    }
  }
}

