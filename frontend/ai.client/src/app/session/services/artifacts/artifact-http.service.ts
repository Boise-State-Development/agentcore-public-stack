import { Injectable, inject } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../../services/config.service';
import type { Artifact } from './artifact.model';

interface RenderTokenResponseDto {
  url: string;
  expires_at: string;
}

interface ArtifactSummaryDto {
  artifact_id: string;
  version: number;
  title: string;
  content_type: string;
  updated_at: string;
  created_at?: string | null;
  produced_by_message_index?: number | null;
}

interface ArtifactListResponseDto {
  artifacts: ArtifactSummaryDto[];
}

interface LibraryArtifactDto {
  artifact_id: string;
  version: number;
  title: string;
  content_type: string;
  created_at: string;
  updated_at: string;
  session_id: string;
}

interface ArtifactLibraryResponseDto {
  artifacts: LibraryArtifactDto[];
}

interface ArtifactContentResponseDto {
  content: string;
  content_type: string;
  version: number;
}

export interface RenderToken {
  url: string;
  expiresAt: string;
}

/** Raw source of one artifact version, for the panel's code view. */
export interface ArtifactContent {
  content: string;
  contentType: string;
  version: number;
}

/**
 * One artifact at its current HEAD, for the library page.
 *
 * Not `Artifact`: that model is per-version and carries the message index
 * the session view anchors a card to, which is meaningless across sessions.
 * This one carries `sessionId` instead — the library's job is to lead you
 * back to the conversation an artifact came from.
 */
export interface LibraryArtifact {
  artifactId: string;
  version: number;
  title: string;
  contentType: string;
  createdAt: string;
  updatedAt: string;
  sessionId: string;
}

/**
 * app-api client for the artifacts feature. Auth rides the httpOnly BFF
 * session cookie + csrfInterceptor automatically (HttpClient pipeline),
 * same as every other app-api call — no Bearer here.
 *
 * The render token is a short-lived (~120s) bearer credential embedded
 * in the returned URL; the panel sets it as the iframe `src` and
 * re-mints on each open rather than caching it.
 */
@Injectable({ providedIn: 'root' })
export class ArtifactHttpService {
  private http = inject(HttpClient);
  private config = inject(ConfigService);

  /** Mint a single-version render URL for the sandboxed iframe. */
  async mintRenderToken(
    artifactId: string,
    version: number,
    sessionId: string,
  ): Promise<RenderToken> {
    const res = await firstValueFrom(
      this.http.post<RenderTokenResponseDto>(
        `${this.config.appApiUrl()}/artifacts/${encodeURIComponent(artifactId)}/render-token`,
        { version, sessionId },
      ),
    );
    return { url: res.url, expiresAt: res.expires_at };
  }

  /**
   * Fetch one artifact version's raw source for the code view. The
   * bytes are inert text the panel highlights client-side. The backend
   * unwraps Markdown back to the authored source and 413s anything too
   * large to highlight inline (callers steer to download on that).
   */
  async getArtifactContent(
    artifactId: string,
    version: number,
  ): Promise<ArtifactContent> {
    const res = await firstValueFrom(
      this.http.get<ArtifactContentResponseDto>(
        `${this.config.appApiUrl()}/artifacts/${encodeURIComponent(artifactId)}/content`,
        { params: { version } },
      ),
    );
    return {
      content: res.content,
      contentType: res.content_type,
      version: res.version,
    };
  }

  /** List the current HEAD of every artifact in a chat session. */
  async listSessionArtifacts(sessionId: string): Promise<Artifact[]> {
    const res = await firstValueFrom(
      this.http.get<ArtifactListResponseDto>(`${this.config.appApiUrl()}/artifacts`, {
        params: { session_id: sessionId },
      }),
    );
    return (res.artifacts ?? []).map((a) => ({
      artifactId: a.artifact_id,
      version: a.version,
      title: a.title,
      contentType: a.content_type,
      updatedAt: a.updated_at,
      createdAt: a.created_at ?? undefined,
      producedByMessageIndex: a.produced_by_message_index ?? null,
    }));
  }

  /**
   * Retitle an artifact. Returns the server's view of the updated HEAD.
   *
   * The backend applies the new title to the HEAD row *and* to every
   * version row, so the library and the conversation that produced the
   * artifact cannot end up showing different names for it. Callers
   * should reconcile against the returned title rather than the one
   * they sent — it comes back trimmed.
   */
  async renameArtifact(artifactId: string, title: string): Promise<LibraryArtifact> {
    const res = await firstValueFrom(
      this.http.patch<LibraryArtifactDto>(
        `${this.config.appApiUrl()}/artifacts/${encodeURIComponent(artifactId)}`,
        { title },
      ),
    );
    return {
      artifactId: res.artifact_id,
      version: res.version,
      title: res.title,
      contentType: res.content_type,
      createdAt: res.created_at,
      updatedAt: res.updated_at,
      sessionId: res.session_id,
    };
  }

  /**
   * Delete an artifact, every version of it, and every share of it.
   *
   * Permanent — there is no trash and no undo, which is why every call
   * site is behind a destructive confirmation dialog. The content bytes
   * sit on a server-side retention clock afterwards, but nothing in the
   * app can bring the artifact back.
   */
  async deleteArtifact(artifactId: string): Promise<void> {
    await firstValueFrom(
      this.http.delete<void>(
        `${this.config.appApiUrl()}/artifacts/${encodeURIComponent(artifactId)}`,
      ),
    );
  }

  /**
   * Every artifact the signed-in user owns, at HEAD, newest-first.
   *
   * Takes no argument on purpose: the endpoint is scoped by the session
   * cookie's identity, and there is no parameter that could widen it.
   * Server-ordered, so callers should not re-sort — a client-side sort
   * would silently diverge the day the backend gains pagination.
   */
  async listLibrary(): Promise<LibraryArtifact[]> {
    const res = await firstValueFrom(
      this.http.get<ArtifactLibraryResponseDto>(
        `${this.config.appApiUrl()}/artifacts/library`,
      ),
    );
    return (res.artifacts ?? []).map((a) => ({
      artifactId: a.artifact_id,
      version: a.version,
      title: a.title,
      contentType: a.content_type,
      createdAt: a.created_at,
      updatedAt: a.updated_at,
      sessionId: a.session_id,
    }));
  }
}
