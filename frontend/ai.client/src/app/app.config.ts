import { ApplicationConfig, inject, provideAppInitializer, provideBrowserGlobalErrorListeners } from '@angular/core';
import { provideRouter, withComponentInputBinding } from '@angular/router';

import { routes } from './app.routes';
import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { csrfInterceptor } from './auth/csrf.interceptor';
import { errorInterceptor } from './auth/error.interceptor';
import { withCredentialsInterceptor } from './auth/with-credentials.interceptor';
import { MARKED_OPTIONS, MarkedOptions, MarkedRenderer, provideMarkdown } from 'ngx-markdown';
import { SessionService } from './auth/session.service';

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
    // GET ${appApiUrl}/auth/session — on 401, SessionService redirects to the
    // BFF's /auth/login (Cognito Hosted UI) and hangs the promise so no SPA
    // route renders before the page tears down. Transport errors leave the
    // SPA in a clean unauthenticated state without redirecting.
    provideAppInitializer(() => inject(SessionService).bootstrap()),
  ]
};
