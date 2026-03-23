import { inject, Injectable } from '@angular/core';
import { AuthService } from '../auth.service';
import { UserService } from '../user.service';
import { SessionService } from '../../session/services/session/session.service';

@Injectable({
  providedIn: 'root'
})
export class CallbackService {
  private authService = inject(AuthService);
  private userService = inject(UserService);
  private sessionService = inject(SessionService);

  /**
   * Exchange authorization code for tokens via Cognito token endpoint.
   * Delegates to AuthService.handleCallback() for the actual token exchange.
   */
  async exchangeCodeForTokens(code: string, state: string): Promise<void> {
    // Exchange code for tokens directly with Cognito via AuthService
    await this.authService.handleCallback(code, state);

    // Refresh user data from new token
    this.userService.refreshUser();

    // Ensure resolved permissions are available before navigating
    await this.userService.ensurePermissionsLoaded();

    // Enable sessions loading now that user is authenticated
    this.sessionService.enableSessionsLoading();
  }
}
