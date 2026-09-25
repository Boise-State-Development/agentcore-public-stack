import { inject } from '@angular/core';
import { Routes } from '@angular/router';
import { authGuard } from './auth/auth.guard';
import { adminGuard } from './auth/admin.guard';
import { firstBootGuard } from './auth/first-boot.guard';
import { legacyMigrationHostGuard } from './shared/utils/legacy-migration-host';
import { FEATURES } from './services/features';

/** Matches only in a build with Shared Projects on (compile-time; see environments/feature-flags.ts). */
const projectsEnabled = () => inject(FEATURES).projects;

export const routes: Routes = [
    {
        path: '',
        loadComponent: () => import('./session/session.page').then(m => m.ConversationPage),
        canActivate: [authGuard],
    },
    {
        path: 's/:sessionId',
        loadComponent: () => import('./session/session.page').then(m => m.ConversationPage),
        canActivate: [authGuard],
    },
    {
        path: 'auth/first-boot',
        loadComponent: () => import('./auth/first-boot/first-boot.page').then(m => m.FirstBootPage),
        canActivate: [firstBootGuard],
    },
    {
        path: 'shared/:shareId',
        loadComponent: () => import('./shared/shared-view.page').then(m => m.SharedViewPage),
        canActivate: [authGuard],
    },
    // Recipient view for a shared artifact. Behind authGuard like every
    // other share surface: "public" means any authenticated tenant user,
    // never anonymous. The share's own ACL is enforced server-side on
    // top of this.
    {
        path: 'shared-artifact/:shareId',
        loadComponent: () =>
            import('./shared/artifact/shared-artifact-view.page').then(
                m => m.SharedArtifactViewPage,
            ),
        canActivate: [authGuard],
        // A recipient opened a link to view one thing. Drop the sidenav
        // and the centred content box so the artifact fills the shell —
        // the app reads this in `app.html`.
        data: { chrome: 'minimal' },
    },
    {
        path: 'auth/login',
        loadComponent: () => import('./auth/login/login.page').then(m => m.LoginPage),
    },
    {
        path: 'admin',
        loadComponent: () => import('./admin/admin.layout').then(m => m.AdminLayout),
        canActivate: [adminGuard],
        loadChildren: () => import('./admin/admin.routes').then(m => m.adminRoutes),
        // Declared once on the parent, and read by `resolveRouteChrome`'s
        // deepest-declared-wins walk, so every child inherits it without
        // repeating the flag 30 times. The shell answers it two ways: the
        // sidenav swaps its body for the admin nav, and the content box drops
        // its `max-w-7xl` cap so the console's tables get the full width.
        data: { chrome: 'admin' },
    },
    // ── Assistant deprecation (Designer Phase 5) ────────────────────────────────────
    // There is one noun, and it is Agent (Marketplace D1). The Designer reached parity
    // and then passed it — bindings, icons, listings, pins, `@`-mention and reports all
    // exist only on the Agent surface — so the old editor had strictly less to offer for
    // the same record.
    //
    // The two **deep** links stay redirects rather than deletions: `/assistants/:id/edit`
    // is in people's bookmarks, in old chat sessions' "edit" links and in links colleagues
    // have shared with each other. The ids are identical on both sides (the compat mapping
    // renders a legacy Assistant *as* an Agent — there was no data migration), so the
    // redirect lands on the same record. Removing them would turn every one of those into
    // a 404 for no gain. They stay *silent* for the same reason they exist: those URLs are
    // an intent ("edit this record"), and interrupting an intent with an announcement is
    // hostile.
    {
        path: 'assistants/new',
        redirectTo: 'agents/new',
        pathMatch: 'full',
    },
    {
        path: 'assistants/:id/edit',
        redirectTo: 'agents/:id/edit',
        pathMatch: 'full',
    },
    {
        // The **list** URL is different: it is the one people browse to, and a silent
        // redirect answers the routing question while leaving the human one — where did my
        // assistants go — entirely unanswered. So it renders the explainer instead, which
        // says what changed, that nothing was lost, and what the Agent surface adds. Every
        // path out of it lands on `/agents`.
        //
        // ⚠️ TEMPORARY host gate: the explainer only renders on the production apex, where
        // people arriving off the previous version of the site have that question. Everywhere
        // else `legacyMigrationHostGuard` restores the old silent redirect onto `/agents`.
        // See `shared/utils/legacy-migration-host.ts`.
        path: 'assistants',
        loadComponent: () => import('./agents/migration/agents-migration.page').then(m => m.AgentsMigrationPage),
        canActivate: [authGuard, legacyMigrationHostGuard],
        pathMatch: 'full',
    },
    {
        path: 'agents/new',
        loadComponent: () => import('./agents/agent-form/agent-form.page').then(m => m.AgentFormPage),
        canActivate: [authGuard],
    },
    {
        path: 'agents/:id/edit',
        loadComponent: () => import('./agents/agent-form/agent-form.page').then(m => m.AgentFormPage),
        canActivate: [authGuard],
    },
    {
        // Marketplace Discover (spec phase 2). Sits under the same preview gate as the
        // rest of /agents — the sidenav entry is system-admin only until Agents are
        // unveiled, so this is not user-visible yet.
        path: 'agents/discover',
        loadComponent: () => import('./agents/discover/discover.page').then(m => m.AgentDiscoverPage),
        canActivate: [authGuard],
    },
    {
        // Marketplace Pinned tab (spec phase 5). Declared with the other literal
        // `agents/*` paths, above `agents/:id`, for the same reason.
        path: 'agents/pinned',
        loadComponent: () => import('./agents/pinned/pinned.page').then(m => m.AgentPinnedPage),
        canActivate: [authGuard],
    },
    {
        // Marketplace detail (spec phase 3). Declared AFTER `agents/discover` so the
        // literal path is not captured by `:id`, and after `agents/:id/edit` so the
        // deeper route still wins. `id` binds to the page's `input.required` via
        // `withComponentInputBinding()`.
        path: 'agents/:id',
        loadComponent: () => import('./agents/detail/agent-detail.page').then(m => m.AgentDetailPage),
        canActivate: [authGuard],
    },
    {
        path: 'agents',
        loadComponent: () => import('./agents/agents.page').then(m => m.AgentsPage),
        canActivate: [authGuard],
    },
    {
        // Shared Projects (shared-projects §6). The tab is part of the URL so a link can
        // land on Members or Settings; the bare project URL opens its Overview.
        // `projectsEnabled` keeps every /projects URL unmatched (→ not found) in a build
        // that has Projects off (src/environments/feature-flags.ts).
        path: 'projects/:id/:tab',
        loadComponent: () => import('./projects/detail/project-detail.page').then(m => m.ProjectDetailPage),
        canMatch: [projectsEnabled],
        canActivate: [authGuard],
    },
    {
        // No guard here (Angular runs redirects before guards): the redirect target
        // is guarded, so with Projects off this still ends at not found.
        path: 'projects/:id',
        redirectTo: 'projects/:id/overview',
    },
    {
        path: 'projects',
        loadComponent: () => import('./projects/projects.page').then(m => m.ProjectsPage),
        canMatch: [projectsEnabled],
        canActivate: [authGuard],
    },
    {
        path: 'schedules/new',
        loadComponent: () => import('./schedules/schedule-form/schedule-form.page').then(m => m.ScheduleFormPage),
        canActivate: [authGuard],
    },
    {
        path: 'schedules/:scheduleId/edit',
        loadComponent: () => import('./schedules/schedule-form/schedule-form.page').then(m => m.ScheduleFormPage),
        canActivate: [authGuard],
    },
    {
        path: 'schedules',
        loadComponent: () => import('./schedules/schedules.page').then(m => m.SchedulesPage),
        canActivate: [authGuard],
    },
    // `/my-skills` was absorbed into `/customize/skills` — one noun, one place.
    // Redirects rather than deletions: the paths are in bookmarks, and the skill
    // detail page linked out to `/my-skills/:id/edit` for its whole life.
    {
        path: 'my-skills/new',
        redirectTo: 'customize/skills/new',
        pathMatch: 'full',
    },
    {
        path: 'my-skills/:skillId/edit',
        redirectTo: 'customize/skills/:skillId/edit',
        pathMatch: 'full',
    },
    {
        path: 'my-skills',
        redirectTo: 'customize/skills',
        pathMatch: 'full',
    },
    // ── Customize ───────────────────────────────────────────────────────────────
    // The capabilities hub: what the user adds to their assistant. Tools and
    // Skills in PR-1; Connectors folds in from `Settings → Connectors` in step 2.
    // Deliberately NOT the Agent Marketplace — an Agent is something you talk to,
    // not a capability you toggle, and splitting that noun across two surfaces is
    // the failure Marketplace D1 exists to prevent.
    // See `docs/specs/customize-surface.md`.
    {
        path: 'customize/tools',
        loadComponent: () =>
            import('./customize/tools/customize-tools.page').then(m => m.CustomizeToolsPage),
        canActivate: [authGuard],
    },
    // One tool: its sub-tools, prompts, resources and catalog facts. The id is
    // bound straight to the page's `toolId` input by `withComponentInputBinding()`.
    {
        path: 'customize/tools/:toolId',
        loadComponent: () =>
            import('./customize/tools/customize-tool-detail.page').then(
                m => m.CustomizeToolDetailPage,
            ),
        canActivate: [authGuard],
    },
    {
        path: 'customize/skills',
        loadComponent: () =>
            import('./customize/skills/customize-skills.page').then(m => m.CustomizeSkillsPage),
        canActivate: [authGuard],
    },
    // ⚠️ ORDER: `new` must stay ABOVE `:skillId`. The router matches in
    // declaration order, so a `:skillId` route declared first swallows this one
    // and the create form renders as "skill not found" for an id of "new".
    {
        path: 'customize/skills/new',
        loadComponent: () =>
            import('./customize/skills/skill-form.page').then(m => m.SkillFormPage),
        canActivate: [authGuard],
    },
    {
        path: 'customize/skills/:skillId/edit',
        loadComponent: () =>
            import('./customize/skills/skill-form.page').then(m => m.SkillFormPage),
        canActivate: [authGuard],
    },
    // One skill: its SKILL.md body, supporting files and catalog facts. The id
    // is bound straight to the page's `skillId` input by
    // `withComponentInputBinding()`.
    {
        path: 'customize/skills/:skillId',
        loadComponent: () =>
            import('./customize/skills/customize-skill-detail.page').then(
                m => m.CustomizeSkillDetailPage,
            ),
        canActivate: [authGuard],
    },
    {
        path: 'customize/connectors',
        loadComponent: () =>
            import('./customize/connectors/customize-connectors.page').then(
                m => m.CustomizeConnectorsPage,
            ),
        canActivate: [authGuard],
    },
    {
        path: 'customize',
        redirectTo: 'customize/tools',
        pathMatch: 'full',
    },
    // Connectors moved out of Settings and into Customize (spec step 2):
    // connecting an account and enabling the tools that need it are one intent.
    // The old deep link stays as a redirect rather than a deletion — it is in
    // bookmarks, and `schedules` linked users straight to it for years.
    {
        path: 'settings/connectors',
        redirectTo: 'customize/connectors',
        pathMatch: 'full',
    },
    {
        path: 'memories',
        loadComponent: () => import('./memory/memory-dashboard.page').then(m => m.MemoryDashboardPage),
        canActivate: [authGuard],
    },
    {
        path: 'memory-spaces/:id',
        loadComponent: () => import('./memory-spaces/memory-space-detail.page').then(m => m.MemorySpaceDetailPage),
        canActivate: [authGuard],
    },
    {
        path: 'memory-spaces',
        loadComponent: () => import('./memory-spaces/memory-spaces.page').then(m => m.MemorySpacesPage),
        canActivate: [authGuard],
    },
    {
        path: 'manage-sessions',
        loadComponent: () => import('./manage-sessions/manage-sessions.page').then(m => m.ManageSessionsPage),
        canActivate: [authGuard],
    },
    {
        path: 'files',
        loadComponent: () => import('./files/file-browser.page').then(m => m.FileBrowserPage),
        canActivate: [authGuard],
    },
    {
        // Declared before the list route so the viewer owns the two-segment
        // path; Angular matches in order.
        path: 'artifacts/:artifactId',
        loadComponent: () => import('./artifacts/artifact-view.page').then(m => m.ArtifactViewPage),
        canActivate: [authGuard],
        // Minimal chrome, same as the shared-artifact viewer. Not cosmetic:
        // the padded content box has no definite height, so a viewer laid
        // out with `h-full` inside it collapses — measured at 150px of
        // iframe in a 720px viewport. The minimal branch is `h-full` of the
        // scroll container, which is `flex-1` of an `h-dvh` main, so the
        // artifact finally gets the whole shell. It costs the sidenav,
        // which is why the header carries a labelled way back.
        data: { chrome: 'minimal' },
    },
    {
        path: 'artifacts',
        loadComponent: () => import('./artifacts/artifact-library.page').then(m => m.ArtifactLibraryPage),
        canActivate: [authGuard],
    },
    {
        path: 'oauth-complete',
        loadComponent: () => import('./oauth-complete/oauth-complete.page').then(m => m.OAuthCompletePage),
    },
    {
        path: 'settings',
        loadComponent: () => import('./settings/settings.page').then(m => m.SettingsPage),
        canActivate: [authGuard],
        loadChildren: () => import('./settings/settings.routes').then(m => m.settingsRoutes),
    },
    {
        path: 'fine-tuning',
        loadComponent: () => import('./fine-tuning/pages/dashboard/fine-tuning-dashboard.page').then(m => m.FineTuningDashboardPage),
        canActivate: [authGuard],
    },
    {
        path: 'fine-tuning/new-training',
        loadComponent: () => import('./fine-tuning/pages/create-training-job/create-training-job.page').then(m => m.CreateTrainingJobPage),
        canActivate: [authGuard],
    },
    {
        path: 'fine-tuning/new-inference',
        loadComponent: () => import('./fine-tuning/pages/create-inference-job/create-inference-job.page').then(m => m.CreateInferenceJobPage),
        canActivate: [authGuard],
    },
    {
        path: 'fine-tuning/training/:jobId',
        loadComponent: () => import('./fine-tuning/pages/training-job-detail/training-job-detail.page').then(m => m.TrainingJobDetailPage),
        canActivate: [authGuard],
    },
    {
        path: 'fine-tuning/inference/:jobId',
        loadComponent: () => import('./fine-tuning/pages/inference-job-detail/inference-job-detail.page').then(m => m.InferenceJobDetailPage),
        canActivate: [authGuard],
    },
    {
        path: '**',
        loadComponent: () => import('./not-found/not-found.page').then(m => m.NotFoundPage),
        canActivate: [authGuard],
    }
];
