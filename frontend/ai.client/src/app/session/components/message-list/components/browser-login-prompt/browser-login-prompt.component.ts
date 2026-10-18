import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroLockClosed } from '@ng-icons/heroicons/outline';

import {
  BrowserLoginRequest,
  BrowserLoginService,
} from '../../../../../services/browser-login/browser-login.service';
import { BrowserPaneStateService } from '../../../../services/browser-pane/browser-pane-state.service';
import {
  BROWSER_CARD_ACTION_CLASS,
  BrowserCardComponent,
  BrowserCardMenuItem,
} from '../browser-card/browser-card.component';
import { hostLabel } from '../browser-card/browser-page';

/**
 * The card for a turn the agent paused so the user can sign in to a site it
 * cannot reach (`docs/specs/authenticated-web-assessment.md`).
 *
 * Only the card lives in the conversation. The live browser opens in the
 * docked side panel (`BrowserPanelComponent`), which owns minting the
 * short-lived live-view URL, framing the sandbox viewer and re-minting before
 * the signature expires — so this component holds no URL and no iframe, and
 * nothing here can tear a stream down by re-rendering.
 *
 * There is a deadline. Past it the backend has released the browser and let
 * the session become reapable, so the card stops offering the viewer and the
 * only honest action left is to dismiss.
 */
@Component({
  selector: 'app-browser-login-prompt',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, BrowserCardComponent],
  providers: [provideIcons({ heroLockClosed })],
  host: { class: 'block' },
  template: `
    <app-browser-card
      [title]="title()"
      [host]="host()"
      [status]="status()"
      [live]="!lapsed() && canFrame()"
      [menuItems]="menuItems()"
      (menuSelect)="onMenu($event)"
    >
      <div
        browserCardPreview
        class="flex h-full flex-col items-center justify-center gap-1.5 px-6 text-center"
      >
        <span class="text-gray-400 dark:text-gray-500">
          <ng-icon name="heroLockClosed" size="20" />
        </span>
        <span
          class="line-clamp-2 text-xs/5 text-gray-600 dark:text-gray-300"
        >
          @if (lapsed()) {
            The sign-in window closed, so the browser was released.
          } @else if (!canFrame()) {
            The sign-in viewer isn't available in this environment.
          } @else {
            {{ request().reason || 'This site needs you to sign in.' }}
          }
        </span>
      </div>

      @if (lapsed()) {
        <button
          browserCardAction
          type="button"
          [class]="actionClass"
          (click)="dismiss()"
        >
          Dismiss
        </button>
      } @else if (canFrame()) {
        <button
          browserCardAction
          type="button"
          [class]="actionClass"
          [attr.aria-label]="'Open ' + title() + ' in the side panel'"
          (click)="openPanel()"
        >
          Open
        </button>
      }
    </app-browser-card>
  `,
})
export class BrowserLoginPromptComponent {
  readonly request = input.required<BrowserLoginRequest>();

  private readonly service = inject(BrowserLoginService);
  private readonly pane = inject(BrowserPaneStateService);

  protected readonly actionClass = BROWSER_CARD_ACTION_CLASS;

  /** Ticks so the deadline countdown re-evaluates without a timer per binding. */
  private readonly now = signal(Date.now());

  constructor() {
    const tick = setInterval(() => this.now.set(Date.now()), 1000);
    inject(DestroyRef).onDestroy(() => clearInterval(tick));
  }

  protected readonly lapsed = computed(() =>
    this.service.hasLapsed(this.request(), this.now()),
  );

  protected readonly canFrame = computed(() => !!this.request().sandboxOrigin);

  protected readonly isOpen = computed(
    () => this.pane.openInterruptId() === this.request().interruptId,
  );

  protected readonly host = computed(() => {
    const target = this.request().targetUrl;
    return (target && hostLabel(target)) || 'Browser';
  });

  protected readonly title = computed(() => {
    const target = this.request().targetUrl;
    const host = target ? hostLabel(target) : null;
    return host ? `Sign in to ${host}` : 'Sign in to continue';
  });

  protected readonly remainingLabel = computed<string | null>(() => {
    const deadline = this.request().deadlineAt;
    if (!deadline) return null;
    const ms = Date.parse(deadline) - this.now();
    if (!Number.isFinite(ms) || ms <= 0) return null;
    const minutes = Math.floor(ms / 60000);
    const seconds = Math.floor((ms % 60000) / 1000);
    return minutes > 0 ? `${minutes}m ${seconds}s` : `${seconds}s`;
  });

  protected readonly status = computed(() => {
    if (this.lapsed()) return 'Sign-in window closed';
    if (!this.canFrame()) return 'Viewer unavailable';
    if (this.isOpen()) return 'Open in side panel';
    const remaining = this.remainingLabel();
    return remaining ? `Waiting for you · ${remaining} left` : 'Waiting for you';
  });

  protected readonly menuItems = computed<BrowserCardMenuItem[]>(() => {
    if (this.lapsed()) return [];
    const items: BrowserCardMenuItem[] = [];
    if (this.canFrame()) items.push({ id: 'complete', label: "I've signed in" });
    items.push({ id: 'skip', label: "Don't sign in", danger: true });
    return items;
  });

  protected openPanel(): void {
    if (this.lapsed() || !this.canFrame()) return;
    this.pane.open(this.request());
  }

  protected onMenu(id: string): void {
    if (id === 'complete') void this.finish();
    if (id === 'skip') void this.dismiss();
  }

  protected async finish(): Promise<void> {
    this.closePanelIfMine();
    await this.service.complete(this.request().interruptId);
  }

  protected async dismiss(): Promise<void> {
    this.closePanelIfMine();
    await this.service.skip(this.request().interruptId);
  }

  private closePanelIfMine(): void {
    if (this.isOpen()) this.pane.close();
  }
}
