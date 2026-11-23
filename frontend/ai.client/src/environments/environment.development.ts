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
        // The `development` GitHub environment sets CDK_ATTACHMENT_PLAN_ENABLED=true.
        attachmentPlan: true,
        // The `development` GitHub environment sets CDK_DOCUMENT_EXTRACT_ENABLED=true.
        documentExtract: true,
        // The `development` GitHub environment sets CDK_FIELD_SET_AUTHORING_ENABLED=true.
        fieldSetAuthoring: true,
        // Needs CDK_SKILL_BUNDLE_IMPORT_ENABLED=true in the `development` GitHub environment.
        skillBundleImport: true,
        // The `development` GitHub environment sets CDK_HANDOFFS_ENABLED=true.
        handoffs: true,
        // The `development` GitHub environment sets CDK_HANDOFF_BRIEF_ENABLED=true.
        handoffBrief: true,
        // Off until the `development` GitHub environment sets CDK_WORKFLOW_RUNS_ENABLED=true.
        workflowRuns: true,
        // The `development` GitHub environment sets CDK_WORKFLOWS_ENABLED=true.
        workflows: true,
        // The `development` GitHub environment sets CDK_PROMPT_STEPS_ENABLED=true.
        promptSteps: true,
        // The `development` GitHub environment sets CDK_CHECKLIST_STEPS_ENABLED=true.
        checklistSteps: true,
        // Default on, matching CDK_SESSION_REWIND_ENABLED / CDK_SESSION_FORK_ENABLED.
        sessionRewind: true,
        sessionFork: true,
        // The `development` GitHub environment sets CDK_QUOTA_REQUESTS_ENABLED=true.
        quotaRequests: true,
        // The `development` GitHub environment sets CDK_PROJECT_FILE_READINESS_ENABLED=true.
        projectFileReadiness: true,
        // Needs CDK_AWS_LIMITS_ENABLED=true in the `development` GitHub environment.
        awsLimits: true,
        // Needs CDK_ORGANIZE_LISTS_ENABLED=true in the `development` GitHub environment (set 2026-11-23).
        organizeLists: true,
        // Reads app-api's knowledge-base mode, which follows the Runtime's
        // CDK_ASSISTANT_KB_FULL_CONTEXT_* variables in the `development` environment.
        knowledgeBaseMode: true,
        // CDK_RELEASE_LEDGER_ENABLED is on in the `development` GitHub environment.
        releaseLedger: true,
        attachmentCards: true,
    } satisfies FeatureFlags,
};
