import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { NgIcon, provideIcons } from '@ng-icons/core';
import { heroInformationCircle, heroXMark } from '@ng-icons/heroicons/outline';
import { SessionService } from '../../services/session/session.service';
import { AgentNoticeService } from '../../services/agent-notice/agent-notice.service';

/**
 * Above the composer: what a project's agent is running this conversation's
 * turn without (the `agent_notice` SSE event). Informational — the turn runs
 * either way — and dismissible. Shows the server's own sentence, so every
 * client says it the same way.
 */
@Component({
  selector: 'app-agent-notice-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgIcon],
  providers: [provideIcons({ heroInformationCircle, heroXMark })],
  template: `
    @if (notice(); as n) {
      <div
        role="status"
        class="mb-2 flex items-start gap-2 rounded-2xl border border-gray-200 bg-white px-3 py-2 text-xs/5 text-gray-700 shadow-xs dark:border-gray-700 dark:bg-gray-800 dark:text-gray-300"
      >
        <ng-icon name="heroInformationCircle" class="mt-0.5 size-4 shrink-0 text-gray-500 dark:text-gray-400" aria-hidden="true" />
        <p class="min-w-0 flex-1">{{ n.message }}</p>
        <button
          type="button"
          (click)="dismiss()"
          aria-label="Dismiss notice"
          class="-my-0.5 -mr-1 grid size-6 shrink-0 place-items-center rounded-2xl text-gray-500 hover:bg-gray-100 hover:text-gray-700 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-primary-500 dark:text-gray-400 dark:hover:bg-gray-700 dark:hover:text-gray-200"
        >
          <ng-icon name="heroXMark" class="size-3.5" aria-hidden="true" />
        </button>
      </div>
    }
  `,
})
export class AgentNoticeBannerComponent {
  private notices = inject(AgentNoticeService);
  private sessions = inject(SessionService);

  private readonly sessionId = computed(() => this.sessions.currentSession().sessionId);
  protected readonly notice = computed(() => this.notices.noticeFor(this.sessionId()));

  protected dismiss(): void {
    const id = this.sessionId();
    if (id) this.notices.dismiss(id);
  }
}
