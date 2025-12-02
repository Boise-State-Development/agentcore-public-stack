import { Component, signal, ChangeDetectionStrategy, inject } from '@angular/core';
import { CommonModule } from '@angular/common';
import { AuthService } from '../auth.service';

@Component({
  selector: 'app-login',
  standalone: true,
  imports: [CommonModule],
  templateUrl: './login.page.html',
  styleUrl: './login.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class LoginPage {
  private authService = inject(AuthService);

  isLoading = signal<boolean>(false);
  errorMessage = signal<string | null>(null);

  async handleLogin(): Promise<void> {
    this.isLoading.set(true);
    this.errorMessage.set(null);

    try {
      await this.authService.login();
      // Note: The login method will redirect the browser, so this code may not execute
    } catch (error) {
      this.isLoading.set(false);
      const errorMsg = error instanceof Error ? error.message : 'An error occurred during login';
      this.errorMessage.set(errorMsg);
    }
  }
}

