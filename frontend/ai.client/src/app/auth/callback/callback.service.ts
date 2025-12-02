import { inject, Injectable } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { AuthService } from '../auth.service';
import { UserService } from '../user.service';
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
  private userService = inject(UserService);

  async exchangeCodeForTokens(code: string, state: string, redirectUri?: string): Promise<TokenExchangeResponse> {
    // Retrieve stored state token from sessionStorage for CSRF validation
    const storedState = this.authService.getStoredState();
    
    if (!storedState) {
      throw new Error('No state token found. Please initiate login again.');
    }

    // Validate state token matches (CSRF protection)
    if (storedState !== state) {
      // Clear stored state on mismatch
      this.authService.clearStoredState();
      throw new Error('State token mismatch. Security validation failed. Please try logging in again.');
    }

    const request: TokenExchangeRequest = {
      code,
      state,
      ...(redirectUri && { redirect_uri: redirectUri })
    };

    try {
      const response = await firstValueFrom(
        this.http.post<TokenExchangeResponse>(`${environment.appApiUrl}/auth/token`, request)
      );

      if (!response || !response.access_token) {
        throw new Error('Invalid token response');
      }

      // Store tokens using AuthService
      this.authService.storeTokens(response);

      // Refresh user data from new token
      this.userService.refreshUser();

      // Clear state token after successful exchange
      this.authService.clearStoredState();

      return response;
    } catch (error) {
      // Clear state token on error
      this.authService.clearStoredState();
      throw error;
    }
  }
}

