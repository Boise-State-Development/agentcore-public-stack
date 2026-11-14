// Production environment. Swapped in for environment.ts at build time
// via angular.json fileReplacements (under configurations.production).
//
// `appApiUrl: '/api'` is same-origin and fronted by CloudFront → app-api ALB
// (CloudFront's `/api/*` behavior with a path-strip Function). Same-origin
// is required for the BFF Token Handler `__Host-` cookies and eliminates
// CORS preflights on every request.
import { FeatureFlags } from './feature-flags';

export const environment = {
    appApiUrl: '/api',
    // Proactive new-build check (AppUpdateService): on tab focus, at most every
    // 10 minutes, one conditional GET of index.html (a 304 when unchanged).
    // Kill switch — chunk-failure recovery works without it.
    versionCheckEnabled: true,
    // Front-end feature switches for prod (feature-flags.ts). Keep in step with the
    // production GitHub environment's CDK_*_ENABLED variables.
    features: {
        projects: true,
        conversationSearch: true,
        userGrants: true,
        sessionPrewarm: true,
        clientTurnTiming: true,
        largeDocumentAttachments: true,
        folderAttachments: true,
        attachmentKb: true,
        folderSync: true,
        // The `production` GitHub environment sets CDK_SOURCE_PROVENANCE_ENABLED=true
        // from 1.36.0 (in development, off by default; #1798).
        sourceProvenance: true,
        // The `production` GitHub environment sets CDK_ATTACHMENT_PLAN_ENABLED=true
        // from the release that ships this (a rollout switch, #1798).
        attachmentPlan: true,
        // The `production` GitHub environment sets CDK_DOCUMENT_EXTRACT_ENABLED=true
        // and CDK_FIELD_SET_AUTHORING_ENABLED=true from 1.36.0 (in development,
        // off by default; #1798).
        documentExtract: true,
        fieldSetAuthoring: true,
        handoffs: false,
        handoffBrief: false,
        sessionRewind: true,
        sessionFork: true,
        quotaRequests: false,
        projectFileReadiness: false,
        awsLimits: false,
        knowledgeBaseMode: true,
        releaseLedger: false,
        // On in our prod from 1.36.1 (UI only; in development, off by default; #1798).
        attachmentCards: true,
    } satisfies FeatureFlags,
};
