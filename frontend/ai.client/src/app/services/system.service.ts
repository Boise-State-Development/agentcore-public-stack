import { Injectable, inject, signal } from '@angular/core';
import { ConfigService } from './config.service';

export interface SystemStatus {
  first_boot_completed: boolean;
}

export interface FirstBootRequest {
  username: string;
  email: string;
  password: string;
}

export interface FirstBootResponse {
  success: boolean;
  user_id: string;
  message: string;
}

/**
 * Service for system-level operations: first-boot status and admin setup.
 * Caches the system status to avoid repeated API calls.
 */
@Injectable({
  providedIn: 'root'
})
export class SystemService {
  private readonly config = inject(ConfigService);
  private cachedStatus = signal<boolean | null>(null);

  /**
   * Check if first-boot has been completed.
   * Caches the result so subsequent calls don't hit the API.
   * @returns true if first-boot is completed, false otherwise
   */
  async checkStatus(): Promise<boolean> {
    const cached = this.cachedStatus();
    if (cached !== null) {
      return cached;
    }

    try {
      const url = `${this.config.appApiUrl()}/system/status`;
      const response = await fetch(url);

      if (!response.ok) {
        // Treat errors as not completed (safe default)
        this.cachedStatus.set(false);
        return false;
      }

      const data: SystemStatus = await response.json();
      this.cachedStatus.set(data.first_boot_completed);
      return data.first_boot_completed;
    } catch {
      // Network error — treat as not completed
      this.cachedStatus.set(false);
      return false;
    }
  }

  /**
   * Submit the first-boot admin registration.
   * On success, invalidates the cached status.
   */
  async firstBoot(username: string, email: string, password: string): Promise<FirstBootResponse> {
    const url = `${this.config.appApiUrl()}/system/first-boot`;
    const response = await fetch(url, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, email, password } as FirstBootRequest),
    });

    if (!response.ok) {
      const errorBody = await response.json().catch(() => ({ detail: 'Unknown error' }));
      const detail = errorBody.detail || `Request failed with status ${response.status}`;
      throw new FirstBootError(detail, response.status);
    }

    const data: FirstBootResponse = await response.json();
    // Invalidate cache so next check reflects completion
    this.cachedStatus.set(true);
    return data;
  }

  /**
   * Clear the cached status (useful for testing or forced re-check).
   */
  clearCache(): void {
    this.cachedStatus.set(null);
  }
}

export class FirstBootError extends Error {
  constructor(message: string, public readonly statusCode: number) {
    super(message);
    this.name = 'FirstBootError';
  }
}
