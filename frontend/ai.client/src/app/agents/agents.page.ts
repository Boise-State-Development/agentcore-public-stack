import { Component, ChangeDetectionStrategy, inject, signal, OnInit } from '@angular/core';
import { Router } from '@angular/router';
import { Dialog } from '@angular/cdk/dialog';
import { firstValueFrom } from 'rxjs';
import { NgIcon, provideIcons } from '@ng-icons/core';
import {
  heroPlus,
  heroPencilSquare,
  heroChatBubbleLeftRight,
  heroShare,
  heroTrash,
  heroCpuChip,
  heroSparkles,
} from '@ng-icons/heroicons/outline';
import { AgentService } from './services/agent.service';
import { AgentListingService } from './services/agent-listing.service';
import { Agent } from './models/agent.model';
import { ListingState } from './models/store.model';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../components/confirmation-dialog/confirmation-dialog.component';
import { ShareAssistantDialogComponent, ShareAssistantDialogData } from '../assistants/components/share-assistant-dialog.component';
import { TooltipDirective } from '../components/tooltip/tooltip.directive';
import { AgentsTabsComponent } from './components/agents-tabs.component';
import { ListingStatusComponent } from './components/listing-status.component';
import {
  SubmitListingDialogComponent,
  SubmitListingDialogData,
  SubmitListingDialogResult,
} from './components/submit-listing-dialog.component';

/**
 * Agent Designer — the list surface, and the author's half of publication.
 *
 * A sibling of the Assistants list page but over the `/agents` surface: each card
 * carries the Agent's model + binding summary (the whole point of an Agent vs. a legacy
 * Assistant). Share reuses the assistants share dialog since the records are the same
 * (agentId == assistantId).
 *
 * This is also the **only** surface from which an Agent reaches the store (D2). The
 * listing state, the reviewer's note and the D13 admin-edit trail all render on the
 * author's own card, and the submit / withdraw controls sit beside them — an author
 * should never have to go looking for the state of their own submission.
 */
@Component({
  selector: 'app-agents',
  templateUrl: './agents.page.html',
  styleUrl: './agents.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, TooltipDirective, AgentsTabsComponent, ListingStatusComponent],
  providers: [
    provideIcons({
      heroPlus,
      heroPencilSquare,
      heroChatBubbleLeftRight,
      heroShare,
      heroTrash,
      heroCpuChip,
      heroSparkles,
    }),
  ],
})
export class AgentsPage implements OnInit {
  private router = inject(Router);
  private agentService = inject(AgentService);
  private listingService = inject(AgentListingService);
  private dialog = inject(Dialog);

  readonly agents = this.agentService.agents$;
  readonly loading = this.agentService.loading$;
  readonly error = this.agentService.error$;

  /**
   * Whether the marketplace is reachable at all. `null` until the probe resolves, so
   * the publication controls appear once rather than flashing in and out, and stay
   * hidden entirely when `AGENT_MARKETPLACE_ENABLED=false` 404s the routes.
   */
  readonly marketplaceAvailable = this.listingService.available;

  /** Agent id whose listing is mid-write; disables that card's controls. */
  readonly listingBusyId = signal<string | null>(null);
  readonly listingError = signal<string | null>(null);

  ngOnInit(): void {
    void this.load();
    // Doubles as the kill-switch probe — see `AgentListingService.available`.
    void this.listingService.loadCategories();
  }

  private async load(): Promise<void> {
    try {
      await this.agentService.loadAgents(true);
    } catch (err) {
      console.error('Error loading agents:', err);
    }
  }

  /** Count bindings by kind for the card summary (KB is welded, shown separately). */
  bindingCount(agent: Agent, kind: string): number {
    return agent.bindings.filter((b) => b.kind === kind).length;
  }

  modelLabel(agent: Agent): string | null {
    return agent.modelConfig?.modelId ?? null;
  }

  async onCreateNew(): Promise<void> {
    try {
      const draft = await this.agentService.createDraft({ name: 'Untitled Agent' });
      this.router.navigate(['/agents', draft.agentId, 'edit']);
    } catch (err) {
      console.error('Error creating draft agent:', err);
    }
  }

  onEdit(agent: Agent): void {
    this.router.navigate(['/agents', agent.agentId, 'edit']);
  }

  onChat(agent: Agent): void {
    this.router.navigate(['/'], { queryParams: { assistantId: agent.agentId } });
  }

  async onShare(agent: Agent): Promise<void> {
    // The share dialog operates on an Assistant shape; agentId == assistantId and
    // the share records are identical, so we adapt the Agent into what it needs.
    const dialogRef = this.dialog.open(ShareAssistantDialogComponent, {
      data: {
        assistant: {
          assistantId: agent.agentId,
          name: agent.name,
          visibility: agent.visibility,
          userPermission: agent.userPermission ?? 'owner',
        },
      } as unknown as ShareAssistantDialogData,
    });
    await firstValueFrom(dialogRef.closed);
  }

  // ── marketplace, the author's half (D2, D7.3) ──────────────────────────────────
  /** Owner-only: an editor may change what an agent does, but not publish it. */
  isOwner(agent: Agent): boolean {
    return (agent.userPermission ?? 'owner') === 'owner';
  }

  private listingState(agent: Agent): ListingState | undefined {
    return agent.listing?.state;
  }

  /**
   * Submittable states, mirroring the backend transition table: never submitted,
   * withdrawn (`private`), returned (`changes_requested`) or delisted (`taken_down`).
   * `in_review` and `published` are deliberately absent — there is nothing to submit.
   */
  canSubmit(agent: Agent): boolean {
    const state = this.listingState(agent);
    return !state || state === 'private' || state === 'changes_requested' || state === 'taken_down';
  }

  submitLabel(agent: Agent): string {
    return this.listingState(agent) ? 'Submit again' : 'Submit to marketplace';
  }

  /** `taken_down` is absent on purpose: the machine allows no author edge out of it. */
  canWithdraw(agent: Agent): boolean {
    const state = this.listingState(agent);
    return state === 'in_review' || state === 'changes_requested' || state === 'published';
  }

  withdrawLabel(agent: Agent): string {
    switch (this.listingState(agent)) {
      case 'published':
        return 'Unpublish';
      case 'in_review':
        return 'Withdraw submission';
      default:
        return 'Withdraw';
    }
  }

  isPublished(agent: Agent): boolean {
    return this.listingState(agent) === 'published';
  }

  onViewInStore(agent: Agent): void {
    this.router.navigate(['/agents', agent.agentId]);
  }

  async onSubmitListing(agent: Agent): Promise<void> {
    this.listingError.set(null);
    const dialogRef = this.dialog.open<SubmitListingDialogResult>(SubmitListingDialogComponent, {
      data: {
        agentId: agent.agentId,
        agentName: agent.name,
        listing: agent.listing,
      } satisfies SubmitListingDialogData,
    });
    // The dialog writes through `AgentListingService`, which patches the card itself;
    // the result is awaited only so a failure inside the dialog stays inside it.
    await firstValueFrom(dialogRef.closed);
  }

  async onWithdrawListing(agent: Agent): Promise<void> {
    const published = this.isPublished(agent);
    const dialogRef = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: published ? 'Unpublish this agent?' : 'Withdraw this submission?',
        // D7.3 — say plainly that this recalls nothing. An author who believes
        // unpublishing revokes access will make a worse decision than one who knows.
        message: published
          ? `"${agent.name}" is removed from the store and no one new can find it there. ` +
            'It revokes nothing retroactively: anyone who already added it keeps it, ' +
            'conversations underway keep running, and it stays reachable by direct link. ' +
            'You can submit it again later.'
          : `"${agent.name}" is pulled from the review queue. You can submit it again at any time.`,
        confirmText: published ? 'Unpublish' : 'Withdraw',
        cancelText: 'Cancel',
        destructive: published,
      } as ConfirmationDialogData,
    });

    if (!(await firstValueFrom(dialogRef.closed))) return;

    this.listingBusyId.set(agent.agentId);
    this.listingError.set(null);
    try {
      await this.listingService.withdraw(agent.agentId);
    } catch (err) {
      const detail = (err as { error?: { detail?: unknown } })?.error?.detail;
      this.listingError.set(
        typeof detail === 'string'
          ? detail
          : `Failed to ${published ? 'unpublish' : 'withdraw'} "${agent.name}".`,
      );
    } finally {
      this.listingBusyId.set(null);
    }
  }

  async onDelete(agent: Agent): Promise<void> {
    const dialogRef = this.dialog.open<boolean>(ConfirmationDialogComponent, {
      data: {
        title: 'Delete Agent',
        message: `Are you sure you want to delete "${agent.name}"? This action cannot be undone.`,
        confirmText: 'Delete',
        cancelText: 'Cancel',
        destructive: true,
      } as ConfirmationDialogData,
    });
    const confirmed = await firstValueFrom(dialogRef.closed);
    if (confirmed) {
      try {
        await this.agentService.deleteAgent(agent.agentId);
      } catch (err) {
        console.error('Error deleting agent:', err);
      }
    }
  }
}
