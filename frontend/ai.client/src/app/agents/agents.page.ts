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
  heroCpuChip,
  heroSparkles,
} from '@ng-icons/heroicons/outline';
import { AgentService } from './services/agent.service';
import { Agent } from './models/agent.model';
import {
  ConfirmationDialogComponent,
  ConfirmationDialogData,
} from '../components/confirmation-dialog/confirmation-dialog.component';
import { ShareAssistantDialogComponent, ShareAssistantDialogData } from '../assistants/components/share-assistant-dialog.component';
import { TooltipDirective } from '../components/tooltip/tooltip.directive';

/**
 * Agent Designer — the list surface. A sibling of the Assistants list page but
 * over the `/agents` surface: each card carries the Agent's model + binding
 * summary (the whole point of an Agent vs. a legacy Assistant). Share reuses the
 * assistants share dialog since the records are the same (agentId == assistantId).
 */
@Component({
  selector: 'app-agents',
  templateUrl: './agents.page.html',
  styleUrl: './agents.page.css',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon, TooltipDirective],
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
  private dialog = inject(Dialog);

  readonly agents = this.agentService.agents$;
  readonly loading = this.agentService.loading$;
  readonly error = this.agentService.error$;

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
