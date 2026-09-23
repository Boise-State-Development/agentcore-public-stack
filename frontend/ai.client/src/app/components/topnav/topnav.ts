import { Component, inject, ChangeDetectionStrategy, computed, signal, afterNextRender, Injector } from '@angular/core';
import { Router } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { CdkMenuTrigger, CdkMenu, CdkMenuItem } from '@angular/cdk/menu';
import { ConnectedPosition } from '@angular/cdk/overlay';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroChevronDown, heroTrash, heroPencilSquare, heroArrowUpOnSquare, heroCloudArrowUp, heroEnvelope, heroEnvelopeOpen } from '@ng-icons/heroicons/outline';
import { SessionService } from '../../session/services/session/session.service';
import { ChatStateService } from '../../session/services/chat/chat-state.service';
import { ShareModalComponent, ShareModalData } from '../../session/components/share-modal';
import { ExportDialogComponent, ExportDialogData } from '../../session/components/export-dialog';
import { UserService } from '../../auth/user.service';
import { SidenavService } from '../../services/sidenav/sidenav.service';
import { ToastService } from '../../services/toast/toast.service';
import { ConfirmationDialogComponent, ConfirmationDialogData } from '../confirmation-dialog';

@Component({
  selector: 'app-topnav',
  imports: [NgIcon, CdkMenuTrigger, CdkMenu, CdkMenuItem],
  providers: [provideIcons({ heroChevronDown, heroTrash, heroPencilSquare, heroArrowUpOnSquare, heroCloudArrowUp, heroEnvelope, heroEnvelopeOpen })],
  templateUrl: './topnav.html',
  styleUrl: './topnav.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class Topnav {
  private router = inject(Router);
  protected sidenavService = inject(SidenavService);
  protected sessionService = inject(SessionService);
  private chatStateService = inject(ChatStateService);
  private dialog = inject(Dialog);
  private toastService = inject(ToastService);
  private userService = inject(UserService);
  private injector = inject(Injector);

  readonly currentSession = this.sessionService.currentSession;

  /**
   * True while the active session's metadata is being fetched (e.g. on a hard
   * refresh, where the session id and title arrive together only once the
   * request resolves). Drives the title skeleton so the header isn't blank
   * during that window.
   */
  protected readonly titleLoading = computed(() =>
    this.sessionService.sessionMetadataResource.isLoading()
  );

  /**
   * True only while a title is genuinely still expected — so the skeleton
   * can never outlive a resolved-but-untitled session. Two pending sources:
   *   1. Metadata is still being fetched (hard refresh).
   *   2. A brand-new conversation whose title is generating server-side and
   *      arrives mid-stream via the `session_title` SSE event; bounded to the
   *      active response, so once the stream ends without a title we fall
   *      back to the static "Untitled Session" label instead of shimmering
   *      forever.
   * A session that already has a title is never pending.
   */
  protected readonly titlePending = computed(() => {
    const session = this.currentSession();
    if (session.title) {
      return false;
    }
    if (this.titleLoading()) {
      return true;
    }
    return this.chatStateService.isSessionLoading(session.sessionId);
  });

  /** Header label once a title is no longer pending — real title or fallback. */
  protected readonly displayTitle = computed(
    () => this.currentSession().title || 'Untitled Session'
  );

  /** True while the title is being edited inline. */
  protected readonly renaming = signal(false);

  /** Holds the current value of the inline rename input. */
  protected readonly renameValue = signal('');

  /** True while the current session is being deleted. */
  protected readonly deleting = signal(false);

  /**
   * Menu positioning - opens below the trigger, aligned to its start edge,
   * with an upward fallback.
   */
  protected readonly menuPositions: ConnectedPosition[] = [
    {
      originX: 'start',
      originY: 'bottom',
      overlayX: 'start',
      overlayY: 'top',
      offsetY: 4
    },
    {
      originX: 'start',
      originY: 'top',
      overlayX: 'start',
      overlayY: 'bottom',
      offsetY: -4
    }
  ];

  newChat() {
    this.router.navigate(['']);
  }

  openSidenav() {
    this.sidenavService.open();
  }

  /**
   * Enters inline rename mode, seeding the input with the current title and
   * focusing it once the template re-renders.
   */
  protected onRenameClick(event: Event): void {
    event.preventDefault();
    event.stopPropagation();
    this.renameValue.set(this.currentSession().title || '');
    this.renaming.set(true);

    afterNextRender(() => {
      const input = document.querySelector<HTMLInputElement>('input[aria-label="Rename conversation title"]');
      if (input) {
        input.focus();
        input.select();
      }
    }, { injector: this.injector });
  }

  /**
   * Applies the rename optimistically: the header and sidenav update
   * immediately and rename mode exits without waiting on the API. Only on
   * failure do we revert to the previous title and surface a toast.
   */
  protected async onRenameSubmit(): Promise<void> {
    const session = this.currentSession();
    const sessionId = session.sessionId;
    const previousTitle = session.title;
    const newTitle = this.renameValue().trim();

    if (!newTitle || newTitle === previousTitle) {
      this.onRenameCancel();
      return;
    }

    // Optimistically apply the new title and leave rename mode right away.
    this.applyTitle(sessionId, newTitle);
    this.renaming.set(false);

    try {
      await this.sessionService.updateSessionTitle(sessionId, newTitle);
      this.sessionService.sessionsResource.reload();
    } catch (error) {
      console.error('Failed to rename session:', error);
      // Roll back the optimistic change.
      this.applyTitle(sessionId, previousTitle);
      this.sessionService.sessionsResource.reload();
      this.toastService.error(
        'Failed to rename',
        'There was an error renaming the conversation. Please try again.'
      );
    }
  }

  /**
   * Reflects a title into the local cache and, when it matches the active
   * session, the currentSession signal so the header updates instantly.
   */
  private applyTitle(sessionId: string, title: string): void {
    this.sessionService.updateSessionTitleInCache(sessionId, title);
    if (this.sessionService.currentSession().sessionId === sessionId) {
      this.sessionService.currentSession.update(current => ({ ...current, title }));
    }
  }

  /** Cancels rename mode without saving. */
  protected onRenameCancel(): void {
    this.renaming.set(false);
  }

  /** Enter submits, Escape cancels. */
  protected onRenameKeydown(event: KeyboardEvent): void {
    if (event.key === 'Enter') {
      event.preventDefault();
      this.onRenameSubmit();
    } else if (event.key === 'Escape') {
      event.preventDefault();
      this.onRenameCancel();
    }
  }

  /** True when the active session currently carries the durable unread flag. */
  protected readonly isCurrentUnread = computed(() => this.currentSession().unread === true);

  /**
   * Toggles the session's durable unread flag from the options menu. When
   * unread, marks it read (clears the sidebar dot); otherwise marks it unread
   * ("remind me to revisit"), which re-surfaces the dot.
   */
  protected onToggleReadClick(event: Event): void {
    event.preventDefault();
    event.stopPropagation();
    const session = this.currentSession();
    if (session.unread) {
      void this.sessionService.markSessionRead(session);
      this.chatStateService.clearSessionUnread(session.sessionId);
      // markSessionRead relies on the watermark (no refetch); kick the sidebar
      // list to re-render so its dot clears too, not just this header's menu.
      this.sessionService.refreshSessions();
    } else {
      // markSessionUnread already refetches the list; the client-side flag
      // surfaces the sidebar dot immediately (server flag is eventually
      // consistent), and clears when the session is next opened.
      void this.sessionService.markSessionUnread(session);
      this.chatStateService.markSessionUnread(session.sessionId);
    }
  }

  /** Opens the share modal for the current session. */
  protected onShareClick(event: Event): void {
    event.preventDefault();
    event.stopPropagation();

    this.dialog.open(ShareModalComponent, {
      data: {
        sessionId: this.currentSession().sessionId,
        ownerEmail: this.userService.currentUser()?.email ?? '',
      } as ShareModalData,
    });
  }

  /** Opens the "Save to…" export dialog for the current session. */
  protected onExportClick(event: Event): void {
    event.preventDefault();
    event.stopPropagation();

    const session = this.currentSession();
    this.dialog.open(ExportDialogComponent, {
      data: {
        sessionId: session.sessionId,
        title: session.title,
      } as ExportDialogData,
    });
  }

  /**
   * Confirms and deletes the current session, navigating home afterwards.
   */
  protected async onDeleteClick(event: Event): Promise<void> {
    event.preventDefault();
    event.stopPropagation();

    const sessionId = this.currentSession().sessionId;

    const dialogRef = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: 'Delete Conversation',
        message: 'Are you sure you want to delete this conversation? This action cannot be undone. Any shared links to this conversation will stop working.',
        confirmText: 'Delete',
        cancelText: 'Cancel',
        destructive: true
      } as ConfirmationDialogData
    });

    const confirmed = await firstValueFrom(dialogRef.closed);
    if (!confirmed) {
      return;
    }

    try {
      this.deleting.set(true);
      await this.sessionService.deleteSession(sessionId);
      this.toastService.success(
        'Conversation deleted',
        'The conversation has been permanently deleted.'
      );
      this.router.navigate(['']);
    } catch (error) {
      console.error('Failed to delete session:', error);
      this.toastService.error(
        'Failed to delete',
        'There was an error deleting the conversation. Please try again.'
      );
    } finally {
      this.deleting.set(false);
    }
  }
}
