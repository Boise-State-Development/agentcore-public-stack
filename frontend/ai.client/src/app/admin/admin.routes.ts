import { Routes } from '@angular/router';
import { FineTuningLayout } from './fine-tuning-access/fine-tuning.layout';

export const adminRoutes: Routes = [
  {
    path: '',
    redirectTo: 'costs',
    pathMatch: 'full',
  },
  {
    path: 'costs',
    loadComponent: () => import('./costs/admin-costs.page').then(m => m.AdminCostsPage),
  },
  {
    path: 'quota',
    loadChildren: () => import('./quota-tiers/quota-routing.module').then(m => m.quotaRoutes),
  },
  {
    path: 'fine-tuning',
    component: FineTuningLayout,
    children: [
      {
        path: '',
        loadComponent: () => import('./fine-tuning-access/fine-tuning-access.page').then(m => m.FineTuningAccessPage),
      },
      {
        path: 'costs',
        loadComponent: () => import('./fine-tuning-costs/fine-tuning-costs.page').then(m => m.FineTuningCostsPage),
      },
    ],
  },
  {
    path: 'manage-models',
    loadComponent: () => import('./manage-models/manage-models.page').then(m => m.ManageModelsPage),
  },
  {
    path: 'manage-models/catalog',
    loadComponent: () => import('./manage-models/model-catalog.page').then(m => m.ModelCatalogPage),
  },
  {
    path: 'manage-models/new',
    loadComponent: () => import('./manage-models/model-form.page').then(m => m.ModelFormPage),
  },
  {
    path: 'manage-models/edit/:id',
    loadComponent: () => import('./manage-models/model-form.page').then(m => m.ModelFormPage),
  },
  {
    path: 'bedrock/models',
    loadComponent: () => import('./bedrock-models/bedrock-models.page').then(m => m.BedrockModelsPage),
  },
  {
    path: 'gemini/models',
    loadComponent: () => import('./gemini-models/gemini-models.page').then(m => m.GeminiModelsPage),
  },
  {
    path: 'openai/models',
    loadComponent: () => import('./openai-models/openai-models.page').then(m => m.OpenAIModelsPage),
  },
  {
    path: 'tools',
    loadComponent: () => import('./tools/pages/tool-list.page').then(m => m.ToolListPage),
  },
  {
    path: 'tools/new',
    loadComponent: () => import('./tools/pages/tool-form.page').then(m => m.ToolFormPage),
  },
  {
    path: 'tools/edit/:toolId',
    loadComponent: () => import('./tools/pages/tool-form.page').then(m => m.ToolFormPage),
  },
  {
    path: 'skills',
    loadComponent: () => import('./skills/pages/skill-list.page').then(m => m.SkillListPage),
  },
  {
    path: 'skills/new',
    loadComponent: () => import('./skills/pages/skill-form.page').then(m => m.SkillFormPage),
  },
  {
    path: 'skills/edit/:skillId',
    loadComponent: () => import('./skills/pages/skill-form.page').then(m => m.SkillFormPage),
  },
  {
    path: 'connectors',
    loadComponent: () => import('./connectors/pages/connector-list.page').then(m => m.ConnectorListPage),
  },
  {
    path: 'connectors/new',
    loadComponent: () => import('./connectors/pages/connector-form.page').then(m => m.ConnectorFormPage),
  },
  {
    path: 'connectors/edit/:providerId',
    loadComponent: () => import('./connectors/pages/connector-form.page').then(m => m.ConnectorFormPage),
  },
  {
    path: 'oauth-providers',
    redirectTo: 'connectors',
    pathMatch: 'full',
  },
  {
    path: 'oauth-providers/new',
    redirectTo: 'connectors/new',
    pathMatch: 'full',
  },
  {
    path: 'oauth-providers/edit/:providerId',
    redirectTo: 'connectors/edit/:providerId',
    pathMatch: 'full',
  },
  {
    path: 'users',
    loadComponent: () => import('./users/pages/user-list/user-list.page').then(m => m.UserListPage),
  },
  {
    path: 'users/:userId',
    loadComponent: () => import('./users/pages/user-detail/user-detail.page').then(m => m.UserDetailPage),
  },
  {
    path: 'roles',
    loadComponent: () => import('./roles/pages/role-list.page').then(m => m.RoleListPage),
  },
  {
    path: 'roles/new',
    loadComponent: () => import('./roles/pages/role-form.page').then(m => m.RoleFormPage),
  },
  {
    path: 'roles/edit/:id',
    loadComponent: () => import('./roles/pages/role-form.page').then(m => m.RoleFormPage),
  },
  {
    path: 'auth-providers',
    loadComponent: () => import('./auth-providers/pages/provider-list.page').then(m => m.AuthProviderListPage),
  },
  {
    path: 'auth-providers/new',
    loadComponent: () => import('./auth-providers/pages/provider-form.page').then(m => m.AuthProviderFormPage),
  },
  {
    path: 'auth-providers/edit/:providerId',
    loadComponent: () => import('./auth-providers/pages/provider-form.page').then(m => m.AuthProviderFormPage),
  },
  {
    path: 'manage-user-menu-links',
    loadComponent: () => import('./manage-user-menu-links/manage-user-menu-links.page').then(m => m.ManageUserMenuLinksPage),
  },
  {
    path: 'manage-user-menu-links/new',
    loadComponent: () => import('./manage-user-menu-links/user-menu-link-form.page').then(m => m.UserMenuLinkFormPage),
  },
  {
    path: 'manage-user-menu-links/edit/:id',
    loadComponent: () => import('./manage-user-menu-links/user-menu-link-form.page').then(m => m.UserMenuLinkFormPage),
  },
  {
    path: 'system-prompts',
    loadComponent: () => import('./system-prompts/manage-system-prompts.page').then(m => m.ManageSystemPromptsPage),
  },
  {
    path: 'system-prompts/new',
    loadComponent: () => import('./system-prompts/system-prompt-form.page').then(m => m.SystemPromptFormPage),
  },
  {
    path: 'system-prompts/edit/:promptId',
    loadComponent: () => import('./system-prompts/system-prompt-form.page').then(m => m.SystemPromptFormPage),
  },
];
