import {
  Component,
  ChangeDetectionStrategy,
  input,
  computed,
  output,
  signal,
  ElementRef,
  inject,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroPencilSquare,
  heroPlusCircle,
  heroChevronDown,
  heroUserGroup,
} from '@ng-icons/heroicons/outline';

/**
 * A prominent assistant indicator chip with gradient accent and
 * action dropdown menu (Share, Edit, New Session).
 */
@Component({
  selector: 'app-assistant-indicator',
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroPencilSquare,
      heroPlusCircle,
      heroChevronDown,
      heroUserGroup,
    }),
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:click)': 'onDocumentClick($event)',
    '(document:keydown.escape)': 'onEscape()',
  },
  template: `
    <div class="indicator-wrapper">
      @if (variant() === 'compact') {
        <!-- Compact pill: subtle badge, name only, opens the actions menu -->
        <button
          type="button"
          (click)="toggleMenu()"
          class="assistant-pill"
          [class.open]="menuOpen()"
          [attr.aria-label]="'Agent: ' + name() + '. Click for options.'"
          [attr.aria-expanded]="menuOpen()"
          aria-haspopup="menu"
        >
          @if (emoji()) {
            <span class="pill-emoji leading-none">{{ emoji() }}</span>
          }
          <span class="pill-name">{{ name() }}</span>
        </button>
      } @else {
        <button
          type="button"
          (click)="toggleMenu()"
          class="assistant-indicator"
          [attr.aria-label]="'Agent: ' + name() + '. Click for options.'"
          [attr.aria-expanded]="menuOpen()"
          aria-haspopup="menu"
        >
          <!-- Avatar -->
          <div class="indicator-avatar" [style.background]="avatarGradient()">
            @if (emoji()) {
              <span class="text-lg leading-none">{{ emoji() }}</span>
            } @else {
              <span class="text-sm font-bold leading-none text-white">{{ firstLetter() }}</span>
            }
          </div>

          <!-- Name + owner -->
          <div class="indicator-text">
            <span class="indicator-name">{{ name() }}</span>
            @if (ownerName()) {
              <span class="indicator-owner">by {{ ownerName() }}</span>
            }
          </div>

          <!-- Chevron -->
          <ng-icon
            name="heroChevronDown"
            class="indicator-chevron"
            [class.rotated]="menuOpen()"
            aria-hidden="true"
          />
        </button>
      }

      <!-- Dropdown menu -->
      @if (menuOpen()) {
        <div
          class="indicator-menu"
          [class.placement-down]="menuPlacement() === 'down'"
          role="menu"
          aria-label="Agent actions"
        >
          <button
            type="button"
            class="menu-item"
            role="menuitem"
            (click)="onNewSession()"
          >
            <ng-icon name="heroPlusCircle" class="menu-icon" />
            <span>New session</span>
          </button>

          @if (isOwner()) {
            <button
              type="button"
              class="menu-item"
              role="menuitem"
              (click)="onEdit()"
            >
              <ng-icon name="heroPencilSquare" class="menu-icon" />
              <span>Edit agent</span>
            </button>

            <button
              type="button"
              class="menu-item"
              role="menuitem"
              (click)="onShare()"
            >
              <ng-icon name="heroUserGroup" class="menu-icon" />
              <span>Share settings</span>
            </button>
          }
        </div>
      }
    </div>
  `,
  styles: [`
    @import "tailwindcss";
    @custom-variant dark (&:where(.dark, .dark *));

    :host {
      display: inline-flex;
      min-width: 0;
    }

    /* ── Compact pill (top nav) ── */
    .assistant-pill {
      display: inline-flex;
      align-items: center;
      gap: 0.375rem;
      max-width: 100%;
      padding: 0.1875rem 0.5rem;
      border-radius: 0.5rem;
      background: var(--color-gray-100);
      color: var(--color-gray-600);
      cursor: pointer;
      transition: background 0.15s ease, color 0.15s ease;

      &:hover,
      &.open {
        background: var(--color-gray-200);
        color: var(--color-gray-800);
      }

      &:focus-visible {
        outline: 2px solid var(--color-blue-500);
        outline-offset: 2px;
      }
    }

    :host-context(html.dark) .assistant-pill {
      background: rgba(255, 255, 255, 0.08);
      color: var(--color-gray-300);

      &:hover,
      &.open {
        background: rgba(255, 255, 255, 0.14);
        color: var(--color-gray-100);
      }
    }

    .pill-emoji {
      font-size: 0.875rem;
      flex-shrink: 0;
    }

    .pill-name {
      font-size: 0.8125rem;
      font-weight: 600;
      letter-spacing: -0.01em;
      white-space: nowrap;
      max-width: 200px;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .indicator-wrapper {
      position: relative;
      display: inline-flex;
    }

    .assistant-indicator {
      display: inline-flex;
      align-items: center;
      gap: 0.625rem;
      padding: 0.25rem 0.75rem 0.25rem 0;
      border-radius: 0.75rem;
      position: relative;
      overflow: hidden;

      /* Card-like surface */
      background: white;
      border: 1px solid var(--color-gray-200);
      box-shadow:
        0 1px 3px rgba(0, 0, 0, 0.06),
        0 4px 12px rgba(0, 0, 0, 0.04);

      /* Animation */
      animation: indicator-enter 0.35s cubic-bezier(0.16, 1, 0.3, 1) forwards;
      opacity: 0;

      /* Interaction */
      cursor: pointer;
      transition:
        transform 0.2s cubic-bezier(0.16, 1, 0.3, 1),
        box-shadow 0.2s ease,
        border-color 0.2s ease;

      &:hover {
        transform: translateY(-1px);
        border-color: var(--color-gray-300);
        box-shadow:
          0 2px 8px rgba(0, 0, 0, 0.08),
          0 8px 24px rgba(0, 0, 0, 0.06);
      }

      &:active {
        transform: translateY(0);
      }

      &:focus-visible {
        outline: 2px solid var(--color-blue-500);
        outline-offset: 2px;
      }
    }

    /* Dark mode */
    :host-context(html.dark) .assistant-indicator {
      background: rgb(30 41 59); /* slate-800 */
      border-color: rgba(255, 255, 255, 0.1);
      box-shadow:
        0 1px 3px rgba(0, 0, 0, 0.2),
        0 4px 12px rgba(0, 0, 0, 0.15);

      &:hover {
        border-color: rgba(255, 255, 255, 0.18);
        box-shadow:
          0 2px 8px rgba(0, 0, 0, 0.25),
          0 8px 24px rgba(0, 0, 0, 0.2);
      }
    }

    .indicator-avatar {
      display: flex;
      align-items: center;
      justify-content: center;
      width: 2.25rem;
      align-self: stretch;
      margin: -0.25rem 0;
      border-radius: 0.75rem 0 0 0.75rem;
      flex-shrink: 0;
    }

    .indicator-text {
      display: flex;
      flex-direction: column;
      min-width: 0;
    }

    .indicator-name {
      font-size: 0.8125rem;
      font-weight: 600;
      color: var(--color-gray-800);
      letter-spacing: -0.01em;
      white-space: nowrap;
      max-width: 200px;
      overflow: hidden;
      text-overflow: ellipsis;
      line-height: 1.2;
    }

    :host-context(html.dark) .indicator-name {
      color: var(--color-gray-100);
    }

    .indicator-owner {
      font-size: 0.6875rem;
      color: var(--color-gray-400);
      line-height: 1.2;
      white-space: nowrap;
    }

    :host-context(html.dark) .indicator-owner {
      color: var(--color-gray-500);
    }

    .indicator-chevron {
      font-size: 0.875rem;
      color: var(--color-gray-400);
      flex-shrink: 0;
      transition: transform 0.2s ease;
    }

    .indicator-chevron.rotated {
      transform: rotate(180deg);
    }

    :host-context(html.dark) .indicator-chevron {
      color: var(--color-gray-500);
    }

    /* ── Dropdown menu ── */
    .indicator-menu {
      position: absolute;
      bottom: calc(100% + 0.375rem);
      left: 50%;
      transform: translateX(-50%);
      min-width: 11rem;
      padding: 0.25rem;
      border-radius: 0.75rem;
      background: white;
      border: 1px solid var(--color-gray-200);
      box-shadow:
        0 4px 16px rgba(0, 0, 0, 0.1),
        0 1px 4px rgba(0, 0, 0, 0.06);
      animation: menu-enter 0.15s cubic-bezier(0.16, 1, 0.3, 1) forwards;
      z-index: 50;
    }

    :host-context(html.dark) .indicator-menu {
      background: rgb(30 41 59);
      border-color: rgba(255, 255, 255, 0.1);
      box-shadow:
        0 4px 16px rgba(0, 0, 0, 0.3),
        0 1px 4px rgba(0, 0, 0, 0.2);
    }

    /* Open below the chip instead of above (e.g. in the top nav). */
    .indicator-menu.placement-down {
      top: calc(100% + 0.375rem);
      bottom: auto;
      left: 0;
      transform: none;
      animation: menu-enter-down 0.15s cubic-bezier(0.16, 1, 0.3, 1) forwards;
    }

    .menu-item {
      display: flex;
      align-items: center;
      gap: 0.5rem;
      width: 100%;
      padding: 0.5rem 0.75rem;
      border-radius: 0.5rem;
      font-size: 0.8125rem;
      font-weight: 500;
      color: var(--color-gray-700);
      cursor: pointer;
      transition: all 120ms ease;
      border: none;
      background: none;

      &:hover {
        background: var(--color-gray-100);
        color: var(--color-gray-900);
      }

      &:focus-visible {
        outline: 2px solid var(--color-blue-500);
        outline-offset: -2px;
      }
    }

    :host-context(html.dark) .menu-item {
      color: var(--color-gray-300);

      &:hover {
        background: rgba(255, 255, 255, 0.08);
        color: var(--color-gray-100);
      }
    }

    .menu-icon {
      font-size: 1rem;
      color: var(--color-gray-400);
      flex-shrink: 0;
    }

    :host-context(html.dark) .menu-icon {
      color: var(--color-gray-500);
    }

    @keyframes indicator-enter {
      0% {
        opacity: 0;
        transform: translateY(-6px) scale(0.97);
      }
      100% {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
    }

    @keyframes menu-enter {
      0% {
        opacity: 0;
        transform: translateX(-50%) translateY(4px) scale(0.96);
      }
      100% {
        opacity: 1;
        transform: translateX(-50%) translateY(0) scale(1);
      }
    }

    @keyframes menu-enter-down {
      0% {
        opacity: 0;
        transform: translateY(-4px) scale(0.96);
      }
      100% {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
    }
  `],
})
export class AssistantIndicatorComponent {
  private elementRef = inject(ElementRef);

  // Inputs
  readonly name = input.required<string>();
  readonly emoji = input<string>('');
  readonly imageUrl = input<string | null>(null);
  readonly ownerName = input<string>('');
  readonly isOwner = input<boolean>(false);
  /** Direction the actions dropdown opens. Use 'down' in the top nav. */
  readonly menuPlacement = input<'up' | 'down'>('up');
  /**
   * Visual style. 'card' is the full chip (avatar + owner); 'compact' is a
   * subtle name-only pill for dense contexts like the top nav.
   */
  readonly variant = input<'card' | 'compact'>('card');

  // Outputs
  readonly newSessionClicked = output<void>();
  readonly editClicked = output<void>();
  readonly shareClicked = output<void>();

  // Menu state
  readonly menuOpen = signal(false);

  // Computed: first letter for avatar fallback
  readonly firstLetter = computed(() => {
    const name = this.name();
    return name ? name.charAt(0).toUpperCase() : '?';
  });

  // Computed: gradient based on first letter
  readonly avatarGradient = computed(() => {
    const letter = this.firstLetter();
    const gradients: Record<string, string> = {
      'A': 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
      'B': 'linear-gradient(135deg, #f093fb 0%, #f5576c 100%)',
      'C': 'linear-gradient(135deg, #4facfe 0%, #00f2fe 100%)',
      'D': 'linear-gradient(135deg, #43e97b 0%, #38f9d7 100%)',
      'E': 'linear-gradient(135deg, #fa709a 0%, #fee140 100%)',
      'F': 'linear-gradient(135deg, #30cfd0 0%, #330867 100%)',
      'G': 'linear-gradient(135deg, #a8edea 0%, #fed6e3 100%)',
      'H': 'linear-gradient(135deg, #5ee7df 0%, #b490ca 100%)',
      'I': 'linear-gradient(135deg, #d299c2 0%, #fef9d7 100%)',
      'J': 'linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%)',
      'K': 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)',
      'L': 'linear-gradient(135deg, #ffecd2 0%, #fcb69f 100%)',
      'M': 'linear-gradient(135deg, #a1c4fd 0%, #c2e9fb 100%)',
      'N': 'linear-gradient(135deg, #d4fc79 0%, #96e6a1 100%)',
      'O': 'linear-gradient(135deg, #84fab0 0%, #8fd3f4 100%)',
      'P': 'linear-gradient(135deg, #cfd9df 0%, #e2ebf0 100%)',
      'Q': 'linear-gradient(135deg, #a6c0fe 0%, #f68084 100%)',
      'R': 'linear-gradient(135deg, #fccb90 0%, #d57eeb 100%)',
      'S': 'linear-gradient(135deg, #e0c3fc 0%, #8ec5fc 100%)',
      'T': 'linear-gradient(135deg, #f093fb 0%, #f5576c 100%)',
      'U': 'linear-gradient(135deg, #4facfe 0%, #00f2fe 100%)',
      'V': 'linear-gradient(135deg, #43e97b 0%, #38f9d7 100%)',
      'W': 'linear-gradient(135deg, #fa709a 0%, #fee140 100%)',
      'X': 'linear-gradient(135deg, #30cfd0 0%, #330867 100%)',
      'Y': 'linear-gradient(135deg, #a8edea 0%, #fed6e3 100%)',
      'Z': 'linear-gradient(135deg, #5ee7df 0%, #b490ca 100%)',
    };
    return gradients[letter] || 'linear-gradient(135deg, #667eea 0%, #764ba2 100%)';
  });

  onDocumentClick(event: MouseEvent): void {
    if (this.menuOpen() && !this.elementRef.nativeElement.contains(event.target)) {
      this.menuOpen.set(false);
    }
  }

  onEscape(): void {
    this.menuOpen.set(false);
  }

  toggleMenu(): void {
    this.menuOpen.update(v => !v);
  }

  onNewSession(): void {
    this.menuOpen.set(false);
    this.newSessionClicked.emit();
  }

  onEdit(): void {
    this.menuOpen.set(false);
    this.editClicked.emit();
  }

  onShare(): void {
    this.menuOpen.set(false);
    this.shareClicked.emit();
  }
}
