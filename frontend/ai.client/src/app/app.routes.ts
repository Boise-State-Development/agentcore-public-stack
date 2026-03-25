import { Routes } from '@angular/router';
import { ConversationPage } from './session/session.page';
import { authGuard } from './auth/auth.guard';
import { adminGuard } from './auth/admin.guard';

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
        path: 'shared/:shareId',
        loadComponent: () => import('./shared/shared-view.page').then(m => m.SharedViewPage),
        canActivate: [authGuard],
    },
    {
        path: 'auth/login',
        loadComponent: () => import('./auth/login/login.page').then(m => m.LoginPage),
    },
    {
        path: 'auth/callback',
        loadComponent: () => import('./auth/callback/callback.page').then(m => m.CallbackPage),
    },
    {
        path: 'connections',
        redirectTo: 'settings/connections',
        pathMatch: 'full',
    },
    {
        path: 'admin',
        loadComponent: () => import('./admin/admin.page').then(m => m.AdminPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/bedrock/models',
        loadComponent: () => import('./admin/bedrock-models/bedrock-models.page').then(m => m.BedrockModelsPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/gemini/models',
        loadComponent: () => import('./admin/gemini-models/gemini-models.page').then(m => m.GeminiModelsPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/openai/models',
        loadComponent: () => import('./admin/openai-models/openai-models.page').then(m => m.OpenAIModelsPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/manage-models',
        loadComponent: () => import('./admin/manage-models/manage-models.page').then(m => m.ManageModelsPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/manage-models/new',
        loadComponent: () => import('./admin/manage-models/model-form.page').then(m => m.ModelFormPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/manage-models/edit/:id',
        loadComponent: () => import('./admin/manage-models/model-form.page').then(m => m.ModelFormPage),
        canActivate: [adminGuard],
    },
    {
        path: 'assistants/new',
        loadComponent: () => import('./assistants/assistant-form/assistant-form.page').then(m => m.AssistantFormPage),
        canActivate: [authGuard],
    },
    {
        path: 'assistants/:id/edit',
        loadComponent: () => import('./assistants/assistant-form/assistant-form.page').then(m => m.AssistantFormPage),
        canActivate: [authGuard],
    },
    {
        path: 'assistants',
        loadComponent: () => import('./assistants/assistants.page').then(m => m.AssistantsPage),
        canActivate: [authGuard],
    },
    {
        path: 'memories',
        loadComponent: () => import('./memory/memory-dashboard.page').then(m => m.MemoryDashboardPage),
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
        path: 'settings/oauth/callback',
        loadComponent: () => import('./settings/oauth-callback/oauth-callback.page').then(m => m.OAuthCallbackPage),
    },
    {
        path: 'settings',
        loadComponent: () => import('./settings/settings.page').then(m => m.SettingsPage),
        canActivate: [authGuard],
        loadChildren: () => import('./settings/settings.routes').then(m => m.settingsRoutes),
    },
    {
        path: 'admin/quota',
        loadChildren: () => import('./admin/quota-tiers/quota-routing.module').then(m => m.quotaRoutes),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/costs',
        loadComponent: () => import('./admin/costs/admin-costs.page').then(m => m.AdminCostsPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/users',
        loadComponent: () => import('./admin/users/pages/user-list/user-list.page').then(m => m.UserListPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/users/:userId',
        loadComponent: () => import('./admin/users/pages/user-detail/user-detail.page').then(m => m.UserDetailPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/roles',
        loadComponent: () => import('./admin/roles/pages/role-list.page').then(m => m.RoleListPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/roles/new',
        loadComponent: () => import('./admin/roles/pages/role-form.page').then(m => m.RoleFormPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/roles/edit/:id',
        loadComponent: () => import('./admin/roles/pages/role-form.page').then(m => m.RoleFormPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/tools',
        loadComponent: () => import('./admin/tools/pages/tool-list.page').then(m => m.ToolListPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/tools/new',
        loadComponent: () => import('./admin/tools/pages/tool-form.page').then(m => m.ToolFormPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/tools/edit/:toolId',
        loadComponent: () => import('./admin/tools/pages/tool-form.page').then(m => m.ToolFormPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/auth-providers',
        loadComponent: () => import('./admin/auth-providers/pages/provider-list.page').then(m => m.AuthProviderListPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/auth-providers/new',
        loadComponent: () => import('./admin/auth-providers/pages/provider-form.page').then(m => m.AuthProviderFormPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/auth-providers/edit/:providerId',
        loadComponent: () => import('./admin/auth-providers/pages/provider-form.page').then(m => m.AuthProviderFormPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/oauth-providers',
        loadComponent: () => import('./admin/oauth-providers/pages/provider-list.page').then(m => m.ProviderListPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/oauth-providers/new',
        loadComponent: () => import('./admin/oauth-providers/pages/provider-form.page').then(m => m.ProviderFormPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/oauth-providers/edit/:providerId',
        loadComponent: () => import('./admin/oauth-providers/pages/provider-form.page').then(m => m.ProviderFormPage),
        canActivate: [adminGuard],
    },
    {
        path: 'admin/fine-tuning',
        loadComponent: () => import('./admin/fine-tuning-access/fine-tuning-access.page').then(m => m.FineTuningAccessPage),
        canActivate: [adminGuard],
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
    }
];
