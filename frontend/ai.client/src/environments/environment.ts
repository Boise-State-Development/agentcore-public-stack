// Local-dev environment (`ng serve`, `ng test`). Deployed builds replace this with
// environment.development.ts (dev) or environment.production.ts (prod) via
// angular.json fileReplacements.
import { FeatureFlags } from './feature-flags';

export const environment = {
    appApiUrl: 'http://localhost:8000',
    // Proactive new-build check (AppUpdateService). Inert on the dev server,
    // whose unhashed `main.js` gives it nothing to compare.
    versionCheckEnabled: true,
    // Front-end feature switches (feature-flags.ts). In-development features are off
    // until you choose to turn them on; match your backend's *_ENABLED values.
    features: {
        projects: false,
    } satisfies FeatureFlags,
};
