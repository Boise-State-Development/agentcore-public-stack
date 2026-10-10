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
   * Session prewarm: opening a conversation (the page load that focuses the
   * composer) starts its AgentCore Runtime microVM ahead of the first send.
   * Backend: SESSION_PREWARM_ENABLED (app-api; `POST /chat/prewarm` 404s while off).
   */
  sessionPrewarm: boolean;
}
