import { Routes } from '@angular/router';
import { authGuard } from './auth/auth.guard';
import { adminGuard } from './auth/admin.guard';
import { firstBootGuard } from './auth/first-boot.guard';
import { legacyMigrationHostGuard } from './shared/utils/legacy-migration-host';

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
    {
        path: 'my-skills/new',
        loadComponent: () => import('./my-skills/my-skill-form.page').then(m => m.MySkillFormPage),
        canActivate: [authGuard],
    },
    {
        path: 'my-skills/:skillId/edit',
        loadComponent: () => import('./my-skills/my-skill-form.page').then(m => m.MySkillFormPage),
        canActivate: [authGuard],
    },
    {
        path: 'my-skills',
        loadComponent: () => import('./my-skills/my-skills.page').then(m => m.MySkillsPage),
        canActivate: [authGuard],
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
