import { Component, inject, signal, ChangeDetectionStrategy, effect, ElementRef } from '@angular/core';
import { ThemeService, ThemePreference } from '../../services/theme.service';
import { DOCUMENT } from '@angular/common';

@Component({
  selector: 'app-theme-toggle',
  templateUrl: './theme-toggle.component.html',
  styleUrl: './theme-toggle.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    'class': 'relative'
  }
})
export class ThemeToggleComponent {
  private readonly themeService = inject(ThemeService);
  private readonly elementRef = inject(ElementRef);
  private readonly document = inject(DOCUMENT);
  
  protected readonly isOpen = signal(false);
  protected readonly currentPreference = this.themeService.preference;
  protected readonly currentTheme = this.themeService.theme;

  constructor() {
    // Handle click outside to close menu
    effect(() => {
      const open = this.isOpen();
      if (open) {
        const handleClickOutside = (event: MouseEvent) => {
          if (!this.elementRef.nativeElement.contains(event.target)) {
            this.closeMenu();
          }
        };
        
        setTimeout(() => {
          this.document.addEventListener('click', handleClickOutside);
        }, 0);
        
        return () => {
          this.document.removeEventListener('click', handleClickOutside);
        };
      }
      return undefined;
    });
  }

  protected toggleMenu(): void {
    this.isOpen.update(open => !open);
  }

  protected closeMenu(): void {
    this.isOpen.set(false);
  }

  protected selectTheme(preference: ThemePreference): void {
    this.themeService.setPreference(preference);
    this.closeMenu();
  }

  protected handleKeydown(event: KeyboardEvent): void {
    if (event.key === 'Escape') {
      this.closeMenu();
    }
  }
}

