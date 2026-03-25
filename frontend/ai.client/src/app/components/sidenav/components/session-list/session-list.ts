import { Component, inject, ChangeDetectionStrategy, computed, signal, afterNextRender, Injector } from '@angular/core';
import { Router, RouterLink, RouterLinkActive } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { CdkMenuTrigger, CdkMenu, CdkMenuItem } from '@angular/cdk/menu';
import { ConnectedPosition } from '@angular/cdk/overlay';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroChatBubbleLeftRight, heroTrash, heroArrowPath, heroPencilSquare, heroArrowUpOnSquare } from '@ng-icons/heroicons/outline';
import { heroEllipsisHorizontalSolid } from '@ng-icons/heroicons/solid';
import { SessionService } from '../../../../session/services/session/session.service';
import { ShareModalComponent, ShareModalData } from '../../../../session/components/share-modal';
import { UserService } from '../../../../auth/user.service';
import { SessionMetadata } from '../../../../session/services/models/session-metadata.model';
import { SidenavService } from '../../../../services/sidenav/sidenav.service';
import { ToastService } from '../../../../services/toast/toast.service';
import { ConfirmationDialogComponent, ConfirmationDialogData } from '../../../confirmation-dialog';

@Component({
  selector: 'app-session-list',
  imports: [RouterLink, RouterLinkActive, NgIcon, CdkMenuTrigger, CdkMenu, CdkMenuItem],
  providers: [provideIcons({ heroChatBubbleLeftRight, heroTrash, heroArrowPath, heroEllipsisHorizontalSolid, heroPencilSquare, heroArrowUpOnSquare })],
  templateUrl: './session-list.html',
  styleUrl: './session-list.css',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class SessionList {
  private sessionService = inject(SessionService);
  private sidenavService = inject(SidenavService);
  private toastService = inject(ToastService);
  private dialog = inject(Dialog);
  private router = inject(Router);
  private injector = inject(Injector);
  private userService = inject(UserService);

  /**
   * Signal tracking which session is currently being deleted.
   * Used to show loading state on the delete button.
   */
  protected deletingSessionId = signal<string | null>(null);

  /**
   * Signal tracking which session is currently being renamed.
   * When set, the session title is replaced with an inline text input.
   */
  protected renamingSessionId = signal<string | null>(null);

  /**
   * Signal holding the current value of the rename input field.
   */
  protected renameValue = signal('');

  /**
   * Reactive resource for fetching sessions (base API data).
   */
  readonly sessionsResource = this.sessionService.sessionsResource;

  /**
   * Merged sessions resource that combines API data with local cache.
   * This computed signal automatically updates when either changes.
   */
  readonly mergedSessionsResource = this.sessionService.mergedSessionsResource;

  /**
   * Computed signal for sessions array extracted from the merged response.
   */
  readonly sessions = computed(() => {
    const response = this.mergedSessionsResource();
    return response?.sessions;
  });

  /**
   * Computed signal that groups sessions by time period:
   * Today, Yesterday, Last 7 Days, Last 30 Days, Older.
   */
  readonly groupedSessions = computed(() => {
    const sessions = this.sessions();
    if (!sessions || sessions.length === 0) return [];

    const now = new Date();
    const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const startOfYesterday = new Date(startOfToday.getTime() - 86400000);
    const startOf7DaysAgo = new Date(startOfToday.getTime() - 6 * 86400000);
    const startOf30DaysAgo = new Date(startOfToday.getTime() - 29 * 86400000);

    const groups: { label: string; sessions: SessionMetadata[] }[] = [
      { label: 'Today', sessions: [] },
      { label: 'Yesterday', sessions: [] },
      { label: 'Last 7 Days', sessions: [] },
      { label: 'Last 30 Days', sessions: [] },
      { label: 'Older', sessions: [] },
    ];

    for (const session of sessions) {
      const date = new Date(session.lastMessageAt || session.createdAt);
      if (date >= startOfToday) {
        groups[0].sessions.push(session);
      } else if (date >= startOfYesterday) {
        groups[1].sessions.push(session);
      } else if (date >= startOf7DaysAgo) {
        groups[2].sessions.push(session);
      } else if (date >= startOf30DaysAgo) {
        groups[3].sessions.push(session);
      } else {
        groups[4].sessions.push(session);
      }
    }

    // Only return groups that have sessions
    return groups.filter(g => g.sessions.length > 0);
  });

  /**
   * Computed signal for pagination token.
   */
  readonly nextToken = computed(() => {
    const response = this.mergedSessionsResource();
    return response?.nextToken ?? null;
  });

  /**
   * Computed signal for loading state.
   */
  readonly isLoading = computed(() => {
    const value = this.sessionsResource.value();
    return value === undefined;
  });

  /**
   * Computed signal for error state.
   */
  readonly error = computed(() => this.sessionsResource.error());

  /**
   * Menu positioning - opens to the right of the trigger, aligned at the top.
   */
  protected readonly menuPositions: ConnectedPosition[] = [
    {
      originX: 'end',
      originY: 'center',
      overlayX: 'start',
      overlayY: 'top',
      offsetX: 4
    },
    {
      originX: 'start',
      originY: 'center',
      overlayX: 'end',
      overlayY: 'top',
      offsetX: -4
    }
  ];

  /**
   * Gets the session ID for routing.
   * @param sessionId - The session ID
   * @returns The session ID formatted for routing
   */
  protected getSessionId(sessionId: string): string {
    return sessionId;
  }

  /**
   * Formats a timestamp for display.
   * Shows relative time if recent, otherwise shows date.
   * @param timestamp - ISO 8601 timestamp string
   * @returns Formatted time string
   */
  protected formatTime(timestamp: string): string {
    const date = new Date(timestamp);
    const now = new Date();
    const diffMs = now.getTime() - date.getTime();
    const diffMins = Math.floor(diffMs / 60000);
    const diffHours = Math.floor(diffMs / 3600000);
    const diffDays = Math.floor(diffMs / 86400000);

    if (diffMins < 1) {
      return 'Just now';
    } else if (diffMins < 60) {
      return `${diffMins}m ago`;
    } else if (diffHours < 24) {
      return `${diffHours}h ago`;
    } else if (diffDays < 7) {
      return `${diffDays}d ago`;
    } else {
      // Format as MM/DD/YYYY
      return date.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    }
  }

  /**
   * Gets the display title for a session.
   * Returns the title if set, otherwise "Untitled Session".
   * @param session - Session metadata
   * @returns Display title
   */
  protected getSessionTitle(session: SessionMetadata): string {
    return session.title || 'Untitled Session';
  }

  /**
   * Handles session selection, closing the sidenav on mobile.
   */
  protected onSessionClick(): void {
    this.sidenavService.close();
  }

  /**
   * Enters rename mode for a session. Populates the input with the current title.
   * Focus is handled in the template via a callback on the input element.
   *
   * @param event - Click event (stopped to prevent navigation)
   * @param session - The session to rename
   */
  protected onRenameClick(event: Event, session: SessionMetadata): void {
    event.preventDefault();
    event.stopPropagation();
    this.renameValue.set(session.title || '');
    this.renamingSessionId.set(session.sessionId);

    // Focus the input after the template re-renders
    afterNextRender(() => {
      const input = document.querySelector<HTMLInputElement>('input[aria-label="Rename conversation"]');
      if (input) {
        input.focus();
        input.select();
      }
    }, { injector: this.injector });
  }

  /**
   * Submits the rename, calling the API and updating the local cache.
   * Exits rename mode on success or error.
   *
   * @param session - The session being renamed
   */
  protected async onRenameSubmit(session: SessionMetadata): Promise<void> {
    const newTitle = this.renameValue().trim();
    if (!newTitle || newTitle === session.title) {
      this.onRenameCancel();
      return;
    }

    try {
      await this.sessionService.updateSessionTitle(session.sessionId, newTitle);
      this.sessionService.updateSessionTitleInCache(session.sessionId, newTitle);
      this.sessionsResource.reload();
    } catch (error) {
      console.error('Failed to rename session:', error);
      this.toastService.error(
        'Failed to rename',
        'There was an error renaming the conversation. Please try again.'
      );
    } finally {
      this.renamingSessionId.set(null);
    }
  }

  /**
   * Cancels rename mode without saving.
   */
  protected onRenameCancel(): void {
    this.renamingSessionId.set(null);
  }

  /**
   * Handles keydown events on the rename input.
   * Enter submits, Escape cancels.
   *
   * @param event - Keyboard event
   * @param session - The session being renamed
   */
  protected onRenameKeydown(event: KeyboardEvent, session: SessionMetadata): void {
    if (event.key === 'Enter') {
      event.preventDefault();
      this.onRenameSubmit(session);
    } else if (event.key === 'Escape') {
      event.preventDefault();
      this.onRenameCancel();
    }
  }

  /**
   * Opens the share modal for a session.
   *
   * @param event - Click event (stopped to prevent navigation)
   * @param session - The session to share
   */
  protected onShareClick(event: Event, session: SessionMetadata): void {
    event.preventDefault();
    event.stopPropagation();

    this.dialog.open(ShareModalComponent, {
      data: {
        sessionId: session.sessionId,
        ownerEmail: this.userService.currentUser()?.email ?? '',
      } as ShareModalData,
    });
  }

  /**
   * Opens a confirmation dialog and deletes the session if confirmed.
   * Navigates to home if the deleted session was the current one.
   *
   * @param event - Click event (stopped to prevent navigation)
   * @param session - The session to delete
   */
  protected async onDeleteClick(event: Event, session: SessionMetadata): Promise<void> {
    // Prevent the click from triggering navigation
    event.preventDefault();
    event.stopPropagation();

    const dialogRef = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: 'Delete Conversation',
        message: 'Are you sure you want to delete this conversation? This action cannot be undone.',
        confirmText: 'Delete',
        cancelText: 'Cancel',
        destructive: true
      } as ConfirmationDialogData
    });

    const confirmed = await firstValueFrom(dialogRef.closed);

    if (confirmed) {
      try {
        this.deletingSessionId.set(session.sessionId);

        // Check if we're deleting the current session
        const isCurrentSession = this.sessionService.currentSession().sessionId === session.sessionId;

        await this.sessionService.deleteSession(session.sessionId);

        // Show success toast
        this.toastService.success(
          'Conversation deleted',
          'The conversation has been permanently deleted.'
        );

        // Navigate to home if we deleted the current session
        if (isCurrentSession) {
          this.router.navigate(['']);
        }
      } catch (error) {
        console.error('Failed to delete session:', error);
        this.toastService.error(
          'Failed to delete',
          'There was an error deleting the conversation. Please try again.'
        );
      } finally {
        this.deletingSessionId.set(null);
      }
    }
  }
}
