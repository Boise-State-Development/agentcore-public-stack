import { Component, inject, ChangeDetectionStrategy, computed } from '@angular/core';
import { RouterLink, RouterLinkActive } from '@angular/router';
import { SessionService } from '../../../../session/services/session/session.service';
import { SessionMetadata } from '../../../../session/services/models/session-metadata.model';

@Component({
  selector: 'app-session-list',
  imports: [RouterLink, RouterLinkActive],
  templateUrl: './session-list.html',
  styleUrl: './session-list.css',
  changeDetection: ChangeDetectionStrategy.OnPush
})
export class SessionList {
  private sessionService = inject(SessionService);

  /**
   * Reactive resource for fetching sessions.
   * Automatically refetches when sessionsParams change.
   */
  readonly sessionsResource = this.sessionService.sessionsResource;

  /**
   * Computed signal for sessions value.
   */
  readonly sessions = computed(() => this.sessionsResource.value());

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
}
