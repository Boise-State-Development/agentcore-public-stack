import { Component, DestroyRef, computed, inject, signal } from '@angular/core';
import { Router, RouterOutlet } from '@angular/router';
import { Sidenav } from './components/sidenav/sidenav';
import { ErrorToastComponent } from './components/error-toast/error-toast.component';
import { ToastComponent } from './components/toast';
import { SidenavService } from './services/sidenav/sidenav.service';
import { HeaderService } from './services/header/header.service';
import { TooltipDirective } from './components/tooltip/tooltip.directive';
import { SessionService } from './auth/session.service';
import { ArtifactStateService } from './session/services/artifacts/artifact-state.service';

@Component({
  selector: 'app-root',
  imports: [
    RouterOutlet,
    Sidenav,
    ErrorToastComponent,
    ToastComponent,
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
  private artifactState = inject(ArtifactStateService);

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
    // Re-probe the BFF session whenever the tab regains focus. A session
    // that expired while the tab was backgrounded surfaces immediately
    // (redirect to /auth/login) instead of waiting for the next user
    // action to 401. SSR-safe via the document guard.
    if (typeof document !== 'undefined') {
      const destroyRef = inject(DestroyRef);
      const handler = () => {
        if (document.visibilityState === 'visible') {
          this.session.recheck();
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
