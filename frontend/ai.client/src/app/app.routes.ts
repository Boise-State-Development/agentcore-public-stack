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
        path: 'auth/login',
        loadComponent: () => import('./auth/login/login.page').then(m => m.LoginPage),
    },
    {
        path: 'auth/callback',
        loadComponent: () => import('./auth/callback/callback.page').then(m => m.CallbackPage),
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
        path: 'costs',
        loadComponent: () => import('./costs/cost-dashboard.page').then(m => m.CostDashboardPage),
        canActivate: [authGuard],
    }
];
