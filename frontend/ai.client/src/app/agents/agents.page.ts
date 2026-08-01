import { Component, ChangeDetectionStrategy, inject, OnInit } from '@angular/core';
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
} from '@ng-icons/heroicons/outline';
import { AgentService } from './services/agent.service';
import { Agent } from './models/agent.model';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../components/confirmation-dialog/confirmation-dialog.component';
import { ShareAgentDialogComponent, ShareAgentDialogData } from './components/share-agent-dialog.component';
import { TooltipDirective } from '../components/tooltip/tooltip.directive';
import { AgentsTabsComponent } from './components/agents-tabs.component';
import { AgentIconComponent } from './components/agent-icon.component';
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
    TooltipDirective,
    AgentsTabsComponent,
    AgentIconComponent,
  ],
  providers: [
    provideIcons({
      heroPlus,
      heroPencilSquare,
      heroChatBubbleLeftRight,
      heroShare,
      heroTrash,
      heroSparkles,
      heroSquares2x2,
      heroBars3,
    }),
  ],
})
export class AgentsPage implements OnInit {
  private router = inject(Router);
  private agentService = inject(AgentService);
  private localSettings = inject(LocalSettingsService);
  private dialog = inject(Dialog);

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
      } catch (err) {
        console.error('Error deleting agent:', err);
      }
    }
  }
}
