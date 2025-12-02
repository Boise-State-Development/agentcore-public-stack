import { Routes } from '@angular/router';
import { ConversationPage } from './conversation/conversation.page';
import { authGuard } from './auth/auth.guard';

export const routes: Routes = [
    {
        path: '',
        loadComponent: () => import('./conversation/conversation.page').then(m => m.ConversationPage),
        canActivate: [authGuard],
    },
    {
        path: 'c/:conversationId',
        loadComponent: () => import('./conversation/conversation.page').then(m => m.ConversationPage),
        canActivate: [authGuard],
    },
    {
        path: 'auth/login',
        loadComponent: () => import('./auth/login/login.page').then(m => m.LoginPage),
    },
    {
        path: 'auth/callback',
        loadComponent: () => import('./auth/callback/callback.page').then(m => m.CallbackPage),
    }
];
