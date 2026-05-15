import { Routes } from '@angular/router';
import { authGuard } from './auth/auth.guard';
import { adminGuard } from './auth/admin.guard';
import { firstBootGuard } from './auth/first-boot.guard';

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
