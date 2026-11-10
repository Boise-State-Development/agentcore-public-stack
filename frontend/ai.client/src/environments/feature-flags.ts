/**
 * Front-end feature switches, one value per build (see CLAUDE.md "Feature flags").
 *
 * Each `environment*.ts` sets them, and `angular.json` picks the file per build:
 *
 *   environment.ts              `ng serve` / `ng test` (local development)
 *   environment.development.ts  `dev-deploy` configuration: the deployed dev site
 *   environment.production.ts   `production` configuration: prod (and a local `ng build`)
 *
 * `scripts/frontend/build.sh` builds `SPA_BUILD_CONFIGURATION`, which each deploy
 * workflow sets to match the environment it deploys to.
 *
 * A feature in development is `false` here until a deployment chooses to turn it on,
 * and its backend switch (an `*_ENABLED` env var, set by CDK from a GitHub environment
 * variable) is opt-in the same way. The backend is the real gate; these only decide
 * what the UI offers, so keep the two in step per environment.
 *
 * Read them through the `FEATURES` token (`app/services/features.ts`), never by
 * importing an environment file from a component.
 */
export interface FeatureFlags {
  /** Shared Projects: nav item, /projects routes, notification bell. Backend: PROJECTS_ENABLED. */
  projects: boolean;
  /**
   * Conversation search: the Cmd/Ctrl+K search dialog and the sidenav search button.
   * On by default; a build that sets it false has no conversation search in the
   * sidebar. Backend: CONVERSATION_SEARCH_ENABLED (`GET /sessions/search` 404s
   * only when it is "false").
   */
  conversationSearch: boolean;
  /**
   * Direct user grants: the Access section on the admin user detail page, where a
   * system admin grants tools, models and skills to one user beside their roles.
   * Backend: USER_GRANTS_ENABLED (app-api; `/admin/user-grants` 404s while off).
   */
  userGrants: boolean;
  /**
   * Session prewarm: a new conversation's page load, or the first keystroke in an
   * existing one, starts its AgentCore Runtime microVM ahead of the send.
   * Backend: SESSION_PREWARM_ENABLED (app-api; `POST /chat/prewarm` 404s while off).
   */
  sessionPrewarm: boolean;
  /**
   * Client-measured time to first token: the SPA reports each turn's
   * send-to-first-token. No UI. Backend: CLIENT_TURN_TIMING_ENABLED (app-api;
   * `POST /telemetry/turn-timing` 404s while off).
   */
  clientTurnTiming: boolean;
  /**
   * Large document attachments: pdf, docx, txt, md and html files up to 50 MB
   * (instead of 4 MB) are accepted and attached by reference, with a "Large
   * document, read on demand" chip. Backend: LARGE_DOCUMENT_ATTACHMENTS_ENABLED
   * (app-api; presign refuses a document over 4 MB while off).
   */
  largeDocumentAttachments: boolean;
  /**
   * Folder attachments (docs/specs/drive-folder-attachments.md): "Attach this
   * folder" in the composer's Drive browser, the folder card, and the folder chip
   * on a sent message. Backend: FOLDER_ATTACHMENTS_ENABLED on app-api (the
   * routes) and the AgentCore Runtime (the manifest); keep them in step.
   */
  folderAttachments: boolean;
  /**
   * Attachments KB: a large document's chip follows its search index
   * ("Indexing…", then "Searchable"), polled from `GET /files/{id}/search-status`.
   * Only meaningful with `largeDocumentAttachments`. Backend: ATTACHMENT_KB_ENABLED
   * (app-api and the Runtime; the endpoint answers `off` while it is off).
   */
  attachmentKb: boolean;
  /**
   * Folder sync (docs/specs/assistant-kb-sync.md §11): Save as project offers
   * "Keep in sync with the folder" with an interval, and a project's Files page
   * lists the folders it is kept in step with. Only meaningful with `projects`
   * and `folderAttachments`. Backend: FOLDER_SYNC_ENABLED (app-api refuses a
   * folder schedule while off; the kb-sync Lambdas carry it too).
   */
  folderSync: boolean;
  /**
   * Source provenance verification
   * (docs/specs/source-provenance-verification.md): a cited value is rendered
   * with a page reference only where the backend could confirm the passage is
   * in the document AND that the value appears in it; a located passage that
   * does not support its value renders as the warning it is. Backend:
   * SOURCE_PROVENANCE_ENABLED (the Runtime builds `verify_sources` and emits
   * `source_provenance`; app-api replays it). In development, so `false`
   * everywhere until a deployment turns both sides on.
   */
  sourceProvenance: boolean;

  /**
   * The attachment planner's `token` profile
   * (docs/specs/unified-attachment-delivery.md phase 3): attachments are decided
   * in tokens against a per-model document budget, and the composer's limits
   * come from `GET /files/limits`, which reports the active profile. Backend:
   * ATTACHMENT_PLAN_ENABLED (the Runtime plans the turn; app-api serves the
   * limits). Nothing in the UI branches on it yet: it is here so the two sides
   * stay in step as phase 4 changes what the composer offers. In development,
   * so `false` everywhere except deployed dev.
   */
  attachmentPlan: boolean;

  /**
   * Structured extraction: a named field set run over a document as typed rows
   * (docs/specs/structured-extraction.md). The UI's share is the rows table —
   * each cell showing its source-provenance state in the same vocabulary
   * `sourceProvenance` established, plus the `schemaEnforced` disclosure when
   * the typed schema was requested rather than guaranteed. Backend:
   * DOCUMENT_EXTRACT_ENABLED (the Runtime mounts `document_extract`, extracts,
   * verifies and emits `document_extraction`; app-api replays the `EXTR#`
   * rows). The SPA renders those rows as a table under the answer, with each
   * value's provenance state. In development: on only in the deployed dev
   * build, whose backend is on; off locally and in prod.
   */
  documentExtract: boolean;

  /**
   * Handoffs (docs/specs/agent-handoffs-and-consults.md, Phase 0): a mid-thread
   * `@`-mention opens a review sheet instead of sending, then starts the Agent's
   * conversation in the background while the user stays put, tracked by a
   * handoff row in the thread. Off, a mid-thread mention sends straight into a
   * new conversation, as before. Backend: HANDOFFS_ENABLED (app-api serves
   * `/sessions/{id}/handoffs`; the Runtime takes `handoff_context`). In
   * development, so `false` unless the environment has turned the backend on.
   */
  handoffs: boolean;

  /**
   * Handoff brief (docs/specs/agent-handoffs-and-consults.md §4.5.1): once the
   * review sheet has its excerpt, ask the source conversation's own model for
   * a brief, read from its prompt cache, and stream it into the context card.
   * The excerpt stays as the fallback. Off, the card is the excerpt, as before.
   * Only meaningful with `handoffs`. Backend: HANDOFF_BRIEF_ENABLED (the
   * Runtime). In development, so `false` unless the environment has turned
   * the backend on.
   */
  handoffBrief: boolean;

  /**
   * Conversation rewind (docs/specs/conversation-rewind-and-fork.md §4): a
   * user message offers "Rewind to here", which hides it and everything after
   * it and puts its text back in the composer to edit and resend. Backend:
   * SESSION_REWIND_ENABLED (app-api's `POST /sessions/{id}/rewind` 404s while
   * off; the Runtime applies the cut). In development, so `false` everywhere
   * until a deployment turns both sides on.
   */
  sessionRewind: boolean;

  /**
   * Conversation fork (docs/specs/conversation-rewind-and-fork.md §7): a user
   * message offers "Fork from here", which starts a new conversation holding
   * everything before it, with its text in the new composer. The parent stays
   * as it is. Backend: SESSION_FORK_ENABLED (app-api's `POST /sessions/{id}/fork`
   * 404s while off; the Runtime seeds the fork's first turn from the parent's
   * live history). In development, so `false` everywhere.
   */
  sessionFork: boolean;

  /**
   * Quota requests (docs/specs/quota-requests-and-extensions.md §5): a user
   * asks for more quota from the composer's quota tab, the blocked state or
   * Settings → Usage, sees their requests and any granted extensions there,
   * answers an admin's request for a funding source, and the notification bell
   * shows with it. Backend: QUOTA_REQUESTS_ENABLED (app-api; `/quota/*` 404s
   * while off). In development, so `false` everywhere until a deployment turns
   * both sides on.
   */
  quotaRequests: boolean;
  /**
   * Project file readiness: a strip above a project chat's composer while any of
   * the project's files are still being prepared ("Preparing 3 of 11 project
   * files…"), "All 11 files ready" once they are, and a note on files that
   * couldn't be added. It reads the project's file list
   * (`GET /projects/{id}/knowledge`), so it needs nothing new from app-api. Its
   * backend partner is PROJECT_FILE_READINESS_ENABLED on the AgentCore Runtime,
   * which tells the model the same thing per turn. In development, so `false`
   * until a deployment turns it on.
   */
  projectFileReadiness: boolean;

  /**
   * AWS Limits admin page (GitHub issue #12): the account's Service Quotas for
   * the services this stack uses, with usage and increase requests, under
   * Usage & Spend. Backend: AWS_LIMITS_ENABLED (app-api; `/admin/aws-limits`
   * 404s while off). In development, so `false` locally and in prod.
   */
  awsLimits: boolean;

  /**
   * Knowledge-base mode line: one read-only line on the Agent Designer's
   * knowledge-base card saying whether the knowledge base is sent to the model
   * whole (full-context mode) or searched per message, and why. Reads
   * `GET /assistants/{id}/knowledge-base/mode` (app-api, owner/editor), which is
   * computed by the same code the AgentCore Runtime decides with, under its
   * ASSISTANT_KB_FULL_CONTEXT_ENABLED and ASSISTANT_KB_FULL_CONTEXT_MAX_TOKENS.
   * In development, so `false` locally and in prod.
   */
  knowledgeBaseMode: boolean;

  /**
   * Release ledger (docs/specs/release-ledger.md §6.4): the Releases tab on
   * `/admin/costs`, which compares a version or build with the one before it
   * (cost, cache, waste, tools, agents, projects and the quality panel) from
   * `GET /admin/costs/ledger/*`. Backend: RELEASE_LEDGER_ENABLED (app-api; the
   * ledger routes 404 while off). In development, so `false` locally and in prod.
   */
  releaseLedger: boolean;

  /**
   * Attachment cards: the composer's and a sent message's files drawn with the
   * browser card's shape (a tray holding the file's preview, then its name and
   * `TYPE · size · status`), laid out as a rail that folds into a summary past
   * five files in the composer, and as a mosaic with a "+N" tile in a message.
   * UI only: no backend switch, since every route it reads already exists.
   * In development while it lands over several PRs (images and folders join
   * next), so `false` locally and in prod, `true` on dev.
   */
  attachmentCards: boolean;
}
