// Deployed dev environment. Swapped in for environment.ts by the `dev-deploy`
// build configuration (angular.json), which the dev deploy workflows build
// (SPA_BUILD_CONFIGURATION in frontend-deploy.yml and nightly-deploy-pipeline.yml).
//
// Same shape and values as environment.production.ts except `features`, which
// is where a deployment turns on work that is still in development.
import { FeatureFlags } from './feature-flags';

export const environment = {
    appApiUrl: '/api',
    versionCheckEnabled: true,
    // Front-end feature switches for the dev site (feature-flags.ts). Keep in step
    // with the development GitHub environment's CDK_*_ENABLED variables.
    features: {
        projects: true,
    } satisfies FeatureFlags,
};
