import { InjectionToken } from '@angular/core';
import { environment } from '../../environments/environment';
import { FeatureFlags } from '../../environments/feature-flags';

/**
 * This build's front-end feature switches (see `environments/feature-flags.ts`).
 *
 * A compile-time constant from the environment file, so the UI knows at once
 * whether to offer a feature: no request, nothing that pops in after a load.
 * Specs provide their own value.
 */
export const FEATURES = new InjectionToken<Readonly<FeatureFlags>>('FEATURES', {
  providedIn: 'root',
  factory: () => environment.features,
});

export type { FeatureFlags };
