import { Injectable, signal } from '@angular/core';
import { environment } from '../../environments/environment';

/**
 * Thin accessor over the build-time `environment.appApiUrl` value.
 *
 * Pre-Phase-7 this was a runtime-config loader that fetched `/config.json`
 * at startup. After the Phase 7 follow-up cleanup the only field still
 * worth keeping is `appApiUrl`, and that's a constant per build (`/api`
 * in production via fileReplacements, `http://localhost:8000` in dev).
 *
 * Kept as an injectable signal-bearing service so the ~50 consumer call
 * sites (`${this.config.appApiUrl()}/foo`) don't need to change.
 */
@Injectable({ providedIn: 'root' })
export class ConfigService {
  readonly appApiUrl = signal(environment.appApiUrl);
}
