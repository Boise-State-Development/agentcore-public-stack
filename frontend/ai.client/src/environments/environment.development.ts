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
        conversationSearch: true,
        userGrants: true,
        sessionPrewarm: true,
        clientTurnTiming: true,
        largeDocumentAttachments: true,
        folderAttachments: true,
        attachmentKb: true,
        // The `development` GitHub environment sets CDK_FOLDER_SYNC_ENABLED=true.
        folderSync: true,
        sourceProvenance: true,
        documentExtract: false,
        // The `development` GitHub environment sets CDK_HANDOFFS_ENABLED=true.
        handoffs: true,
        // The `development` GitHub environment sets CDK_SESSION_REWIND_ENABLED=true
        // and CDK_SESSION_FORK_ENABLED=true.
        sessionRewind: true,
        sessionFork: true,
        // The `development` GitHub environment sets CDK_QUOTA_REQUESTS_ENABLED=true.
        quotaRequests: true,
        // The `development` GitHub environment sets CDK_PROJECT_FILE_READINESS_ENABLED=true.
        projectFileReadiness: true,
        // Needs CDK_AWS_LIMITS_ENABLED=true in the `development` GitHub environment.
        awsLimits: true,
    } satisfies FeatureFlags,
};
