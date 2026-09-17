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
import { AnnouncementModalService } from './services/announcements/announcement-modal.service';
import { ConfigService } from './services/config.service';
import { durableDownloadUrlFromHref } from './shared/utils/file-download-url';
import { installLazyMermaid } from './shared/utils/lazy-mermaid';
import { installKatexMathExtensions } from './shared/utils/katex-math-markdown';

function markedOptionsFactory(config: ConfigService): MarkedOptions {
  const renderer = new MarkedRenderer();
  const renderLink = renderer.link;

  renderer.link = function (link) {
    // A raw user-files S3 URL in assistant prose is always broken. The model
    // reads the same tool-result JSON the download card does, and when that
    // JSON carried a presigned URL it would compose its own "[Download](...)"
    // link from it — truncated at the `?`, so the signature was gone and S3
    // answered AccessDenied while the card's own button worked. The backend no
    // longer puts signed URLs in tool results; this rewrite heals the links
    // already persisted in conversations, routing them to the durable
    // `/files/{uploadId}/download` endpoint.
    const durable = durableDownloadUrlFromHref(config.appApiUrl(), link.href);
    const html = renderLink.call(this, durable ? { ...link, href: durable } : link);
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
        deps: [ConfigService],
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

    // AnnouncementModalService owns the §D8 turn-safety gate and opens the
    // announcement modal itself. It is started here rather than mounted in
    // app.html because a CDK overlay is not a layout element — and because
    // nothing else would ever inject it. Same pattern as ThemeService above.
    provideAppInitializer(() => { inject(AnnouncementModalService); }),

    // ngx-markdown's `mermaid` plugin reads the library off the global scope
    // and throws if it isn't there. Publishing a stand-in before the first
    // markdown renders lets the real 3.57 MB library stay in a lazy chunk that
    // is only fetched when a message actually contains a diagram.
    provideAppInitializer(() => { installLazyMermaid(); }),

    // marked reads `\(` as an escaped paren and drops the backslash, so
    // LaTeX's inline-math delimiters never reached KaTeX. These tokenizers
    // claim the span first and pass the delimiters through verbatim.
    provideAppInitializer(() => { installKatexMathExtensions(); }),
  ]
};
