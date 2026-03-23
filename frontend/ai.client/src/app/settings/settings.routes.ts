import { Routes } from '@angular/router';

export const settingsRoutes: Routes = [
  {
    path: '',
    redirectTo: 'profile',
    pathMatch: 'full',
  },
  {
    path: 'profile',
    loadComponent: () =>
      import('./pages/profile/profile-settings.page').then(m => m.ProfileSettingsPage),
  },
  {
    path: 'appearance',
    loadComponent: () =>
      import('./pages/appearance/appearance-settings.page').then(m => m.AppearanceSettingsPage),
  },
  {
    path: 'chat',
    loadComponent: () =>
      import('./pages/chat-preferences/chat-preferences-settings.page').then(m => m.ChatPreferencesSettingsPage),
  },
  {
    path: 'connections',
    loadComponent: () =>
      import('./pages/connections-settings/connections-settings.page').then(m => m.ConnectionsSettingsPage),
  },
  {
    path: 'api-keys',
    loadComponent: () =>
      import('./pages/api-keys/api-keys.page').then(m => m.ApiKeysPage),
  },
  {
    path: 'usage',
    loadComponent: () =>
      import('./pages/usage/usage-settings.page').then(m => m.UsageSettingsPage),
  },
];
