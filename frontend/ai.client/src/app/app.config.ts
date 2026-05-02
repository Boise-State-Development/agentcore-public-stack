import { ApplicationConfig, provideBrowserGlobalErrorListeners, APP_INITIALIZER } from '@angular/core';
import { provideRouter, withComponentInputBinding } from '@angular/router';

import { routes } from './app.routes';
import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { authInterceptor } from './auth/auth.interceptor';
import { csrfInterceptor } from './auth/csrf.interceptor';
import { errorInterceptor } from './auth/error.interceptor';
import { MARKED_OPTIONS, MarkedOptions, MarkedRenderer, provideMarkdown } from 'ngx-markdown';
import { ConfigService } from './services/config.service';
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

/**
 * Application initialization factory.
 *
 * Two things must happen before the SPA is allowed to start rendering:
 *
 *   1. `ConfigService.loadConfig()` fetches `/config.json` so every
 *      service can read `appApiUrl`, `cognitoDomainUrl`, etc. without
 *      racing the bootstrap.
 *   2. `SessionService.bootstrap()` calls `GET ${appApiUrl}/auth/session`
 *      to establish the BFF cookie session. On 401 it redirects to the
 *      BFF's `/auth/login`, which routes to Cognito Hosted UI — this is
 *      the new Phase 6c login entry point.
 *
 * They MUST run sequentially: `bootstrap()` reads `appApiUrl` out of
 * `ConfigService` to compose the session URL. Multiple `APP_INITIALIZER`
 * providers run in parallel via `Promise.all`, so we collapse both
 * steps into a single factory that awaits them in order.
 *
 * Failure handling matches the dormant-Phase-5 contract on
 * `SessionService.bootstrap()`: HTTP errors leave the SPA in a clean
 * unauthenticated state without redirecting (so a transient outage
 * doesn't kick everyone out); 401 specifically does redirect to the
 * BFF login.
 */
function initializeApp(configService: ConfigService, sessionService: SessionService) {
  return async () => {
    await configService.loadConfig();
    await sessionService.bootstrap();
  };
}

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideHttpClient(
      // Order matters: csrfInterceptor runs after authInterceptor so the
      // Bearer fallback (still present for the rollback bundle) is
      // attached first; CSRF then layers on top for the cookie path.
      // errorInterceptor stays last so it sees the final response.
      withInterceptors([authInterceptor, csrfInterceptor, errorInterceptor]),
    ),
    provideMarkdown({
      markedOptions: {
        provide: MARKED_OPTIONS,
        useFactory: markedOptionsFactory,
      },
    }),
    provideRouter(routes, withComponentInputBinding()),

    // Load runtime configuration AND bootstrap the BFF session before the
    // first component renders. See `initializeApp` for the chaining rationale.
    {
      provide: APP_INITIALIZER,
      useFactory: initializeApp,
      deps: [ConfigService, SessionService],
      multi: true
    }
  ]
};
