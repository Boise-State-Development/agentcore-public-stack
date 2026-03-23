import { Component, OnInit, signal, ChangeDetectionStrategy, inject } from '@angular/core';
import { Router, ActivatedRoute } from '@angular/router';
import { CommonModule } from '@angular/common';
import { CallbackService } from './callback.service';

@Component({
  selector: 'app-callback',
  templateUrl: './callback.page.html',
  styleUrl: './callback.page.css',
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class CallbackPage implements OnInit {
  private router = inject(Router);
  private route = inject(ActivatedRoute);
  private callbackService = inject(CallbackService);

  statusMessage = signal<string>('Processing your login...');
  isLoading = signal<boolean>(true);
  errorMessage = signal<string | null>(null);

  ngOnInit(): void {
    this.handleAuthCallback();
  }

  private async handleAuthCallback(): Promise<void> {
    try {
      // Get code and state from URL query parameters
      const queryParams = this.route.snapshot.queryParams;
      const code = queryParams['code'];
      const state = queryParams['state'];

      // Validate required parameters
      if (!code || !state) {
        this.errorMessage.set('Authentication error: Missing authorization code or state parameter');
        this.isLoading.set(false);
        this.statusMessage.set('Authentication failed');
        return;
      }

      // Exchange code for tokens
      this.statusMessage.set('Exchanging authorization code for tokens...');
      await this.callbackService.exchangeCodeForTokens(code, state);

      // Success - redirect to return URL or home
      this.statusMessage.set('Authentication successful! Redirecting...');
      this.isLoading.set(false);
      
      // Get stored return URL from sessionStorage (preserves query params from original URL)
      const returnUrl = sessionStorage.getItem('auth_return_url');
      sessionStorage.removeItem('auth_return_url'); // Clean up
      
      // Small delay to show success message before redirect
      setTimeout(() => {
        if (returnUrl) {
          // Navigate to the return URL (includes query params)
          this.router.navigateByUrl(returnUrl);
        } else {
          // Fallback to home if no return URL
          this.router.navigate(['/']);
        }
      }, 500);
    } catch (error) {
      // Handle errors
      const errorMsg = error instanceof Error ? error.message : 'An error occurred during authentication';
      this.errorMessage.set(errorMsg);
      this.isLoading.set(false);
      this.statusMessage.set('Authentication failed');
    }
  }
}

