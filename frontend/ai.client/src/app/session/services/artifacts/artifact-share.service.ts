import { Injectable, inject } from '@angular/core';
import {
  HttpClient,
  HttpContext,
  HttpErrorResponse,
} from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import { SUPPRESS_ERROR_TOAST } from '../../../auth/error.interceptor';
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

/**
 * One artifact somebody else shared with you, as the library's
 * "Shared with you" tab sees it.
 *
 * Deliberately *not* `LibraryArtifact` with extra fields. A received
 * artifact has no `artifactId` and no `sessionId` you can reach — the
 * share id is the only handle you have on it — and no `updatedAt` that
 * means anything to you, because that is the owner's clock. Modelling
 * the two as one type would invite a template to reach for a field that
 * is structurally absent on half its rows.
 */
export interface SharedWithMeArtifact {
  shareId: string;
  title: string;
  contentType: string;
  version: number;
  /** Who shared it. */
  ownerEmail: string;
  /** When it was shared with you — not when it was made or last edited. */
  sharedAt: string;
  /** SPA-relative recipient route, e.g. `/shared-artifact/{id}`. */
  shareUrl: string;
}

interface SharedWithMeResponseDto {
  artifacts: SharedWithMeArtifact[];
  nextCursor: string | null;
}

/**
 * A page of the share inbox, plus where to continue from.
 *
 * `nextCursor` terminates the listing, not the page length: the backend
 * drops rows after its underlying query (a share revoked since it was
 * fanned out, an allowlist edited), so a short page can still have more
 * behind it. Page until the cursor is null.
 */
export interface SharedWithMePage {
  artifacts: SharedWithMeArtifact[];
  nextCursor: string | null;
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

  /**
   * Opt a request out of the global error toast.
   *
   * Applied to every call whose caller renders its own inline error —
   * the share modal's message box and the recipient page's 403/404/500
   * screens. Without it a dead share link produces two notices at once:
   * the page's "Artifact not found" *and* a generic toast repeating the
   * backend detail.
   *
   * Deliberately NOT applied to `listShares`: it degrades silently by
   * design (the dialog still opens and can create a link), so the toast
   * is the only signal that the existing-links list is incomplete.
   */
  private inlineErrors(): { context: HttpContext } {
    return { context: new HttpContext().set(SUPPRESS_ERROR_TOAST, true) };
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
        this.inlineErrors(),
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
        this.inlineErrors(),
      ),
    );
  }

  /** Revoke a share. Effective within one render-token TTL (~120s):
   *  already-issued tokens finish out their life, no new ones are minted. */
  async revokeShare(shareId: string): Promise<void> {
    await firstValueFrom(
      this.http.delete(
        `${this.artifactsUrl()}/shares/${encodeURIComponent(shareId)}`,
        this.inlineErrors(),
      ),
    );
  }

  // ----------------------------------------------------------------
  // Recipient
  // ----------------------------------------------------------------

  /**
   * One page of the artifacts other people have shared with the caller.
   *
   * Returns `null` — not an empty page — when the endpoint 404s, which
   * is how the backend says the inbox does not exist in this
   * environment (`ARTIFACT_SHARE_INBOX_ENABLED` off). The two are
   * different facts and the library page renders them differently: null
   * hides the tabs entirely, an empty page shows "Nothing yet". Any
   * other failure rethrows, because "we could not load your inbox" is
   * not the same as "you do not have one".
   *
   * Scoped by the session cookie; there is no parameter that could ask
   * about anybody else.
   */
  async listSharedWithMe(
    cursor?: string,
    limit = 100,
  ): Promise<SharedWithMePage | null> {
    try {
      const res = await firstValueFrom(
        this.http.get<SharedWithMeResponseDto>(this.sharedUrl(), {
          ...this.inlineErrors(),
          params: cursor ? { cursor, limit } : { limit },
        }),
      );
      return {
        artifacts: res.artifacts ?? [],
        nextCursor: res.nextCursor ?? null,
      };
    } catch (err: unknown) {
      if (err instanceof HttpErrorResponse && err.status === 404) {
        return null;
      }
      throw err;
    }
  }

  /** Metadata for a shared artifact. Never returns content. */
  async getSharedArtifact(shareId: string): Promise<SharedArtifact> {
    return firstValueFrom(
      this.http.get<SharedArtifact>(
        `${this.sharedUrl()}/${encodeURIComponent(shareId)}`,
        this.inlineErrors(),
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
        this.inlineErrors(),
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
        this.inlineErrors(),
      ),
    );
    return {
      content: res.content,
      contentType: res.content_type,
      version: res.version,
    };
  }
}
