import { Routes } from '@angular/router';

export const quotaRoutes: Routes = [
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
    path: 'tiers/:tierId',
    loadComponent: () =>
      import('./pages/tier-detail/tier-detail.component').then(
        (m) => m.TierDetailComponent
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
    path: 'assignments/:assignmentId',
    loadComponent: () =>
      import('./pages/assignment-detail/assignment-detail.component').then(
        (m) => m.AssignmentDetailComponent
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
    path: 'overrides/:overrideId',
    loadComponent: () =>
      import('./pages/override-detail/override-detail.component').then(
        (m) => m.OverrideDetailComponent
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
];
