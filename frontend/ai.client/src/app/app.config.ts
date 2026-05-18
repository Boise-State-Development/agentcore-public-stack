import { ApplicationConfig, inject, provideAppInitializer, provideBrowserGlobalErrorListeners } from '@angular/core';
import { provideRouter, withComponentInputBinding } from '@angular/router';

import { routes } from './app.routes';
import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { csrfInterceptor } from './auth/csrf.interceptor';
import { errorInterceptor } from './auth/error.interceptor';
import { withCredentialsInterceptor } from './auth/with-credentials.interceptor';
import { MARKED_OPTIONS, MarkedOptions, MarkedRenderer, provideMarkdown } from 'ngx-markdown';
import { SessionService } from './auth/session.service';
import { ThemeService } from './components/topnav/components/theme-toggle/theme.service';
import { provideBuiltInToolRenderers } from './session/components/message-list/components/tool-use/built-in-renderers';

function markedOptionsFactory(): MarkedOptions {
  const renderer = new MarkedRenderer();
  const renderLink = renderer.link;

  renderer.link = function (link) {
    const html = renderLink.call(this, link);
    return html.replace(/^<a /, '<a target="_blank" rel="noopener noreferrer" ');
  };

  return { renderer };
}

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideHttpClient(
      // withCredentialsInterceptor flips the cookie-attaching flag on app-api
      // requests (cross-origin in local dev; no-op same-origin in prod).
      // csrfInterceptor attaches the X-CSRF-Token header on unsafe-method
      // requests; errorInterceptor stays last so it sees the final response.
      withInterceptors([withCredentialsInterceptor, csrfInterceptor, errorInterceptor]),
    ),
    provideMarkdown({
      markedOptions: {
        provide: MARKED_OPTIONS,
        useFactory: markedOptionsFactory,
      },
    }),
    provideRouter(routes, withComponentInputBinding()),

    // Bootstrap the BFF cookie session before the first component renders.
    // GET ${appApiUrl}/auth/session — on 401, SessionService sends the browser
    // to the SPA's /auth/login page (with a returnUrl) and hangs the promise
    // so no protected route renders before the page tears down. If we're
    // already on /auth/login the bootstrap resolves and the page renders so
    // the user can pick a provider. Transport errors leave the SPA in a clean
    // unauthenticated state without redirecting.
    provideAppInitializer(() => inject(SessionService).bootstrap()),

    // ThemeService applies the persisted/system theme to <html> in its
    // constructor. It's providedIn:'root' but only injected by the topnav
    // and authed pages, so on a cold load to /auth/login or /auth/first-boot
    // it would never run and the dark-mode CSS on those screens would sit
    // dormant. Inject it at bootstrap so the lava-lamp backdrop honors the
    // user's preference (and prefers-color-scheme) on every route.
    provideAppInitializer(() => { inject(ThemeService); }),

    // Register the built-in tool-result renderers (text/JSON/image default
    // plus the migrated proof-point renderers) into the renderer registry
    // before the first message renders.
    provideBuiltInToolRenderers(),
  ]
};
