import { ChangeDetectionStrategy, Component, computed, inject, input, resource, signal } from '@angular/core';
import { HttpClient, HttpErrorResponse } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { reviewDate } from '../script-review.model';

/** The part of `GET /skills/{id}/script-trust` this reads: whether the scripts are off, and by whom. */
interface ScriptsSwitchState {
  scriptsOff: { by: 'author' | 'admin' | string; at?: string | null } | null;
}

/**
 * The author's **Scripts can run** switch (docs/specs/skill-code-sandbox.md §8.4–§8.5),
 * on their own skill's page when it has scripts. Off, the skill's scripts refuse for
 * everyone, the author included, in every Agent and project that uses it. When an
 * administrator turned them off, the switch is replaced by a line saying so: only an
 * administrator can turn them back on.
 */
@Component({
  selector: 'app-skill-scripts-switch',
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'block' },
  template: `
    @if (state.hasValue()) {
      @if (adminOff(); as off) {
        <p class="text-sm/6 text-state-warning-700 dark:text-state-warning-300">
          An administrator turned off this skill's scripts{{ off.date ? ' on ' + off.date : '' }}.
        </p>
      } @else {
        <div class="flex items-start justify-between gap-4">
          <div class="min-w-0">
            <p id="scripts-switch-label" class="text-sm/6 font-medium text-gray-900 dark:text-white">Scripts can run</p>
            <p id="scripts-switch-help" class="text-sm/6 text-gray-600 dark:text-gray-400">
              Turn this off to stop this skill's scripts from running anywhere, including in the
              Agents and projects that use it.
            </p>
          </div>
          <button
            type="button"
            role="switch"
            [attr.aria-checked]="on()"
            aria-labelledby="scripts-switch-label"
            aria-describedby="scripts-switch-help"
            [disabled]="busy()"
            (click)="toggle()"
            class="relative inline-flex h-6 w-11 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 disabled:cursor-not-allowed disabled:opacity-50"
            [class]="on() ? 'bg-primary-accessible' : 'bg-gray-300 dark:bg-gray-600'"
          >
            <span
              aria-hidden="true"
              class="pointer-events-none inline-block size-5 rounded-full bg-white shadow ring-0 transition-transform motion-reduce:transition-none"
              [class]="on() ? 'translate-x-5' : 'translate-x-0'"
            ></span>
          </button>
        </div>
      }
      @if (error()) {
        <p role="alert" class="mt-2 text-sm/6 text-state-danger-700 dark:text-state-danger-400">{{ error() }}</p>
      }
    }
  `,
})
export class SkillScriptsSwitchComponent {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);

  readonly skillId = input.required<string>();

  protected readonly busy = signal(false);
  protected readonly error = signal<string | null>(null);

  protected readonly state = resource({
    params: () => ({ skillId: this.skillId() }),
    loader: ({ params }) =>
      firstValueFrom(
        this.http.get<ScriptsSwitchState>(
          `${this.config.appApiUrl()}/skills/${encodeURIComponent(params.skillId)}/script-trust`,
        ),
      ),
  });

  protected readonly on = computed(() => !this.state.value()?.scriptsOff);

  protected readonly adminOff = computed(() => {
    const off = this.state.value()?.scriptsOff;
    return off?.by === 'admin' ? { date: reviewDate(off.at) } : null;
  });

  protected async toggle(): Promise<void> {
    const off = this.on();
    this.busy.set(true);
    this.error.set(null);
    try {
      const result = await firstValueFrom(
        this.http.put<{ off: boolean; by: string | null }>(
          `${this.config.appApiUrl()}/skills/mine/${encodeURIComponent(this.skillId())}/scripts`,
          { off },
        ),
      );
      this.state.set({ scriptsOff: result.off ? { by: result.by ?? 'author' } : null });
    } catch (err) {
      if (err instanceof HttpErrorResponse && err.status === 409) {
        // An administrator got there first; show what they set.
        this.state.reload();
      }
      this.error.set(off ? "This skill's scripts couldn't be turned off. Try again." : "This skill's scripts couldn't be turned on. Try again.");
    } finally {
      this.busy.set(false);
    }
  }
}
