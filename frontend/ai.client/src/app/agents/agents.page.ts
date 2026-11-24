import { Component, ChangeDetectionStrategy, computed, inject, OnInit, signal } from '@angular/core';
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
  heroEye,
  heroEyeSlash,
  heroArrowsUpDown,
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
import { FEATURES } from '../services/features';
import { ListArrangementService } from '../services/list-arrangement/list-arrangement.service';
import { ArrangementSort, arrangeList } from '../services/list-arrangement/list-arrangement.models';
import { ArrangeListComponent, ArrangeResult, ArrangeRow } from '../components/arrange-list/arrange-list.component';

type AgentFilter = 'all' | 'mine' | 'shared' | 'hidden';

const AGENT_FILTERS: { value: AgentFilter; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'mine', label: 'Mine' },
  { value: 'shared', label: 'Shared with me' },
  { value: 'hidden', label: 'Hidden' },
];

const SORTS: { value: ArrangementSort; label: string }[] = [
  { value: 'recent', label: 'Recently added' },
  { value: 'name', label: 'Name' },
  { value: 'custom', label: 'Custom' },
];

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
    ArrangeListComponent,
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
      heroEye,
      heroEyeSlash,
      heroArrowsUpDown,
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
  private arrangements = inject(ListArrangementService);
  protected readonly features = inject(FEATURES);
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

  /** The agent being hidden or unhidden; disables that row's Hide button meanwhile. */
  readonly hidingId = signal<string | null>(null);

  /**
   * Which agents to show (docs/specs/agent-project-organization.md §6.2). Only offered with
   * `features.organizeLists`; without it the page lists every agent, as before.
   */
  readonly filter = signal<AgentFilter>('all');

  private readonly arrangement = this.arrangements.arrangement('agents');

  private readonly arranged = computed(() =>
    arrangeList(this.agents(), {
      idOf: (agent) => agent.agentId,
      nameOf: (agent) => agent.name,
      arrangement: this.arrangement(),
    }),
  );

  protected readonly sorts = SORTS;
  readonly sort = computed(() => this.arrangement().sort);

  /** Arrange mode (spec §6.3): the page becomes one list the user reorders and hides from. */
  readonly arranging = signal(false);

  /** Every agent, shown ones then hidden ones, as Arrange starts from them. */
  readonly arrangeRows = computed<ArrangeRow[]>(() => {
    const { visible, hidden } = this.arranged();
    const hiddenIds = new Set(hidden.map((agent) => agent.agentId));
    return [...visible, ...hidden].map((agent) => ({
      id: agent.agentId,
      name: agent.name,
      detail: agent.isSharedWithMe ? 'Shared with you' : undefined,
      hidden: hiddenIds.has(agent.agentId),
      cannotHide: this.isPinLocked(agent),
    }));
  });

  readonly hiddenCount = computed(() => this.arranged().hidden.length);

  /** The chips: Hidden appears once something is hidden, or while it is the chosen filter. */
  readonly filters = computed(() =>
    AGENT_FILTERS.filter((f) => f.value !== 'hidden' || this.hiddenCount() > 0 || this.filter() === 'hidden'),
  );

  /** The agents the current filter shows, in the list's own order. */
  readonly shownAgents = computed(() => {
    if (!this.features.organizeLists) return this.agents();
    const { visible, hidden } = this.arranged();
    switch (this.filter()) {
      case 'hidden':
        return hidden;
      case 'mine':
        return visible.filter((agent) => !agent.isSharedWithMe);
      case 'shared':
        return visible.filter((agent) => agent.isSharedWithMe === true);
      default:
        return visible;
    }
  });

  ngOnInit(): void {
    void this.load();
    // Session-wide and loaded once, so this is free when the sidebar already asked. A
    // failure leaves every row reading "not pinned", which is the safe direction.
    void this.pinService.load();
    this.arrangements.open('agents');
  }

  setFilter(value: AgentFilter): void {
    this.filter.set(value);
  }

  filterLabel(f: { value: AgentFilter; label: string }): string {
    return f.value === 'hidden' ? `${f.label} (${this.hiddenCount()})` : f.label;
  }

  /** Badged under a saved custom order when it arrived since the user last arranged (D6). */
  isNew(agent: Agent): boolean {
    return this.arranged().newIds.has(agent.agentId);
  }

  async setSort(value: ArrangementSort): Promise<void> {
    if (value === this.sort()) return;
    try {
      await this.arrangements.setSort('agents', value, this.pageIds());
    } catch {
      this.toast.error('Could not change the order', 'Please try again.');
    }
  }

  startArranging(): void {
    this.filter.set('all');
    this.arranging.set(true);
  }

  /**
   * Save an arrangement. An agent newly hidden here is unpinned first, as Hide does (D8); a
   * locked role pin is never offered the box.
   */
  async onArranged(result: ArrangeResult): Promise<void> {
    this.arranging.set(false);
    const wasHidden = this.arrangements.hiddenIds('agents')();
    const toUnpin = [...result.hiddenIds].filter(
      (id) => !wasHidden.has(id) && this.pinService.isPinned(id) && !this.pinService.isLocked(id),
    );
    try {
      for (const id of toUnpin) await this.pinService.unpin(id);
      await this.arrangements.saveArranged('agents', result.ids, result.hiddenIds, this.pageIds());
    } catch {
      this.toast.error('Could not save your order', 'Please try again.');
    }
  }

  isHidden(agent: Agent): boolean {
    return this.arrangements.hiddenIds('agents')().has(agent.agentId);
  }

  /**
   * Hide or unhide one agent from this list (spec D1). Access is untouched: it still opens
   * from a link, runs, and answers to `@` by name. Hiding a pinned agent unpins it first
   * (D8), so the sidebar never shows something the user took off the page; Undo puts both
   * back. A locked role pin is never offered Hide.
   */
  async onToggleHidden(agent: Agent): Promise<void> {
    if (this.hidingId()) return;
    const hide = !this.isHidden(agent);
    if (hide && this.isPinLocked(agent)) return;
    const wasPinned = hide && this.isPinned(agent);
    this.hidingId.set(agent.agentId);
    try {
      if (wasPinned) await this.pinService.unpin(agent.agentId);
      await this.arrangements.setHidden('agents', agent.agentId, hide, this.pageIds());
      if (hide) {
        this.toast.info(
          `Hid ${agent.name}`,
          wasPinned ? 'It is unpinned from your sidebar too. Find it under Hidden.' : 'Find it under Hidden.',
          { action: { label: 'Undo', handler: () => void this.undoHide(agent, wasPinned) } },
        );
      }
    } catch {
      this.toast.error(hide ? 'Could not hide agent' : 'Could not unhide agent', 'Please try again.');
    } finally {
      this.hidingId.set(null);
    }
  }

  private async undoHide(agent: Agent, repin: boolean): Promise<void> {
    try {
      await this.arrangements.setHidden('agents', agent.agentId, false, this.pageIds());
      if (repin) await this.pinService.pin(agent.agentId);
    } catch {
      this.toast.error('Could not undo', 'Please try again.');
    }
  }

  /** Every agent id the page lists, in the list's own (most recent first) order. */
  private pageIds(): string[] {
    return this.agents().map((agent) => agent.agentId);
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
