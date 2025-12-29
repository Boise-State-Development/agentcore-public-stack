import { Component, inject, ChangeDetectionStrategy, computed, signal } from '@angular/core';
import { Router, RouterLink, RouterLinkActive } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { CdkMenuTrigger, CdkMenu, CdkMenuItem } from '@angular/cdk/menu';
import { ConnectedPosition } from '@angular/cdk/overlay';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroChatBubbleLeftRight, heroTrash, heroArrowPath } from '@ng-icons/heroicons/outline';
import { heroEllipsisHorizontalSolid } from '@ng-icons/heroicons/solid';
import { SessionService } from '../../../../session/services/session/session.service';
import { SessionMetadata } from '../../../../session/services/models/session-metadata.model';
import { SidenavService } from '../../../../services/sidenav/sidenav.service';
import { ToastService } from '../../../../services/toast/toast.service';
import { ConfirmationDialogComponent, ConfirmationDialogData } from '../../../confirmation-dialog';

@Component({
  selector: 'app-session-list',
  imports: [RouterLink, RouterLinkActive, NgIcon, CdkMenuTrigger, CdkMenu, CdkMenuItem],
  providers: [provideIcons({ heroChatBubbleLeftRight, heroTrash, heroArrowPath, heroEllipsisHorizontalSolid })],
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

  /**
   * Signal tracking which session is currently being deleted.
   * Used to show loading state on the delete button.
   */
  protected deletingSessionId = signal<string | null>(null);

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
