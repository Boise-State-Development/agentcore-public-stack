import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
  untracked,
} from '@angular/core';
import { AgentSkillPin, AgentSkillPinMoved } from '../../models/agent.model';
import { AgentService } from '../../services/agent.service';

/**
 * The Agent form's Update for pinned skills (docs/specs/skill-version-history.md D9).
 *
 * A pinned skill keeps running its version on purpose, and until now an Agent's pin
 * moved only if someone turned the skill off and on again, with nothing saying a
 * newer version existed. This lists each pinned skill whose author has changed it
 * since, with **Update**, which moves the pin at once (`POST …/skills/{id}/pin`).
 *
 * `pinned` carries the new version back: the form sends each binding's stored pin
 * on save, so it must keep this one, or a later save would put the old pin back.
 * The skills picker can't host a button per row, which is why this is a strip of
 * its own under it.
 */
@Component({
  selector: 'app-agent-skill-pins',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (behind().length > 0 || moved().length > 0) {
      <section
        aria-labelledby="agent-skill-pins-heading"
        class="mt-4 rounded-2xl border border-gray-200 p-4 dark:border-gray-700"
      >
        <h3 id="agent-skill-pins-heading" class="text-sm/6 font-medium text-gray-900 dark:text-white">
          Skill updates
        </h3>
        <p class="mt-1 text-sm/6 text-gray-600 dark:text-gray-400">
          This agent pins these skills to a version, and their authors have changed them since.
          If this agent is published, resubmit it so an update reaches the people using it.
        </p>
        @if (error()) {
          <p role="alert" class="mt-2 text-sm/6 text-state-danger-700 dark:text-state-danger-400">
            {{ error() }}
          </p>
        }
        <ul role="list" class="mt-3 flex flex-col gap-2">
          @for (pin of behind(); track pin.skillId) {
            <li class="flex flex-wrap items-center justify-between gap-3 text-sm/6">
              <span class="min-w-0 text-gray-700 dark:text-gray-300">
                <span class="font-medium text-gray-900 dark:text-white">{{ label(pin.skillId) }}</span>
                · on version {{ pin.version }} · Update available
              </span>
              <button
                type="button"
                (click)="update(pin)"
                [disabled]="busy() === pin.skillId"
                [attr.aria-label]="'Update ' + label(pin.skillId)"
                class="rounded-2xl border border-gray-300 bg-white px-3 py-1 text-sm/6 font-medium text-gray-700 hover:bg-gray-50 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:opacity-50 dark:border-gray-600 dark:bg-gray-800 dark:text-gray-300 dark:hover:bg-gray-700"
              >
                {{ busy() === pin.skillId ? 'Updating…' : 'Update' }}
              </button>
            </li>
          }
          @for (m of moved(); track m.skillId) {
            <li role="status" class="text-sm/6 text-state-success-700 dark:text-state-success-400">
              {{ label(m.skillId) }} now uses version {{ m.version }}.
            </li>
          }
        </ul>
      </section>
    }
  `,
})
export class AgentSkillPinsComponent {
  private readonly agents = inject(AgentService);

  readonly agentId = input.required<string>();
  /** Skill names by id, from the form's skills picker. */
  readonly labels = input<Record<string, string>>({});
  /** The binding's new pin, for the form to keep. */
  readonly pinned = output<AgentSkillPinMoved>();

  private readonly pins = signal<AgentSkillPin[]>([]);
  protected readonly moved = signal<AgentSkillPinMoved[]>([]);
  protected readonly busy = signal<string | null>(null);
  protected readonly error = signal<string | null>(null);

  protected readonly behind = computed(() =>
    this.pins().filter(
      (p) => p.updateAvailable && !this.moved().some((m) => m.skillId === p.skillId),
    ),
  );

  constructor() {
    effect(() => {
      const id = this.agentId();
      untracked(() => void this.load(id));
    });
  }

  protected label(skillId: string): string {
    return this.labels()[skillId] ?? skillId;
  }

  protected async update(pin: AgentSkillPin): Promise<void> {
    this.busy.set(pin.skillId);
    this.error.set(null);
    try {
      const version = await this.agents.updateSkillPin(this.agentId(), pin.skillId);
      const moved = { skillId: pin.skillId, version };
      this.moved.update((rows) => [...rows, moved]);
      this.pinned.emit(moved);
    } catch (err) {
      const detail = (err as { error?: { detail?: unknown } } | null)?.error?.detail;
      this.error.set(
        typeof detail === 'string' && detail.trim()
          ? detail
          : `We couldn't update ${this.label(pin.skillId)}. Try again.`,
      );
    } finally {
      this.busy.set(null);
    }
  }

  private async load(agentId: string): Promise<void> {
    try {
      const pins = await this.agents.getSkillPins(agentId);
      if (agentId === this.agentId()) {
        this.pins.set(pins);
        this.moved.set([]);
      }
    } catch {
      // A strip that couldn't load says nothing; the skills themselves still work.
      this.pins.set([]);
    }
  }
}
