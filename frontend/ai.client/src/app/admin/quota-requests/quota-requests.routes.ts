import { Routes } from '@angular/router';
import { QuotaRequestsLayout } from './quota-requests.layout';

export const quotaRequestsRoutes: Routes = [
  {
    path: '',
    component: QuotaRequestsLayout,
    children: [
      { path: '', redirectTo: 'requests', pathMatch: 'full' },
      {
        path: 'requests',
        loadComponent: () => import('./pages/request-queue.page').then(m => m.RequestQueuePage),
      },
      {
        path: 'requests/:requestId',
        loadComponent: () => import('./pages/request-detail.page').then(m => m.RequestDetailPage),
      },
      {
        path: 'extensions',
        loadComponent: () => import('./pages/extension-list.page').then(m => m.ExtensionListPage),
      },
      {
        path: 'settings',
        loadComponent: () => import('./pages/policy-settings.page').then(m => m.PolicySettingsPage),
      },
    ],
  },
];
