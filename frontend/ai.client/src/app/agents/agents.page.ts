import { Component, ChangeDetectionStrategy, inject, OnInit, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { NgTemplateOutlet } from '@angular/common';
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
  heroSparkles,
  heroSquares2x2,
  heroBars3,
  heroDocumentDuplicate,
  heroLockClosed,
} from '@ng-icons/heroicons/outline';
import { AGENT_DUPLICATED_TOAST, AgentService } from './services/agent.service';
import { ToastService } from '../services/toast/toast.service';
import { Agent } from './models/agent.model';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../components/confirmation-dialog/confirmation-dialog.component';
import { ShareAgentDialogComponent, ShareAgentDialogData } from './components/share-agent-dialog.component';
import { TemplatePickerDialogComponent } from './components/template-picker-dialog.component';
import { TooltipDirective } from '../components/tooltip/tooltip.directive';
import { AgentsTabsComponent } from './components/agents-tabs.component';
import { AgentIconComponent } from './components/agent-icon.component';
import { AgentPinService } from './services/agent-pin.service';
import { appPin } from '../components/sidenav/components/session-list/pin-icons';
import { AgentsViewMode, LocalSettingsService } from '../services/local-settings.service';

/**
 * Agent Designer — the list surface.
 *
 * A card is an **index entry**, not a summary: icon, name, one line, and the handful of
 * verbs you act on it with. The model, the binding counts and the whole publication
 * apparatus used to live here too, and between them they turned a page you scan into a
 * page you read.
 *
 * Publication now lives in the share dialog, with the rest of "who can reach this?" —
 * people, the link and the store listing are one question asked at three widths, and
 * splitting them across a card rail and an editor section made them read as three
 * unrelated features.
 */
@Component({
  selector: 'app-agents',
  templateUrl: './agents.page.html',
  styleUrl: './agents.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    NgIcon,
    NgTemplateOutlet,
    TooltipDirective,
    AgentsTabsComponent,
    AgentIconComponent,
  ],
  providers: [
    provideIcons({
      appPin,
      heroPlus,
      heroPencilSquare,
      heroChatBubbleLeftRight,
      heroShare,
      heroTrash,
      heroSparkles,
      heroSquares2x2,
      heroBars3,
      heroDocumentDuplicate,
      heroLockClosed,
    }),
  ],
})
export class AgentsPage implements OnInit {
  private router = inject(Router);
  private agentService = inject(AgentService);
  private pinService = inject(AgentPinService);
  private localSettings = inject(LocalSettingsService);
  private dialog = inject(Dialog);
  private toast = inject(ToastService);
  /** The agent currently being duplicated; disables the Duplicate buttons meanwhile. */
  readonly duplicatingId = signal<string | null>(null);
  /** The agent whose pin is being toggled; disables that row's Pin button meanwhile. */
  readonly pinningId = signal<string | null>(null);

  readonly agents = this.agentService.agents$;
  readonly loading = this.agentService.loading$;
  readonly error = this.agentService.error$;

  /**
   * Grid or list, remembered per device (`LocalSettingsService`).
   *
   * Both views render the same agents with the same controls — the toggle changes
   * density, never what is available.
   */
  readonly viewMode = this.localSettings.agentsViewMode;

  ngOnInit(): void {
    void this.load();
    // Session-wide and loaded once, so this is free when the sidebar already asked. A
    // failure leaves every row reading "not pinned", which is the safe direction.
    void this.pinService.load();
  }

  private async load(): Promise<void> {
    try {
      await this.agentService.loadAgents(true);
    } catch (err) {
      console.error('Error loading agents:', err);
    }
  }

  setViewMode(mode: AgentsViewMode): void {
    this.localSettings.setAgentsViewMode(mode);
  }

  /**
   * Open the "Start from a template" picker. The dialog owns the whole flow — it stashes
   * the chosen template in `localStorage` and navigates to the create-agent form — so
   * there is nothing to do with its result here.
   */
  onStartFromTemplate(): void {
    this.dialog.open(TemplatePickerDialogComponent, {});
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

  canDuplicate(agent: Agent): boolean {
    return ['owner', 'editor'].includes(agent.userPermission ?? 'owner');
  }

  async onDuplicate(agent: Agent): Promise<void> {
    if (this.duplicatingId()) return;
    this.duplicatingId.set(agent.agentId);
    try {
      const copy = await this.agentService.duplicateAgent(agent.agentId);
      await this.router.navigate(['/agents', copy.agentId, 'edit']);
      this.toast.info(AGENT_DUPLICATED_TOAST.title, AGENT_DUPLICATED_TOAST.message);
    } catch (err) {
      console.error('Error duplicating agent:', err);
      this.toast.error('Could not duplicate agent', (err as HttpErrorResponse)?.error?.detail ?? 'Please try again.');
    } finally {
      this.duplicatingId.set(null);
    }
  }

  onChat(agent: Agent): void {
    this.router.navigate(['/'], { queryParams: { assistantId: agent.agentId } });
  }

  isPinned(agent: Agent): boolean {
    return this.pinService.isPinned(agent.agentId);
  }

  /** A role pin an admin locked (D9.4): the row shows a lock instead of the toggle. */
  isPinLocked(agent: Agent): boolean {
    return this.pinService.isLocked(agent.agentId);
  }

  pinTooltip(agent: Agent): string {
    return this.isPinned(agent) ? 'Unpin from sidebar' : 'Pin to sidebar';
  }

  lockTooltip(agent: Agent): string {
    return `${agent.name} is pinned for your role and can't be removed`;
  }

  /**
   * Pin or unpin from the sidebar (#1897). Every row qualifies: pinning is gated on
   * reach, not publication, and this list is exactly the agents the caller can reach.
   * Project harnesses never appear here, so the server's refusal of those never shows.
   */
  async onTogglePin(agent: Agent): Promise<void> {
    if (this.pinningId()) return;
    this.pinningId.set(agent.agentId);
    try {
      await this.pinService.toggle(agent.agentId);
    } catch {
      // The service has already rolled the list back and holds the message.
      this.toast.error('Could not update pin', this.pinService.error() ?? 'Please try again.');
    } finally {
      this.pinningId.set(null);
    }
  }

  /**
   * Everything about who can reach this agent — people, the link, and the marketplace
   * listing — lives in this one dialog. `agentId == assistantId`, so the share records
   * and the agent record are the same thing under two names.
   */
  async onShare(agent: Agent): Promise<void> {
    const dialogRef = this.dialog.open(ShareAgentDialogComponent, {
      data: {
        agent: {
          assistantId: agent.agentId,
          name: agent.name,
          visibility: agent.visibility,
          userPermission: agent.userPermission ?? 'owner',
          emoji: agent.emoji,
          iconUrl: agent.iconUrl,
        },
      } satisfies ShareAgentDialogData,
    });
    await firstValueFrom(dialogRef.closed);
    // The dialog can publish, withdraw or widen visibility; re-read so the card it was
    // opened from is not the one surface still showing the old state.
    await this.agentService.loadAgents(true);
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
        // A pinned agent would otherwise linger in the sidebar until the next full load.
        if (this.isPinned(agent)) void this.pinService.load(true);
      } catch (err) {
        console.error('Error deleting agent:', err);
      }
    }
  }
}
