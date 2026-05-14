import { Component, DestroyRef, inject, signal } from '@angular/core';
import { Router, RouterOutlet } from '@angular/router';
import { Sidenav } from './components/sidenav/sidenav';
import { ErrorToastComponent } from './components/error-toast/error-toast.component';
import { ToastComponent } from './components/toast';
import { SidenavService } from './services/sidenav/sidenav.service';
import { HeaderService } from './services/header/header.service';
import { TooltipDirective } from './components/tooltip/tooltip.directive';
import { SessionService } from './auth/session.service';

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
