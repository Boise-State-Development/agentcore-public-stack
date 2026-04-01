import {
  Component,
  ChangeDetectionStrategy,
  input,
  output,
  signal,
  computed,
  HostListener,
} from '@angular/core';
import { NgTemplateOutlet, SlicePipe } from '@angular/common';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroSparkles,
  heroEllipsisVertical,
  heroPencilSquare,
  heroShare,
  heroGlobeAlt,
  heroLockClosed,
  heroTrash,
  heroUserGroup,
  heroUser,
  heroChatBubbleLeftRight,
} from '@ng-icons/heroicons/outline';
import { Assistant } from '../models/assistant.model';
import { TooltipDirective } from '../../components/tooltip/tooltip.directive';

/**
 * Gradient colors keyed by first letter — matches the assistant-card component exactly.
 * Each entry stores the two hex stops so we can derive both the full gradient
 * (for the fallback avatar) and a tinted wash (for the card hero background).
 */
const GRADIENT_STOPS: Record<string, [string, string]> = {
  A: ['#667eea', '#764ba2'],
  B: ['#f093fb', '#f5576c'],
  C: ['#4facfe', '#00f2fe'],
  D: ['#43e97b', '#38f9d7'],
  E: ['#fa709a', '#fee140'],
  F: ['#30cfd0', '#330867'],
  G: ['#a8edea', '#fed6e3'],
  H: ['#5ee7df', '#b490ca'],
  I: ['#d299c2', '#fef9d7'],
  J: ['#89f7fe', '#66a6ff'],
  K: ['#667eea', '#764ba2'],
  L: ['#ffecd2', '#fcb69f'],
  M: ['#a1c4fd', '#c2e9fb'],
  N: ['#d4fc79', '#96e6a1'],
  O: ['#84fab0', '#8fd3f4'],
  P: ['#a18cd1', '#fbc2eb'],
  Q: ['#a6c0fe', '#f68084'],
  R: ['#fccb90', '#d57eeb'],
  S: ['#e0c3fc', '#8ec5fc'],
  T: ['#f093fb', '#f5576c'],
  U: ['#4facfe', '#00f2fe'],
  V: ['#43e97b', '#38f9d7'],
  W: ['#fa709a', '#fee140'],
  X: ['#30cfd0', '#330867'],
  Y: ['#a8edea', '#fed6e3'],
  Z: ['#5ee7df', '#b490ca'],
};

const DEFAULT_STOPS: [string, string] = ['#667eea', '#764ba2'];

/** Build a full gradient string from two hex stops */
function buildGradient(stops: [string, string]): string {
  return `linear-gradient(135deg, ${stops[0]} 0%, ${stops[1]} 100%)`;
}

/** Convert a hex color to an rgba string at the given opacity */
function hexToRgba(hex: string, alpha: number): string {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r},${g},${b},${alpha})`;
}

/** Build a tinted wash gradient from two hex stops — light mode uses low opacity, dark mode even lower */
function buildWashGradient(stops: [string, string], isDark: boolean): string {
  const alpha = isDark ? 0.15 : 0.2;
  return `linear-gradient(135deg, ${hexToRgba(stops[0], alpha)} 0%, ${hexToRgba(stops[1], alpha)} 100%)`;
}

@Component({
  selector: 'app-assistant-list',
  templateUrl: './assistant-list.component.html',
  styleUrl: './assistant-list.component.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, NgTemplateOutlet, SlicePipe, TooltipDirective],
  providers: [
    provideIcons({
      heroSparkles,
      heroEllipsisVertical,
      heroPencilSquare,
      heroShare,
      heroGlobeAlt,
      heroLockClosed,
      heroTrash,
      heroUserGroup,
      heroUser,
      heroChatBubbleLeftRight,
    }),
  ],
})
export class AssistantListComponent {
  assistants = input.required<Assistant[]>();
  assistantSelected = output<Assistant>();
  chatRequested = output<Assistant>();
  shareRequested = output<Assistant>();
  makePublicRequested = output<Assistant>();
  makePrivateRequested = output<Assistant>();
  deleteRequested = output<Assistant>();

  openMenuId = signal<string | null>(null);

  myAssistants = computed(() => {
    return this.assistants().filter((a) => !a.isSharedWithMe);
  });

  sharedWithMe = computed(() => {
    return this.assistants().filter((a) => a.isSharedWithMe);
  });

  @HostListener('document:click', ['$event'])
  onDocumentClick(event: Event): void {
    if (this.openMenuId() !== null) {
      const target = event.target as HTMLElement;
      if (!target.closest('.context-menu-container')) {
        this.openMenuId.set(null);
      }
    }
  }

  onAssistantClick(assistant: Assistant): void {
    this.chatRequested.emit(assistant);
  }

  onEditClick(assistant: Assistant, event: Event): void {
    event.stopPropagation();
    this.assistantSelected.emit(assistant);
  }

  onMenuToggle(assistantId: string, event: Event): void {
    event.stopPropagation();
    this.openMenuId.set(this.openMenuId() === assistantId ? null : assistantId);
  }

  onMenuAction(
    assistant: Assistant,
    action: 'edit' | 'share' | 'make-public' | 'make-private' | 'delete',
    event: Event,
  ): void {
    event.stopPropagation();
    this.openMenuId.set(null);

    switch (action) {
      case 'edit':
        this.assistantSelected.emit(assistant);
        break;
      case 'share':
        this.shareRequested.emit(assistant);
        break;
      case 'make-public':
        this.makePublicRequested.emit(assistant);
        break;
      case 'make-private':
        this.makePrivateRequested.emit(assistant);
        break;
      case 'delete':
        this.deleteRequested.emit(assistant);
        break;
    }
  }

  isMenuOpen(assistantId: string): boolean {
    return this.openMenuId() === assistantId;
  }

  /** Get a tinted wash of the assistant's letter-gradient for the card hero background */
  getCardBackground(assistant: Assistant): string {
    const letter = assistant.name ? assistant.name.charAt(0).toUpperCase() : '?';
    const stops = GRADIENT_STOPS[letter] || DEFAULT_STOPS;
    const isDark = document.documentElement.classList.contains('dark');
    return buildWashGradient(stops, isDark);
  }

  /** Get the full gradient for the first-letter fallback avatar (matches assistant-card) */
  getLetterGradient(name: string): string {
    const letter = name ? name.charAt(0).toUpperCase() : '?';
    const stops = GRADIENT_STOPS[letter] || DEFAULT_STOPS;
    return buildGradient(stops);
  }

  getStatusBadgeClasses(status: Assistant['status']): string {
    const baseClasses = 'inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide';

    switch (status) {
      case 'COMPLETE':
        return `${baseClasses} bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400`;
      case 'DRAFT':
        return `${baseClasses} bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400`;
      default:
        return `${baseClasses} bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400`;
    }
  }

  getVisibilityBadgeClasses(visibility: Assistant['visibility']): string {
    const baseClasses = 'inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide';

    switch (visibility) {
      case 'PUBLIC':
        return `${baseClasses} bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400`;
      case 'SHARED':
        return `${baseClasses} bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400`;
      case 'PRIVATE':
        return `${baseClasses} bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400`;
      default:
        return `${baseClasses} bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-400`;
    }
  }

  getVisibilityIcon(visibility: Assistant['visibility']): string {
    switch (visibility) {
      case 'PUBLIC':
        return 'heroGlobeAlt';
      case 'SHARED':
        return 'heroUserGroup';
      case 'PRIVATE':
        return 'heroLockClosed';
      default:
        return 'heroLockClosed';
    }
  }

  getOwnerBadgeClasses(): string {
    return 'inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide bg-indigo-100 text-indigo-700 dark:bg-indigo-900/30 dark:text-indigo-400';
  }

  private simpleHash(str: string): number {
    let hash = 0;
    for (let i = 0; i < str.length; i++) {
      hash = ((hash << 5) - hash) + str.charCodeAt(i);
      hash |= 0;
    }
    return Math.abs(hash);
  }
}
