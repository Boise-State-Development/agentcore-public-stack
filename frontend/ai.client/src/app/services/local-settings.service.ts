import { Injectable, signal } from '@angular/core';

@Injectable({
  providedIn: 'root'
})
export class LocalSettingsService {
  private readonly SHOW_TOKEN_COUNT_KEY = 'show-token-count';

  readonly showTokenCount = signal(this.loadBoolean(this.SHOW_TOKEN_COUNT_KEY, false));

  setShowTokenCount(value: boolean): void {
    this.showTokenCount.set(value);
    localStorage.setItem(this.SHOW_TOKEN_COUNT_KEY, JSON.stringify(value));
  }

  private loadBoolean(key: string, defaultValue: boolean): boolean {
    const stored = localStorage.getItem(key);
    if (stored === null) return defaultValue;
    try {
      return JSON.parse(stored) === true;
    } catch {
      return defaultValue;
    }
  }
}
