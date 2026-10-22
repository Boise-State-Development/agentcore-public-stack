import { DOCUMENT } from '@angular/common';
import { Injectable, effect, inject, signal, untracked } from '@angular/core';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { Dialog, DialogRef } from '@angular/cdk/dialog';
import { NavigationEnd, Router } from '@angular/router';
import { filter } from 'rxjs/operators';
import { AnnouncementsService } from './announcements.service';
import {
  AnnouncementModalComponent,
  AnnouncementModalData,
} from '../../components/announcement-modal/announcement-modal.component';
import { Announcement, AnnouncementSurface } from './announcement.model';
import { SessionService } from '../../auth/session.service';
import { MessageMapService } from '../../session/services/session/message-map.service';
import { ToolApprovalService } from '../tool-approval/tool-approval.service';
import { OAuthConsentService } from '../oauth-consent/oauth-consent.service';
import { McpAppConsentService } from '../../session/services/mcp-apps/mcp-app-consent.service';
import { isMinimalChromeRoute } from '../../shared/utils/route-chrome';

/**
 * Decides *whether* to interrupt with an announcement modal (§D8).
 *
 * A service rather than a component in the app shell, for two reasons: the
 * modal is a CDK overlay and has no place in the layout, and self-starting via
 * `provideAppInitializer` keeps the trigger out of `app.html` entirely — the
 * same shape `ThemeService` already uses.
 *
 * ## The gate
 *
 * The modal opens on route settle, and only when all of these hold:
 *
 * - no active stream (`MessageMapService.isLoadingSession()` is null)
 * - no pending tool-approval, OAuth-consent, or MCP-App consent prompt
 * - the composer is not focused with a non-empty draft
 * - the route is not a minimal-chrome page (a shared artifact opened from a
 *   link is the whole point of that visit; do not put a dialog over it)
 *
 * **The consent checks are not belt-and-braces.** Per
 * `docs/specs/mid-turn-steering.md` (#934), `isLoading()` is `false` while a
 * turn is paused on an interrupt — so a stream-only check would happily throw
 * a modal over an OAuth consent dialog and steal its focus. The prompt
 * services are asked directly. (The spec names tool-approval and
 * oauth-consent; MCP-App consent is the same class of prompt and is included
 * for the same reason.)
 *
 * ## Why the gate inputs are read untracked
 *
 * `isLoadingSession` and the three `hasPending` signals are all reactive, so
 * reading them normally inside the effect would re-run it the instant a stream
 * ends or a consent is answered — and fire a modal into the middle of the
 * user's session, seconds after they finished a thought. §D8 is explicit that
 * a failed gate must **not** queue the modal for later: it stays eligible and
 * opens on the next clean load. So the effect tracks only the announcement
 * itself and the navigation counter, and takes everything else as a snapshot.
 * Deferred modals that fire minutes later, mid-thought, are the worst version
 * of this feature.
 */
@Injectable({ providedIn: 'root' })
export class AnnouncementModalService {
  private readonly announcements = inject(AnnouncementsService);
  private readonly dialog = inject(Dialog);
  private readonly router = inject(Router);
  private readonly document = inject(DOCUMENT);
  private readonly session = inject(SessionService);
  private readonly messageMap = inject(MessageMapService);
  private readonly toolApproval = inject(ToolApprovalService);
  private readonly oauthConsent = inject(OAuthConsentService);
  private readonly mcpAppConsent = inject(McpAppConsentService);

  /**
   * Bumped on every completed navigation. The effect tracks this rather than
   * the router directly, so "route settle" is a reactive input and a gate that
   * failed on one page gets a fresh chance on the next.
   */
  private readonly navigations = signal(0);

  /** Announcements already shown in this tab, so a re-navigation is not a re-open. */
  private readonly shown = new Set<string>();

  private openRef: DialogRef<void> | null = null;

  constructor() {
    this.router.events
      .pipe(
        filter((e): e is NavigationEnd => e instanceof NavigationEnd),
        takeUntilDestroyed(),
      )
      .subscribe(() => this.navigations.update(n => n + 1));

    effect(() => {
      const item = this.announcements.modalItem();
      // Tracked deliberately: a settled navigation is the retry opportunity.
      this.navigations();

      if (!item) return;
      untracked(() => {
        if (this.shown.has(item.announcement_id)) return;
        if (!this.canInterrupt()) return;
        this.open(item, 'modal');
      });
    });
  }

  /**
   * Open the detail dialog for one announcement **because the user asked**.
   *
   * The banner calls this when its text is clicked: the pill is one line and
   * renders no body, so without a way in, the only affordances on it are ✕ and
   * an optional CTA — which is how you train people to dismiss unread.
   *
   * Two things it deliberately does *not* do. It does not consult
   * `canInterrupt()`: that gate exists to stop us throwing a dialog at someone
   * mid-thought, and a click is not an interruption — the user is the one
   * asking. It *does* respect `openRef`, because two stacked dialogs is a bug
   * whoever opened them.
   *
   * It also marks the announcement `shown`, so the §D8 effect cannot re-open
   * the same item as an interruption after the user has already read it here.
   */
  openFor(
    announcement: Announcement,
    sourceSurface: AnnouncementSurface = 'modal',
  ): void {
    if (this.openRef !== null) return;
    this.open(announcement, sourceSurface);
  }

  /** The single place a dialog is constructed, so `openRef` cannot drift. */
  private open(
    announcement: Announcement,
    sourceSurface: AnnouncementSurface,
  ): void {
    this.shown.add(announcement.announcement_id);
    this.openRef = this.dialog.open<void, AnnouncementModalData>(
      AnnouncementModalComponent,
      {
        data: { announcement, sourceSurface },
        hasBackdrop: false, // the dialog component owns its own backdrop
        // The only exit from a `requiresAck` announcement is its button —
        // including when the user opened it themselves from the banner.
        disableClose: announcement.requires_ack,
        panelClass: 'announcement-modal',
      },
    );
    this.openRef.closed.subscribe(() => {
      this.openRef = null;
    });
  }

  /**
   * The §D8 gate. Every read here is a snapshot — see the class comment for
   * why this must not be reactive.
   */
  private canInterrupt(): boolean {
    if (this.openRef !== null) return false;
    if (!this.session.isAuthenticated()) return false;
    if (isMinimalChromeRoute(this.router.routerState.snapshot.root)) return false;

    if (this.messageMap.isLoadingSession() !== null) return false;
    if (this.toolApproval.hasPending()) return false;
    if (this.oauthConsent.hasPending()) return false;
    if (this.mcpAppConsent.pending().length > 0) return false;

    return !this.composerHasDraft();
  }

  /**
   * Whether the user is mid-sentence in the composer.
   *
   * Read from the DOM rather than a shared signal because the draft has no
   * home outside `chat-input` — it lives in that component's own state. The
   * question is inherently "what is focused right now", which is a DOM
   * question, and asking it at gate time is exactly when the answer matters.
   * Scoped by `closest('app-chat-input')` rather than the textarea's id, so a
   * template rename cannot silently turn this check off.
   */
  private composerHasDraft(): boolean {
    const active = this.document.activeElement as HTMLElement | null;
    if (!active || !active.closest('app-chat-input')) return false;
    const value = (active as HTMLTextAreaElement | HTMLInputElement).value;
    return typeof value === 'string' && value.trim().length > 0;
  }
}
