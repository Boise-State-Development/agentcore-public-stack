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
}

interface ArtifactListResponseDto {
  artifacts: ArtifactSummaryDto[];
}

export interface RenderToken {
  url: string;
  expiresAt: string;
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
    }));
  }
}
