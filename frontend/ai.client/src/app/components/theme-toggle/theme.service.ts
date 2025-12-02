import { Injectable, signal, effect, inject } from '@angular/core';
import { DOCUMENT } from '@angular/common';

export type ThemePreference = 'light' | 'dark' | 'system';

@Injectable({
  providedIn: 'root'
})
export class ThemeService {
  private readonly document = inject(DOCUMENT);
  private readonly storageKey = 'theme-preference';
  
  readonly preference = signal<ThemePreference>(this.getStoredPreference());
  readonly theme = signal<'light' | 'dark'>(this.getEffectiveTheme());

  constructor() {
    // Apply theme immediately on service initialization
    this.applyTheme(this.theme());
    
    // Watch for system preference changes
    if (typeof window !== 'undefined' && window.matchMedia) {
      const mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
      const handleChange = () => {
        if (this.preference() === 'system') {
          const effectiveTheme = mediaQuery.matches ? 'dark' : 'light';
          this.theme.set(effectiveTheme);
          this.applyTheme(effectiveTheme);
        }
      };
      
      if (mediaQuery.addEventListener) {
        mediaQuery.addEventListener('change', handleChange);
      } else {
        // Fallback for older browsers
        mediaQuery.addListener(handleChange);
      }
    }
    
    // Watch for preference changes and apply theme
    effect(() => {
      const pref = this.preference();
      const effectiveTheme = this.getEffectiveTheme();
      this.theme.set(effectiveTheme);
      this.applyTheme(effectiveTheme);
      this.savePreference(pref);
    });
  }

  setPreference(preference: ThemePreference): void {
    this.preference.set(preference);
  }

  private getStoredPreference(): ThemePreference {
    if (typeof window === 'undefined' || !window.localStorage) {
      return 'system';
    }
    const stored = localStorage.getItem(this.storageKey);
    if (stored === 'light' || stored === 'dark' || stored === 'system') {
      return stored;
    }
    return 'system';
  }

  private getEffectiveTheme(): 'light' | 'dark' {
    const pref = this.preference();
    if (pref === 'system') {
      if (typeof window !== 'undefined' && window.matchMedia) {
        return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
      }
      return 'light';
    }
    return pref;
  }

  private applyTheme(theme: 'light' | 'dark'): void {
    const htmlElement = this.document.documentElement;
    if (theme === 'dark') {
      htmlElement.classList.add('dark');
      htmlElement.style.colorScheme = 'dark';
    } else {
      htmlElement.classList.remove('dark');
      htmlElement.style.colorScheme = 'light';
    }
  }

  private savePreference(preference: ThemePreference): void {
    if (typeof window !== 'undefined' && window.localStorage) {
      localStorage.setItem(this.storageKey, preference);
    }
  }
}
