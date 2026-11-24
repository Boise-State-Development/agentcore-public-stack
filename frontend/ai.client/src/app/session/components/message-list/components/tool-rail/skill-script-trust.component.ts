import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import { HttpClient, HttpParams } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../../../services/config.service';
import { ChatStateService } from '../../../../services/chat/chat-state.service';
import { SkillProvenance } from '../../../../../shared/skills/skill-provenance';
import { reviewDate } from '../../../../../shared/skills/script-review.model';
import type { ToolCallDisplay } from './tool-rail.model';
import { scriptRefused, scriptVersion, skillScriptCall } from './skill-script-call';

/** `GET /skills/scripts/trust`: who stands behind a script that ran in this conversation. */
export interface ScriptRunTrust {
  displayName: string;
  author: string | null;
  version: string | null;
  /** `own`, `imported`, `reviewed`, `admin`, or null when nothing allows it. */
  trustedBy: string | null;
  /** For `reviewed`: `review`, `authored` ("Shared by") or `backfill`. */
  method: string | null;
  reviewer: string | null;
  reviewedAt: string | null;
  provenance: SkillProvenance | null;
}

/** The expanded rail's trust line (§8.6). */
export function trustLine(trust: ScriptRunTrust): string | null {
  const date = reviewDate(trust.reviewedAt);
  const when = date ? `, ${date}` : '';
  switch (trust.trustedBy) {
    case 'own':
      return 'You wrote this skill';
    case 'imported': {
      const p = trust.provenance;
      if (p?.type === 'github' && p.owner && p.repo) return `You imported this skill from ${p.owner}/${p.repo}`;
      if (p?.type === 'zip') return 'You imported this skill from a zip file';
      return 'You imported this skill';
    }
    case 'admin':
      return 'From the admin catalog';
    case 'reviewed':
      if (trust.method === 'authored') return `Shared by ${trust.author ?? 'its author'}${when}`;
      return `Reviewed by ${trust.reviewer ?? 'its owner'}${when}`;
    default:
      return null;
  }
}

/**
 * The opened rail row for a skill script run: the skill, its author and version, whose
 * decision let it run, and where it ran. Fetched when the row is opened, so a collapsed
 * rail costs nothing. Display names only; the endpoint never returns user ids.
 */
@Component({
  selector: 'app-skill-script-trust',
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (script(); as s) {
      <div class="call-script text-xs/5 text-gray-600 dark:text-gray-300" role="group" [attr.aria-label]="'About ' + s.filename">
        <div class="detail-label text-gray-500 dark:text-gray-400">Script</div>
        <p>
          <span class="font-medium text-gray-800 dark:text-gray-100">{{ trust.value()?.displayName ?? s.skill }}</span>
          @if (trust.value()?.author; as author) { by {{ author }} }
          @if (version(); as v) { · version {{ v }} }
        </p>
        @if (trust.value(); as t) {
          @if (line(t); as l) {
            <p>{{ l }}</p>
          }
        }
        @if (!refused()) {
          <p>Ran in a sandbox with no internet access</p>
        }
      </div>
    }
  `,
})
export class SkillScriptTrustComponent {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);
  private readonly chatState = inject(ChatStateService);

  readonly call = input.required<ToolCallDisplay>();

  protected readonly script = computed(() => skillScriptCall(this.call()));
  protected readonly version = computed(() => scriptVersion(this.call()));
  protected readonly refused = computed(() => this.call().status === 'error' || scriptRefused(this.call()));

  protected readonly trust = resource({
    params: () => {
      const script = this.script();
      const sessionId = this.chatState.viewedSessionId();
      return script && sessionId ? { name: script.skill, sessionId, version: this.version() } : undefined;
    },
    loader: async ({ params }) => {
      let query = new HttpParams().set('name', params.name).set('sessionId', params.sessionId);
      if (params.version) query = query.set('version', params.version);
      try {
        return await firstValueFrom(
          this.http.get<ScriptRunTrust>(`${this.config.appApiUrl()}/skills/scripts/trust`, { params: query }),
        );
      } catch {
        // The row still says what ran; the trust line is the part that couldn't be read.
        return null;
      }
    },
  });

  protected line(trust: ScriptRunTrust): string | null {
    return trustLine(trust);
  }
}
