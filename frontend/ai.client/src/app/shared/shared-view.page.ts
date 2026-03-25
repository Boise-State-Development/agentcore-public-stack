import { Component, ChangeDetectionStrategy, inject, signal, OnInit } from '@angular/core';
import { ActivatedRoute, Router } from '@angular/router';
import { DatePipe } from '@angular/common';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroLockClosed,
  heroExclamationTriangle,
  heroDocumentDuplicate,
} from '@ng-icons/heroicons/outline';
import { ShareService, SharedConversationResponse } from '../session/services/share/share.service';
import { MessageListComponent } from '../session/components/message-list/message-list.component';
import { SessionService } from '../session/services/session/session.service';
import { UserService } from '../auth/user.service';
import { Message } from '../session/services/models/message.model';

@Component({
  selector: 'app-shared-view',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, DatePipe, MessageListComponent],
  providers: [provideIcons({ heroLockClosed, heroExclamationTriangle, heroDocumentDuplicate })],
  template: `
    <div class="min-h-screen bg-white dark:bg-gray-900">
      <!-- Header banner -->
      <div class="sticky top-0 z-10 border-b border-gray-200 bg-white/95 backdrop-blur dark:border-gray-700 dark:bg-gray-900/95">
        <div class="mx-auto flex max-w-4xl items-center justify-between px-4 py-3">
          <div class="flex items-center gap-2">
            <ng-icon name="heroLockClosed" class="size-4 text-gray-400" aria-hidden="true" />
            <span class="text-xs font-medium text-gray-500 dark:text-gray-400">Shared read-only snapshot</span>
          </div>
          <div class="flex items-center gap-3">
            @if (conversation()) {
              <span class="text-xs text-gray-400 dark:text-gray-500">
                Shared {{ conversation()!.createdAt | date:'medium' }}
              </span>
              <button
                type="button"
                (click)="onExport()"
                [disabled]="isExporting()"
                class="inline-flex items-center gap-1.5 rounded-md bg-primary-600 px-2.5 py-1.5 text-xs font-medium text-white shadow-xs hover:bg-primary-500 disabled:opacity-50 disabled:cursor-not-allowed dark:bg-primary-500 dark:hover:bg-primary-400"
              >
                @if (isExporting()) {
                  <span class="size-3.5 animate-spin rounded-full border-2 border-white border-t-transparent" aria-hidden="true"></span>
                } @else {
                  <ng-icon name="heroDocumentDuplicate" class="size-3.5" aria-hidden="true" />
                }
                Export to new conversation
              </button>
            }
          </div>
        </div>
      </div>

      @if (isLoading()) {
        <div class="flex items-center justify-center py-20">
          <div class="text-center">
            <div class="mx-auto size-8 animate-spin rounded-full border-2 border-gray-300 border-t-primary-500"></div>
            <p class="mt-3 text-sm text-gray-500 dark:text-gray-400">Loading shared conversation...</p>
          </div>
        </div>
      } @else if (errorStatus()) {
        <div class="flex items-center justify-center py-20">
          <div class="text-center">
            <ng-icon name="heroExclamationTriangle" class="mx-auto size-12 text-gray-400 dark:text-gray-500" aria-hidden="true" />
            @if (errorStatus() === 403) {
              <h2 class="mt-4 text-lg font-semibold text-gray-900 dark:text-white">Access denied</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">You don't have permission to view this conversation.</p>
            } @else if (errorStatus() === 404) {
              <h2 class="mt-4 text-lg font-semibold text-gray-900 dark:text-white">Conversation not found</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">This share link may have been revoked.</p>
            } @else {
              <h2 class="mt-4 text-lg font-semibold text-gray-900 dark:text-white">Something went wrong</h2>
              <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">Failed to load the shared conversation.</p>
            }
          </div>
        </div>
      } @else if (conversation()) {
        <!-- Title -->
        <div class="mx-auto max-w-4xl px-4 pt-6 pb-2">
          <h1 class="text-xl font-semibold text-gray-900 dark:text-white">
            {{ conversation()!.title }}
          </h1>
        </div>

        <!-- Messages -->
        <div class="mx-auto max-w-4xl px-4 pb-16">
          <app-message-list
            [messages]="messages()"
            [embeddedMode]="true"
          />
        </div>
      }
    </div>
  `,
})
export class SharedViewPage implements OnInit {
  private route = inject(ActivatedRoute);
  private router = inject(Router);
  private shareService = inject(ShareService);
  private sessionService = inject(SessionService);
  private userService = inject(UserService);

  protected conversation = signal<SharedConversationResponse | null>(null);
  protected messages = signal<Message[]>([]);
  protected isLoading = signal(true);
  protected isExporting = signal(false);
  protected errorStatus = signal<number | null>(null);

  async ngOnInit(): Promise<void> {
    const shareId = this.route.snapshot.paramMap.get('shareId');
    if (!shareId) {
      this.errorStatus.set(404);
      this.isLoading.set(false);
      return;
    }

    try {
      const data = await this.shareService.getSharedConversation(shareId);
      this.conversation.set(data);
      this.messages.set(data.messages as Message[]);
    } catch (err: unknown) {
      const status = (err as any)?.status ?? (err as any)?.error?.status ?? 500;
      this.errorStatus.set(status);
    } finally {
      this.isLoading.set(false);
    }
  }

  protected async onExport(): Promise<void> {
    const conv = this.conversation();
    if (!conv) return;

    this.isExporting.set(true);
    try {
      const result = await this.shareService.exportSharedConversation(conv.shareId);

      // Add the new session to the local cache so sidenav picks it up
      const user = this.userService.currentUser();
      const userId = user?.user_id || 'anonymous';
      this.sessionService.addSessionToCache(result.sessionId, userId, result.title);

      // Navigate to the new session
      this.router.navigate(['/s', result.sessionId]);
    } catch (err: unknown) {
      console.error('Failed to export shared conversation:', err);
    } finally {
      this.isExporting.set(false);
    }
  }
}
