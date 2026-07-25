import { Component, ChangeDetectionStrategy, input } from '@angular/core';

/**
 * The small square that identifies an agent in the admin tables.
 *
 * Phase 1 renders the agent's emoji on a neutral tile. D5's real identity work — an
 * uploaded 512×512 icon plus a *generated gradient* fallback derived from the agent id,
 * at four render sizes — is Phase 4. Deliberately not approximating that here: a
 * throwaway gradient would be one more thing Phase 4 has to unpick, and nothing in
 * Phase 1 is user-visible.
 */
@Component({
  selector: 'app-agent-tile',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    <span
      class="flex shrink-0 items-center justify-center rounded-2xl bg-gray-100 dark:bg-gray-700"
      [class]="sizeClass()"
      aria-hidden="true"
    >
      {{ emoji() || '✦' }}
    </span>
  `,
})
export class AgentTileComponent {
  readonly emoji = input<string | undefined>();
  readonly size = input<'sm' | 'md'>('md');

  sizeClass(): string {
    return this.size() === 'sm' ? 'size-7 text-sm' : 'size-10 text-xl';
  }
}
