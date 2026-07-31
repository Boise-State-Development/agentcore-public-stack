import { Routes } from '@angular/router';
import { FineTuningLayout } from './fine-tuning-access/fine-tuning.layout';
import {
  adminLandingGuard,
  adminScopeGuard,
  AdminScopeRouteData,
} from '../auth/admin-scope.guard';

/**
 * Admin console routes.
 *
 * Every entry carries `data.scope` and `adminScopeGuard`. The parent
 * `adminGuard` (in `app.routes.ts`) only decides whether the console opens at
 * all; this is what decides which pages inside it are reachable. A route added
 * without a scope is denied by the guard rather than left open — see
 * `adminScopeGuard`.
 *
 * `satisfies AdminScopeRouteData` on each `data` object makes a mistyped scope
 * a build error instead of a page that silently denies everyone.
 */
export const adminRoutes: Routes = [
  {
    // Not a static `redirectTo: 'costs'` any more: a delegated admin without
    // `admin.costs` would land on a page they cannot open and bounce straight
    // back out. The guard always returns a UrlTree, so `children: []` is never
    // activated — it exists only because a route needs *something* to match.
    path: '',
    pathMatch: 'full',
    canActivate: [adminLandingGuard],
    children: [],
  },
  {
    path: 'costs',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.costs' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./costs/admin-costs.page').then(m => m.AdminCostsPage),
  },
  {
    path: 'costs/sessions/:id',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.costs' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./costs/pages/session-cost-anatomy.page').then(m => m.SessionCostAnatomyPage),
  },
  {
    path: 'quota',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.quota' } satisfies AdminScopeRouteData,
    loadChildren: () => import('./quota-tiers/quota-routing.module').then(m => m.quotaRoutes),
  },
  {
    path: 'fine-tuning',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.fine_tuning' } satisfies AdminScopeRouteData,
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
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.models' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./manage-models/manage-models.page').then(m => m.ManageModelsPage),
  },
  {
    path: 'manage-models/catalog',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.models' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./manage-models/model-catalog.page').then(m => m.ModelCatalogPage),
  },
  {
    path: 'manage-models/new',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.models' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./manage-models/model-form.page').then(m => m.ModelFormPage),
  },
  {
    path: 'manage-models/edit/:id',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.models' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./manage-models/model-form.page').then(m => m.ModelFormPage),
  },
  {
    path: 'bedrock/models',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.models' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./bedrock-models/bedrock-models.page').then(m => m.BedrockModelsPage),
  },
  {
    path: 'gemini/models',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.models' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./gemini-models/gemini-models.page').then(m => m.GeminiModelsPage),
  },
  {
    path: 'openai/models',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.models' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./openai-models/openai-models.page').then(m => m.OpenAIModelsPage),
  },
  {
    path: 'tools',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.tools' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./tools/pages/tool-list.page').then(m => m.ToolListPage),
  },
  {
    path: 'tools/new',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.tools' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./tools/pages/tool-form.page').then(m => m.ToolFormPage),
  },
  {
    path: 'tools/edit/:toolId',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.tools' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./tools/pages/tool-form.page').then(m => m.ToolFormPage),
  },
  {
    path: 'skills',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.skills' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./skills/pages/skill-list.page').then(m => m.SkillListPage),
  },
  {
    path: 'skills/new',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.skills' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./skills/pages/skill-form.page').then(m => m.SkillFormPage),
  },
  {
    path: 'skills/edit/:skillId',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.skills' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./skills/pages/skill-form.page').then(m => m.SkillFormPage),
  },
  // Agent Marketplace (docs/specs/agent-marketplace.md) — six of D10's seven admin
  // surfaces. Default pins live under Roles, because the AppRole record owns a seed.
  {
    path: 'marketplace/review',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.marketplace' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./marketplace/pages/review-queue.page').then(m => m.ReviewQueuePage),
  },
  {
    path: 'marketplace/reports',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.marketplace' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./marketplace/pages/reports.page').then(m => m.ReportsPage),
  },
  {
    path: 'marketplace/listings',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.marketplace' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./marketplace/pages/listings.page').then(m => m.MarketplaceListingsPage),
  },
  {
    path: 'marketplace/publishers',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.marketplace' } satisfies AdminScopeRouteData,
    loadComponent: () =>
      import('./marketplace/pages/publishers.page').then(m => m.MarketplacePublishersPage),
  },
  {
    path: 'marketplace/categories',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.marketplace' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./marketplace/pages/categories.page').then(m => m.MarketplaceCategoriesPage),
  },
  {
    path: 'marketplace/store-front',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.marketplace' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./marketplace/pages/store-front.page').then(m => m.MarketplaceStoreFrontPage),
  },
  {
    path: 'marketplace/default-pins',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.marketplace' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./marketplace/pages/default-pins.page').then(m => m.MarketplaceDefaultPinsPage),
  },
  {
    path: 'marketplace',
    redirectTo: 'marketplace/review',
    pathMatch: 'full',
  },
  {
    path: 'connectors',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.connectors' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./connectors/pages/connector-list.page').then(m => m.ConnectorListPage),
  },
  {
    path: 'connectors/new',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.connectors' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./connectors/pages/connector-form.page').then(m => m.ConnectorFormPage),
  },
  {
    path: 'connectors/edit/:providerId',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.connectors' } satisfies AdminScopeRouteData,
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
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.users' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./users/pages/user-list/user-list.page').then(m => m.UserListPage),
  },
  {
    path: 'users/:userId',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.users' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./users/pages/user-detail/user-detail.page').then(m => m.UserDetailPage),
  },
  {
    path: 'roles',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.roles' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./roles/pages/role-list.page').then(m => m.RoleListPage),
  },
  {
    path: 'roles/new',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.roles' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./roles/pages/role-form.page').then(m => m.RoleFormPage),
  },
  {
    path: 'roles/edit/:id',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.roles' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./roles/pages/role-form.page').then(m => m.RoleFormPage),
  },
  {
    path: 'auth-providers',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.auth_providers' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./auth-providers/pages/provider-list.page').then(m => m.AuthProviderListPage),
  },
  {
    path: 'auth-providers/new',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.auth_providers' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./auth-providers/pages/provider-form.page').then(m => m.AuthProviderFormPage),
  },
  {
    path: 'auth-providers/edit/:providerId',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.auth_providers' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./auth-providers/pages/provider-form.page').then(m => m.AuthProviderFormPage),
  },
  {
    path: 'audit',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.audit' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./audit/pages/audit-log.page').then(m => m.AuditLogPage),
  },
  {
    path: 'manage-user-menu-links',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.user_menu_links' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./manage-user-menu-links/manage-user-menu-links.page').then(m => m.ManageUserMenuLinksPage),
  },
  {
    path: 'manage-user-menu-links/new',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.user_menu_links' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./manage-user-menu-links/user-menu-link-form.page').then(m => m.UserMenuLinkFormPage),
  },
  {
    path: 'manage-user-menu-links/edit/:id',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.user_menu_links' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./manage-user-menu-links/user-menu-link-form.page').then(m => m.UserMenuLinkFormPage),
  },
  {
    path: 'system-prompts',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.system_prompts' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./system-prompts/manage-system-prompts.page').then(m => m.ManageSystemPromptsPage),
  },
  {
    path: 'system-prompts/new',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.system_prompts' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./system-prompts/system-prompt-form.page').then(m => m.SystemPromptFormPage),
  },
  {
    path: 'system-prompts/edit/:promptId',
    canActivate: [adminScopeGuard],
    data: { scope: 'admin.system_prompts' } satisfies AdminScopeRouteData,
    loadComponent: () => import('./system-prompts/system-prompt-form.page').then(m => m.SystemPromptFormPage),
  },
];
