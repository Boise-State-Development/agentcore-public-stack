import {
  EnvironmentProviders,
  ErrorHandler,
  Injectable,
  Injector,
  inject,
  makeEnvironmentProviders,
  provideAppInitializer,
} from '@angular/core';
import { NavigationError } from '@angular/router';
import { environment } from '../../../environments/environment';
import { AppUpdateService } from './app-update.service';
import { isChunkLoadError } from './chunk-load-error';

/**
 * Router `withNavigationErrorHandler` callback: a navigation whose lazy chunk
 * failed to load becomes a full page load of its destination (or a refresh
 * prompt, when that is not safe). Runs in an injection context.
 */
export function recoverFromStaleChunkNavigation(event: NavigationError): void {
  if (!isChunkLoadError(event.error)) return;
  inject(AppUpdateService).recoverFromFailedNavigation(event.url);
}

/**
 * The default `ErrorHandler`, plus the chunk failures that are NOT a
 * navigation — a `@defer` block or a lazily imported library whose chunk the
 * last deploy removed. Those get the refresh prompt; the error is still
 * logged as before.
 *
 * `AppUpdateService` is resolved on first use rather than injected: the
 * `ErrorHandler` is created before most of the app, and the service's own
 * dependencies (HttpClient, the chat state) have no business being built that
 * early.
 */
@Injectable()
export class ChunkLoadErrorHandler extends ErrorHandler {
  private readonly injector = inject(Injector);

  override handleError(error: unknown): void {
    if (isChunkLoadError(error)) {
      this.injector.get(AppUpdateService).recoverFromChunkLoadError();
    }
    super.handleError(error);
  }
}

/**
 * Recover from a deploy replacing the build under an open tab. Pair with
 * `withNavigationErrorHandler(recoverFromStaleChunkNavigation)` on the router.
 */
export function provideStaleBuildRecovery(): EnvironmentProviders {
  return makeEnvironmentProviders([
    { provide: ErrorHandler, useClass: ChunkLoadErrorHandler },
    provideAppInitializer(() => {
      if (environment.versionCheckEnabled) inject(AppUpdateService).startVersionCheck();
    }),
  ]);
}
