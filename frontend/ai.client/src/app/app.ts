import { Component, DestroyRef, computed, inject, signal } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { NavigationEnd, Router, RouterOutlet } from '@angular/router';
import { filter } from 'rxjs/operators';
import { Title } from '@angular/platform-browser';
import { Sidenav } from './components/sidenav/sidenav';
import { ErrorToastComponent } from './components/error-toast/error-toast.component';
import { ToastComponent } from './components/toast';
import { BackgroundTaskToastsComponent } from './components/background-task-toasts/background-task-toasts.component';
import { AnnouncementBannerComponent } from './components/announcement-banner/announcement-banner.component';
import { SidenavService } from './services/sidenav/sidenav.service';
import { HeaderService } from './services/header/header.service';
import { TooltipDirective } from './components/tooltip/tooltip.directive';
import { SessionService } from './auth/session.service';
import { SessionService as SessionListService } from './session/services/session/session.service';
import { ArtifactStateService } from './session/services/artifacts/artifact-state.service';
import { isMinimalChromeRoute } from './shared/utils/route-chrome';
import { BrandingService } from '../branding/branding.service';

@Component({
  selector: 'app-root',
  imports: [
    RouterOutlet,
    Sidenav,
    ErrorToastComponent,
    ToastComponent,
    BackgroundTaskToastsComponent,
    AnnouncementBannerComponent,
    TooltipDirective
  ],
  templateUrl: './app.html',
  styleUrl: './app.css',
})
export class App {
  protected readonly title = signal('boisestate.ai');
  protected sidenavService = inject(SidenavService);
  protected headerService = inject(HeaderService);
  private router = inject(Router);
  private session = inject(SessionService);
  private sessionList = inject(SessionListService);
  private artifactState = inject(ArtifactStateService);
  private titleService = inject(Title);
  private branding = inject(BrandingService);

  /** Re-read on every completed navigation; the value itself is unused,
   *  it exists so `minimalChrome` recomputes when the route changes. */
  private readonly navigated = toSignal(
    this.router.events.pipe(filter((e) => e instanceof NavigationEnd)),
    { initialValue: null },
  );

  /**
   * True when the active route asks for a stripped shell via
   * `data: { chrome: 'minimal' }`.
   *
   * Declarative on purpose. The older way to do this is an imperative
   * `sidenavService.hide()` in `ngOnInit` paired with `show()` in
   * `ngOnDestroy` (login, first-boot, not-found, agent-form all do it),
   * but that leaves the chrome hidden app-wide if the restore is ever
   * missed. Deriving it from the route means there is nothing to
   * restore: navigate away and the shell comes back on its own.
   */
  protected readonly minimalChrome = computed(() => {
    this.navigated();
    return isMinimalChromeRoute(this.router.routerState.snapshot.root);
  });

  /**
   * Whether the sidenav and its floating controls are suppressed —
   * either because a page hid them imperatively or because the active
   * route asked for a minimal shell.
   */
  protected readonly chromeHidden = computed(
    () => this.sidenavService.isHidden() || this.minimalChrome(),
  );

  /**
   * Whether the ambient announcement strip renders.
   *
   * Gated on the session, not just the chrome. `AnnouncementsService` loads
   * its feed lazily on the first read of `bannerItem()`, and `resource()`
   * loads exactly once — so instantiating the banner on the login screen
   * would fire `GET /announcements` unauthenticated, take the 401's
   * empty-feed fallback, and then never retry. The user would land in the
   * app with announcements permanently missing for the life of the tab.
   * Waiting for `isAuthenticated()` also keeps the server's role-based
   * targeting honest: it needs the session to evaluate it.
   */
  protected readonly showAnnouncementBanner = computed(
    () => this.session.isAuthenticated() && !this.minimalChrome(),
  );

  /** True while an artifact pane is docked — content reserves right-side
   *  space for it (desktop only) so the fixed panel doesn't occlude chat. */
  protected readonly artifactPanelOpen = computed(
    () => this.artifactState.openArtifact() !== null,
  );

  /** Exposed as a CSS var on the content wrapper so the desktop-only
   *  media-query rules (here and in chat-container) reserve exactly the
   *  user-chosen pane width. */
  protected readonly artifactPaneWidthCss = computed(
    () => `${this.artifactState.paneWidth()}px`,
  );

  constructor() {
    // Set page title from branding config
    this.titleService.setTitle(this.branding.pageTitle);

    // Re-probe the BFF session whenever the tab regains focus. A session
    // that expired while the tab was backgrounded surfaces immediately
    // (redirect to /auth/login) instead of waiting for the next user
    // action to 401. SSR-safe via the document guard.
    if (typeof document !== 'undefined') {
      const destroyRef = inject(DestroyRef);
      const handler = () => {
        if (document.visibilityState === 'visible') {
          this.session.recheck();
          // Surface unread dots from scheduled (server-side) runs that finished
          // while the tab was backgrounded — refetch the list on return, no poll.
          this.sessionList.refreshSessions();
        }
      };
      document.addEventListener('visibilitychange', handler);
      destroyRef.onDestroy(() => document.removeEventListener('visibilitychange', handler));
    }
  }

  newChat() {
    this.router.navigate(['']);
  }
}
