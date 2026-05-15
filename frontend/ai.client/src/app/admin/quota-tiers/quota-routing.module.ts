import { Routes } from '@angular/router';
import { QuotaLayout } from './quota.layout';

export const quotaRoutes: Routes = [
  {
    path: '',
    component: QuotaLayout,
    children: [
      {
        path: '',
        redirectTo: 'tiers',
        pathMatch: 'full',
      },
      {
        path: 'tiers',
        loadComponent: () =>
          import('./pages/tier-list/tier-list.component').then(
            (m) => m.TierListComponent
          ),
      },
      {
        path: 'assignments',
        loadComponent: () =>
          import('./pages/assignment-list/assignment-list.component').then(
            (m) => m.AssignmentListComponent
          ),
      },
      {
        path: 'overrides',
        loadComponent: () =>
          import('./pages/override-list/override-list.component').then(
            (m) => m.OverrideListComponent
          ),
      },
      {
        path: 'inspector',
        loadComponent: () =>
          import('./pages/quota-inspector/quota-inspector.component').then(
            (m) => m.QuotaInspectorComponent
          ),
      },
      {
        path: 'events',
        loadComponent: () =>
          import('./pages/event-viewer/event-viewer.component').then(
            (m) => m.EventViewerComponent
          ),
      },
    ],
  },
  {
    path: 'tiers/:tierId',
    loadComponent: () =>
      import('./pages/tier-detail/tier-detail.component').then(
        (m) => m.TierDetailComponent
      ),
  },
  {
    path: 'assignments/:assignmentId',
    loadComponent: () =>
      import('./pages/assignment-detail/assignment-detail.component').then(
        (m) => m.AssignmentDetailComponent
      ),
  },
  {
    path: 'overrides/:overrideId',
    loadComponent: () =>
      import('./pages/override-detail/override-detail.component').then(
        (m) => m.OverrideDetailComponent
      ),
  },
];
