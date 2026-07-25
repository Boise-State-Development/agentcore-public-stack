import { Component, ChangeDetectionStrategy, input, computed } from '@angular/core';
import {
  AgentIconComponent,
  AgentIconSize,
} from '../../../agents/components/agent-icon.component';

/**
 * The small square that identifies an agent in the admin tables.
 *
 * A thin adapter over `app-agent-icon` (Phase 4): the reviewer sees the same tile a
 * browsing user will — the uploaded icon, or the generated gradient derived from the
 * agent id. That matters for the two admin jobs that involve an icon: catching an
 * off-brand one in the review queue, and judging a D13 icon swap against what the shelf
 * actually renders.
 */
@Component({
  selector: 'app-agent-tile',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AgentIconComponent],
  template: `
    <app-agent-icon
      [agentId]="agentId()"
      [iconUrl]="iconUrl()"
      [emoji]="emoji()"
      [size]="iconSize()"
    />
  `,
})
export class AgentTileComponent {
  readonly agentId = input.required<string>();
  readonly emoji = input<string | undefined>();
  readonly iconUrl = input<string | undefined>();
  readonly size = input<'sm' | 'md'>('md');

  readonly iconSize = computed<AgentIconSize>(() => (this.size() === 'sm' ? 28 : 40));
}
