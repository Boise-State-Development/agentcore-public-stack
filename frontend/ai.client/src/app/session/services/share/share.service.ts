import { inject, Injectable, computed } from '@angular/core';
import { HttpClient, HttpContext } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { SUPPRESS_ERROR_TOAST } from '../../../auth/error.interceptor';
import { Message } from '../models/message.model';
import type { RenderToken } from '../artifacts/artifact-http.service';

// ------------------------------------------------------------------
// Interfaces
// ------------------------------------------------------------------

/**
 * Who can open a share. `project` means the members of the task's project
 * (shared-projects §5, 1.6): only offered for a session with
 * `preferences.projectId`.
 */
export type ShareAccessLevel = 'public' | 'specific' | 'project';

export interface CreateShareRequest {
  accessLevel: ShareAccessLevel;
  allowedEmails?: string[];
}

export interface UpdateShareRequest {
  accessLevel?: ShareAccessLevel;
  allowedEmails?: string[];
}

export interface ShareResponse {
  shareId: string;
  sessionId: string;
  ownerId: string;
  accessLevel: ShareAccessLevel;
  allowedEmails?: string[];
  /** Set on a `project` share. */
  projectId?: string | null;
  createdAt: string;
  shareUrl: string;
}

export interface ShareListResponse {
  shares: ShareResponse[];
}

/**
 * One artifact pinned into a shared conversation's snapshot.
 *
 * The recipient shape — no artifact-level share id, because there is no
 * artifact share: the CONVERSATION share is the grant, and the pair
 * (conversation shareId, artifactId) is the whole handle a recipient
 * has. `version` is the version the artifact stood at when the
 * conversation was shared, matching the frozen transcript around it
 * rather than whatever the owner has edited it into since.
 */
export interface SharedConversationArtifact {
  artifactId: string;
  version: number;
  title: string;
  contentType: string;
  /** Anchors the card under the same turn the owner sees it under.
   *  Null for artifacts written before that linkage existed. */
  producedByMessageIndex: number | null;
}

export interface SharedConversationResponse {
  shareId: string;
  title: string;
  accessLevel: ShareAccessLevel;
  createdAt: string;
  ownerId: string;
  messages: Message[];
  /** Empty for shares created before artifacts were captured, and for
   *  conversations that produced none — indistinguishable, and neither
   *  is an error. */
  artifacts: SharedConversationArtifact[];
}

export interface ExportResponse {
  sessionId: string;
  title: string;
}

// ------------------------------------------------------------------
// Service
// ------------------------------------------------------------------

@Injectable({ providedIn: 'root' })
export class ShareService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);
  private readonly conversationsUrl = computed(() => `${this.config.appApiUrl()}/conversations`);
  private readonly sharesUrl = computed(() => `${this.config.appApiUrl()}/shares`);
  private readonly sharedUrl = computed(() => `${this.config.appApiUrl()}/shared`);

  /**
   * For callers that show the failure inline themselves (the share modal, a
   * project's Tasks tab), so the global toast doesn't repeat it.
   */
  private quiet(suppressErrorToast: boolean | undefined) {
    return suppressErrorToast ? { context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true) } : {};
  }

  async createShare(
    sessionId: string,
    accessLevel: ShareAccessLevel,
    allowedEmails?: string[],
    options?: { suppressErrorToast?: boolean },
  ): Promise<ShareResponse> {
    const body: CreateShareRequest = {
      accessLevel,
      ...(allowedEmails?.length ? { allowedEmails } : {}),
    };

    return firstValueFrom(
      this.http.post<ShareResponse>(
        `${this.conversationsUrl()}/${sessionId}/share`,
        body,
        this.quiet(options?.suppressErrorToast),
      )
    );
  }

  async listSharesForSession(sessionId: string): Promise<ShareListResponse> {
    return firstValueFrom(
      this.http.get<ShareListResponse>(`${this.conversationsUrl()}/${sessionId}/shares`)
    );
  }

  async getSharedConversation(shareId: string): Promise<SharedConversationResponse> {
    return firstValueFrom(
      this.http.get<SharedConversationResponse>(`${this.sharedUrl()}/${shareId}`)
    );
  }

  /**
   * Mint a render URL for one artifact inside a shared conversation.
   *
   * The grant is the conversation share, so this is addressed by
   * (shareId, artifactId) and never by an artifact share id — there
   * isn't one. The backend serves only artifacts the snapshot pinned,
   * at the version it pinned, so a 404 here means "not part of this
   * share" as much as "gone".
   *
   * The returned URL embeds a short-lived (~120s) bearer credential:
   * set it as an iframe `src` and re-mint on each open rather than
   * caching it.
   */
  async mintConversationArtifactToken(
    shareId: string,
    artifactId: string,
  ): Promise<RenderToken> {
    const res = await firstValueFrom(
      this.http.post<{ url: string; expires_at: string }>(
        `${this.sharedUrl()}/${encodeURIComponent(shareId)}/artifacts/` +
          `${encodeURIComponent(artifactId)}/render-token`,
        {},
      ),
    );
    return { url: res.url, expiresAt: res.expires_at };
  }

  async updateShare(shareId: string, accessLevel?: ShareAccessLevel, allowedEmails?: string[]): Promise<ShareResponse> {
    const body: UpdateShareRequest = {};
    if (accessLevel) body.accessLevel = accessLevel;
    if (allowedEmails) body.allowedEmails = allowedEmails;

    return firstValueFrom(
      this.http.patch<ShareResponse>(`${this.sharesUrl()}/${shareId}`, body)
    );
  }

  async revokeShare(shareId: string, options?: { suppressErrorToast?: boolean }): Promise<void> {
    await firstValueFrom(
      this.http.delete(`${this.sharesUrl()}/${shareId}`, this.quiet(options?.suppressErrorToast))
    );
  }

  /**
   * Fork a share into a new session of the caller's. A member forking a task
   * of an active project gets a task in that project, on its current agent.
   */
  async exportSharedConversation(shareId: string, options?: { suppressErrorToast?: boolean }): Promise<ExportResponse> {
    return firstValueFrom(
      this.http.post<ExportResponse>(
        `${this.sharesUrl()}/${shareId}/export`,
        {},
        this.quiet(options?.suppressErrorToast),
      )
    );
  }
}
