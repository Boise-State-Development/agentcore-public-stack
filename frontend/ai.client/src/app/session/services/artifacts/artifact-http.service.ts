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
}
