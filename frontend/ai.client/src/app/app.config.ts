import { ApplicationConfig, provideBrowserGlobalErrorListeners, APP_INITIALIZER } from '@angular/core';
import { provideRouter, withComponentInputBinding } from '@angular/router';

import { routes } from './app.routes';
import { provideHttpClient, withInterceptors } from '@angular/common/http';
import { authInterceptor } from './auth/auth.interceptor';
import { errorInterceptor } from './auth/error.interceptor';
import { MARKED_OPTIONS, MarkedOptions, MarkedRenderer, provideMarkdown } from 'ngx-markdown';
import { ConfigService } from './services/config.service';

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
 * Application initialization factory
 * 
 * Loads runtime configuration from /config.json before the app starts.
 * This ensures all services have access to configuration values when they initialize.
 * 
 * The initialization:
 * - Fetches config.json from the server
 * - Validates the configuration structure
 * - Falls back to environment.ts if fetch fails
 * - Allows the app to continue even if config loading fails
 * 
 * @param configService - The ConfigService instance
 * @returns Factory function that returns a Promise
 */
function initializeApp(configService: ConfigService) {
  return () => configService.loadConfig();
}

export const appConfig: ApplicationConfig = {
  providers: [
    provideBrowserGlobalErrorListeners(),
    provideHttpClient(
      withInterceptors([authInterceptor, errorInterceptor]),
    ),
    provideMarkdown({
      markedOptions: {
        provide: MARKED_OPTIONS,
        useFactory: markedOptionsFactory,
      },
    }),
    provideRouter(routes, withComponentInputBinding()),
    
    // Load runtime configuration before app starts
    {
      provide: APP_INITIALIZER,
      useFactory: initializeApp,
      deps: [ConfigService],
      multi: true
    }
  ]
};
