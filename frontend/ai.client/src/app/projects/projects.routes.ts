import { Routes } from '@angular/router';
import { unsavedChangesGuard } from './settings/unsaved-changes.guard';

/**
 * Everything under `/projects/:id` (shared-projects §6). The parent route is
 * componentless, so each page receives `id` through `withComponentInputBinding`.
 *
 * The overview holds the composer, the caller's tasks and the settings rail. Each
 * rail row but Model is a page of its own, so a link (the bell, an Activity entry,
 * the schedule form's way back) lands on it and Back walks back through them.
 * Model is a menu on its row.
 */
export const PROJECT_ROUTES: Routes = [
  { path: '', pathMatch: 'full', redirectTo: 'overview' },
  {
    path: 'overview',
    loadComponent: () => import('./detail/project-detail.page').then(m => m.ProjectDetailPage),
  },
  {
    // A browser beside a file (2.8); `scope`, `file` and `view` ride the query string.
    path: 'memory',
    loadComponent: () => import('./memory/project-memory.page').then(m => m.ProjectMemoryPage),
  },
  {
    path: 'instructions',
    loadComponent: () => import('./settings/project-instructions.page').then(m => m.ProjectInstructionsPage),
    canDeactivate: [unsavedChangesGuard],
  },
  {
    path: 'files',
    loadComponent: () => import('./settings/project-files.page').then(m => m.ProjectFilesPage),
  },
  {
    path: 'tools',
    loadComponent: () => import('./settings/project-bindings.page').then(m => m.ProjectBindingsPage),
    canDeactivate: [unsavedChangesGuard],
    data: { kind: 'tools' },
  },
  {
    path: 'skills',
    loadComponent: () => import('./settings/project-bindings.page').then(m => m.ProjectBindingsPage),
    canDeactivate: [unsavedChangesGuard],
    data: { kind: 'skills' },
  },
  {
    path: 'schedules',
    loadComponent: () => import('./settings/project-schedules.page').then(m => m.ProjectSchedulesPage),
  },
  {
    path: 'members',
    loadComponent: () => import('./settings/project-members.page').then(m => m.ProjectMembersPage),
  },
  {
    path: 'activity',
    loadComponent: () => import('./settings/project-activity.page').then(m => m.ProjectActivityPage),
  },
  {
    // `?version=N` opens that version.
    path: 'history',
    loadComponent: () => import('./settings/project-history.page').then(m => m.ProjectHistoryPage),
  },
  // Tabs that predate these pages: Settings was the instructions; Tasks and Model
  // live on the overview.
  { path: 'settings', redirectTo: 'instructions' },
  { path: '**', redirectTo: 'overview' },
];
