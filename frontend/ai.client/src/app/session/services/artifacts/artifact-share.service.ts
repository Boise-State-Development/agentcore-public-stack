import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import type {
  ArtifactContent,
  RenderToken,
} from './artifact-http.service';

/** Who may open a share. `public` means any *authenticated* tenant user
 *  — never anonymous, matching conversation sharing. */
export type ArtifactShareAccessLevel = 'public' | 'specific';

/** One artifact share, as its owner sees it. */
export interface ArtifactShare {
  shareId: string;
  artifactId: string;
  /** The immutable version this share is pinned to — never HEAD. */
  version: number;
  ownerId: string;
  accessLevel: ArtifactShareAccessLevel;
  allowedEmails?: string[];
  title: string;
  contentType: string;
  createdAt: string;
  updatedAt?: string;
  /** SPA-relative recipient route, e.g. `/shared-artifact/{id}`. */
  shareUrl: string;
}

interface ArtifactShareListResponse {
  shares: ArtifactShare[];
}

/** Recipient-facing share metadata. Never carries artifact content. */
export interface SharedArtifact {
  shareId: string;
  title: string;
  contentType: string;
  version: number;
  createdAt: string;
  ownerEmail: string;
  canDownload: boolean;
}

interface RenderTokenResponseDto {
  url: string;
  expires_at: string;
}

interface ArtifactContentResponseDto {
  content: string;
  content_type: string;
  version: number;
}

/**
 * app-api client for artifact sharing. Auth rides the httpOnly BFF
 * session cookie + csrfInterceptor automatically, same as every other
 * app-api call — no Bearer here.
 *
 * Wire shape is camelCase (unlike `ArtifactHttpService`, whose endpoints
 * are snake_case): the sharing API mirrors the conversation-sharing
 * models this feature is adapted from. The render-token response is the
 * one exception — it reuses the owner endpoint's shape verbatim,
 * `expires_at` included, so the panel's iframe and the download service
 * work against either path unchanged.
 */
@Injectable({ providedIn: 'root' })
export class ArtifactShareService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  private artifactsUrl(): string {
    return `${this.config.appApiUrl()}/artifacts`;
  }

  private sharedUrl(): string {
    return `${this.config.appApiUrl()}/shared-artifacts`;
  }

  // ----------------------------------------------------------------
  // Owner CRUD
  // ----------------------------------------------------------------

  /**
   * Share one immutable artifact version. `allowedEmails` is required by
   * the backend for `specific` and ignored for `public`; the owner is
   * added to the allowlist server-side, so callers need not include it.
   */
  async createShare(
    artifactId: string,
    version: number,
    accessLevel: ArtifactShareAccessLevel,
    allowedEmails?: string[],
  ): Promise<ArtifactShare> {
    return firstValueFrom(
      this.http.post<ArtifactShare>(
        `${this.artifactsUrl()}/${encodeURIComponent(artifactId)}/shares`,
        {
          version,
          accessLevel,
          ...(allowedEmails?.length ? { allowedEmails } : {}),
        },
      ),
    );
  }

  /** Every share the caller owns for this artifact, across all versions. */
  async listShares(artifactId: string): Promise<ArtifactShare[]> {
    const res = await firstValueFrom(
      this.http.get<ArtifactShareListResponse>(
        `${this.artifactsUrl()}/${encodeURIComponent(artifactId)}/shares`,
      ),
    );
    return res.shares ?? [];
  }

  /** Change who may view an existing share. The pinned version is
   *  immutable — only access can change. */
  async updateShare(
    shareId: string,
    accessLevel?: ArtifactShareAccessLevel,
    allowedEmails?: string[],
  ): Promise<ArtifactShare> {
    const body: Record<string, unknown> = {};
    if (accessLevel) body['accessLevel'] = accessLevel;
    if (allowedEmails) body['allowedEmails'] = allowedEmails;

    return firstValueFrom(
      this.http.patch<ArtifactShare>(
        `${this.artifactsUrl()}/shares/${encodeURIComponent(shareId)}`,
        body,
      ),
    );
  }

  /** Revoke a share. Effective within one render-token TTL (~120s):
   *  already-issued tokens finish out their life, no new ones are minted. */
  async revokeShare(shareId: string): Promise<void> {
    await firstValueFrom(
      this.http.delete(
        `${this.artifactsUrl()}/shares/${encodeURIComponent(shareId)}`,
      ),
    );
  }

  // ----------------------------------------------------------------
  // Recipient
  // ----------------------------------------------------------------

  /** Metadata for a shared artifact. Never returns content. */
  async getSharedArtifact(shareId: string): Promise<SharedArtifact> {
    return firstValueFrom(
      this.http.get<SharedArtifact>(
        `${this.sharedUrl()}/${encodeURIComponent(shareId)}`,
      ),
    );
  }

  /**
   * Mint a render URL for a shared artifact version.
   *
   * The returned URL embeds a short-lived (~120s) bearer credential — set
   * it as an iframe `src` and re-mint on each open rather than caching
   * it. The backend re-checks the share's ACL on every call, which is
   * what bounds revocation at the token TTL instead of at session length.
   */
  async mintSharedRenderToken(shareId: string): Promise<RenderToken> {
    const res = await firstValueFrom(
      this.http.post<RenderTokenResponseDto>(
        `${this.sharedUrl()}/${encodeURIComponent(shareId)}/render-token`,
        {},
      ),
    );
    return { url: res.url, expiresAt: res.expires_at };
  }

  /**
   * Raw source of a shared artifact version, for the recipient's code
   * view. The parallel of `ArtifactHttpService.getArtifactContent`,
   * which builds its key from the authenticated user and so can only
   * ever serve an owner.
   *
   * The bytes are inert text the page highlights client-side — never
   * executed. Oversized artifacts 413 here exactly as they do for the
   * owner, so callers steer to download.
   */
  async getSharedArtifactContent(shareId: string): Promise<ArtifactContent> {
    const res = await firstValueFrom(
      this.http.get<ArtifactContentResponseDto>(
        `${this.sharedUrl()}/${encodeURIComponent(shareId)}/content`,
      ),
    );
    return {
      content: res.content,
      contentType: res.content_type,
      version: res.version,
    };
  }
}
