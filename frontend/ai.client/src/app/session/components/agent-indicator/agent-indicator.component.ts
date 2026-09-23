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
  heroChevronUp,
  heroUserGroup,
  heroLockClosed,
} from '@ng-icons/heroicons/outline';

/**
 * What an Agent fixes for the conversation it is bound to.
 *
 * A bound Agent governs its own model, tools and skills: the backend applies
 * them at invocation regardless of what the client sends, so the user's own
 * preferences — the ones they set in Customize — do not apply here. Before this
 * existed, that fact was only legible inside the composer's settings drawer, as
 * greyed switches. With the drawer gone (step 5 of
 * `docs/specs/customize-surface.md`) a user could toggle a skill in Customize,
 * return to an agent conversation and watch it be ignored, with nothing
 * anywhere saying why.
 *
 * `null` on any field means "not governed — the user's own setting applies".
 * The whole input defaults to null, so a surface that does not know the answer
 * (the Designer preview and the marketplace test-drive both render this
 * component without ever applying the locks) says nothing rather than guessing.
 */
export interface AgentGovernance {
  /** Display name of the model the Agent pins, or null when it pins none. */
  modelName: string | null;
  /** How many tools the Agent binds, or null when it binds none. */
  toolCount: number | null;
  /** How many skills the Agent binds, or null when it binds none. */
  skillCount: number | null;
}

/**
 * The agent bound to the conversation, as a compact chip in the composer's
 * meta row — the left-hand counterpart of the cost + context badge on the
 * right. Sized and coloured to sit at the badge's weight, so the row reads as
 * one line of quiet metadata rather than a control competing with the input.
 *
 * Opens an actions menu (New session, and for the owner Edit / Share) upward
 * and anchored to its own left edge, because it lives on the composer's left
 * edge: a centred menu would hang off the side of a narrow composer.
 */
@Component({
  selector: 'app-agent-indicator',
  imports: [NgIcon],
  providers: [
    provideIcons({
      heroPencilSquare,
      heroPlusCircle,
      heroChevronUp,
      heroUserGroup,
      heroLockClosed,
    }),
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: {
    '(document:click)': 'onDocumentClick($event)',
    '(document:keydown.escape)': 'onEscape()',
  },
  template: `
    <div class="indicator-wrapper">
      <button
        type="button"
        (click)="toggleMenu()"
        class="agent-pill"
        [class.open]="menuOpen()"
        [attr.aria-label]="'Agent: ' + name() + '. Click for options.'"
        [attr.aria-expanded]="menuOpen()"
        aria-haspopup="menu"
      >
        @if (emoji()) {
          <span class="pill-emoji" aria-hidden="true">{{ emoji() }}</span>
        } @else {
          <span class="pill-avatar" [style.background]="avatarGradient()" aria-hidden="true">
            {{ firstLetter() }}
          </span>
        }
        <span class="pill-name">{{ name() }}</span>
        @if (isGoverned()) {
          <ng-icon
            name="heroLockClosed"
            class="pill-lock"
            [attr.aria-label]="governanceLabel()"
          />
        }
        <ng-icon
          name="heroChevronUp"
          class="pill-chevron"
          [class.rotated]="menuOpen()"
          aria-hidden="true"
        />
      </button>

      @if (menuOpen()) {
        <div class="indicator-menu" role="menu" aria-label="Agent actions">
          <div class="menu-header">
            <span class="menu-title">{{ name() }}</span>
            @if (ownerName()) {
              <span class="menu-owner">by {{ ownerName() }}</span>
            }
          </div>

          @if (isGoverned()) {
            <div class="menu-governance">
              <p class="governance-title">
                <ng-icon name="heroLockClosed" class="governance-icon" aria-hidden="true" />
                <span>Fixed by this agent</span>
              </p>
              <ul class="governance-list">
                @if (governance()?.modelName; as modelName) {
                  <li><span class="governance-key">Model</span><span>{{ modelName }}</span></li>
                }
                @if (toolLabel(); as tools) {
                  <li><span class="governance-key">Tools</span><span>{{ tools }}</span></li>
                }
                @if (skillLabel(); as skills) {
                  <li><span class="governance-key">Skills</span><span>{{ skills }}</span></li>
                }
              </ul>
              <p class="governance-note">
                Your own choices in Customize don't apply in this conversation.
              </p>
            </div>
          }

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
    @reference "../../../../styles/theme.css";

    :host {
      display: inline-flex;
      min-width: 0;
      max-width: 100%;
    }

    .indicator-wrapper {
      position: relative;
      display: inline-flex;
      min-width: 0;
      max-width: 100%;
    }

    /* ── Chip ──
       Same type size and ink as the cost badge it mirrors (text-xs, gray-500),
       with a hover surface as the only hint that it is a control. */
    .agent-pill {
      display: inline-flex;
      align-items: center;
      gap: 0.375rem;
      min-width: 0;
      max-width: 100%;
      height: 1.25rem;
      padding: 0 0.375rem;
      border-radius: 0.375rem;
      font-size: 0.75rem;
      line-height: 1;
      color: var(--color-gray-500);
      cursor: pointer;
      animation: pill-enter 0.25s ease-out 0.35s backwards;
      transition: background 0.15s ease, color 0.15s ease;

      &:hover,
      &.open {
        background: var(--color-gray-200);
        color: var(--color-gray-800);
      }

      &:focus-visible {
        outline: 2px solid var(--color-primary-accessible);
        outline-offset: 1px;
      }
    }

    :host-context(html.dark) .agent-pill {
      color: var(--color-gray-400);

      &:hover,
      &.open {
        background: rgba(255, 255, 255, 0.1);
        color: var(--color-gray-100);
      }
    }

    .pill-emoji {
      font-size: 0.8125rem;
      line-height: 1;
      flex-shrink: 0;
    }

    .pill-avatar {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      width: 0.875rem;
      height: 0.875rem;
      border-radius: 0.25rem;
      font-size: 0.5625rem;
      font-weight: 700;
      color: var(--color-white);
      flex-shrink: 0;
    }

    .pill-name {
      font-weight: 500;
      white-space: nowrap;
      min-width: 0;
      max-width: 12rem;
      overflow: hidden;
      text-overflow: ellipsis;
    }

    .pill-lock,
    .pill-chevron {
      width: 0.75rem;
      height: 0.75rem;
      flex-shrink: 0;
      color: var(--color-gray-400);
    }

    .pill-chevron {
      transition: transform 0.2s ease;
    }

    .pill-chevron.rotated {
      transform: rotate(180deg);
    }

    :host-context(html.dark) .pill-lock,
    :host-context(html.dark) .pill-chevron {
      color: var(--color-gray-500);
    }

    /* ── Menu ── */
    .indicator-menu {
      position: absolute;
      bottom: calc(100% + 0.375rem);
      left: 0;
      /* Sizes to content so a governance row like "Model  Claude Sonnet 5"
         reads on one line, but never wider than the viewport allows. */
      width: max-content;
      min-width: 12rem;
      max-width: min(20rem, calc(100vw - 2rem));
      padding: 0.25rem;
      border-radius: 0.75rem;
      background: var(--color-white);
      border: 1px solid var(--color-gray-200);
      box-shadow:
        0 4px 16px rgba(0, 0, 0, 0.1),
        0 1px 4px rgba(0, 0, 0, 0.06);
      transform-origin: bottom left;
      animation: menu-enter 0.15s cubic-bezier(0.16, 1, 0.3, 1) forwards;
      z-index: 50;
    }

    :host-context(html.dark) .indicator-menu {
      background: var(--color-gray-800);
      border-color: rgba(255, 255, 255, 0.1);
      box-shadow:
        0 4px 16px rgba(0, 0, 0, 0.3),
        0 1px 4px rgba(0, 0, 0, 0.2);
    }

    /* The chip truncates a long name and drops the owner, so the menu is where
       both read in full. */
    .menu-header {
      display: flex;
      flex-direction: column;
      gap: 0.125rem;
      padding: 0.5rem 0.625rem;
      border-bottom: 1px solid var(--color-gray-200);
      margin-bottom: 0.25rem;
    }

    :host-context(html.dark) .menu-header {
      border-bottom-color: rgba(255, 255, 255, 0.1);
    }

    .menu-title {
      font-size: 0.8125rem;
      font-weight: 600;
      line-height: 1.25rem;
      color: var(--color-gray-900);
      overflow-wrap: anywhere;
    }

    .menu-owner {
      font-size: 0.6875rem;
      line-height: 1rem;
      color: var(--color-gray-500);
    }

    :host-context(html.dark) .menu-title {
      color: var(--color-gray-100);
    }

    :host-context(html.dark) .menu-owner {
      color: var(--color-gray-400);
    }

    .menu-governance {
      padding: 0.25rem 0.625rem 0.625rem;
      border-bottom: 1px solid var(--color-gray-200);
      margin-bottom: 0.25rem;
    }

    :host-context(html.dark) .menu-governance {
      border-bottom-color: rgba(255, 255, 255, 0.1);
    }

    .governance-title {
      display: flex;
      align-items: center;
      gap: 0.375rem;
      font-size: 0.6875rem;
      font-weight: 600;
      letter-spacing: 0.02em;
      text-transform: uppercase;
      color: var(--color-gray-500);
    }

    .governance-icon {
      width: 0.75rem;
      height: 0.75rem;
      flex-shrink: 0;
    }

    .governance-list {
      margin: 0.375rem 0 0;
      padding: 0;
      list-style: none;
      display: grid;
      gap: 0.125rem;
      font-size: 0.75rem;
      line-height: 1.25rem;
      color: var(--color-gray-700);
    }

    .governance-list li {
      display: flex;
      gap: 0.5rem;
      /* The value is the interesting half: let a long model name truncate
         rather than wrap the label away from it. */
      min-width: 0;
    }

    .governance-list li > :not(.governance-key) {
      min-width: 0;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }

    .governance-key {
      flex-shrink: 0;
      min-width: 2.75rem;
      color: var(--color-gray-500);
    }

    .governance-note {
      margin-top: 0.375rem;
      font-size: 0.6875rem;
      line-height: 1rem;
      color: var(--color-gray-500);
    }

    :host-context(html.dark) .governance-list {
      color: var(--color-gray-300);
    }

    :host-context(html.dark) .governance-title,
    :host-context(html.dark) .governance-key,
    :host-context(html.dark) .governance-note {
      color: var(--color-gray-400);
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
        outline: 2px solid var(--color-primary-accessible);
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

    /* Matches the cost badge's entrance so the two ends of the meta row
       arrive together. */
    @keyframes pill-enter {
      from {
        opacity: 0;
        transform: translateY(6px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }

    @keyframes menu-enter {
      0% {
        opacity: 0;
        transform: translateY(4px) scale(0.96);
      }
      100% {
        opacity: 1;
        transform: translateY(0) scale(1);
      }
    }
  `],
})
export class AgentIndicatorComponent {
  private elementRef = inject(ElementRef);

  // Inputs
  readonly name = input.required<string>();
  readonly emoji = input<string>('');
  readonly ownerName = input<string>('');
  readonly isOwner = input<boolean>(false);
  /**
   * What this Agent fixes for the conversation. Null (the default) means the
   * caller does not know — say nothing rather than guess. See `AgentGovernance`.
   */
  readonly governance = input<AgentGovernance | null>(null);

  // Outputs
  readonly newSessionClicked = output<void>();
  readonly editClicked = output<void>();
  readonly shareClicked = output<void>();

  // Menu state
  readonly menuOpen = signal(false);

  /** True when the Agent fixes at least one of model / tools / skills. */
  readonly isGoverned = computed(() => {
    const g = this.governance();
    if (!g) return false;
    return !!g.modelName || !!g.toolCount || !!g.skillCount;
  });

  /** "4 tools", or null when tools are the user's own to choose. */
  readonly toolLabel = computed(() => countLabel(this.governance()?.toolCount ?? null, 'tool'));

  /** "2 skills", or null when skills are the user's own to choose. */
  readonly skillLabel = computed(() => countLabel(this.governance()?.skillCount ?? null, 'skill'));

  /**
   * Accessible name for the lock glyph. The glyph is the only governance cue on
   * the collapsed chip, so it names what is fixed rather than just saying
   * "locked" — a screen-reader user should not have to open the menu to learn
   * which of their settings this conversation overrides.
   */
  readonly governanceLabel = computed(() => {
    const g = this.governance();
    if (!g) return '';
    const parts: string[] = [];
    if (g.modelName) parts.push(`model ${g.modelName}`);
    const tools = this.toolLabel();
    if (tools) parts.push(tools);
    const skills = this.skillLabel();
    if (skills) parts.push(skills);
    if (parts.length === 0) return '';
    return `This agent fixes ${joinList(parts)} for this conversation.`;
  });

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

/** `3` + `'tool'` -> `'3 tools'`; 0 and null both mean "not governed". */
function countLabel(count: number | null, noun: string): string | null {
  if (!count) return null;
  return `${count} ${noun}${count === 1 ? '' : 's'}`;
}

/** `['a','b','c']` -> `'a, b and c'`. */
function joinList(parts: string[]): string {
  if (parts.length === 1) return parts[0];
  return `${parts.slice(0, -1).join(', ')} and ${parts[parts.length - 1]}`;
}
