import { Injectable, inject, computed } from '@angular/core';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';
import { ConfigService } from '../../services/config.service';
import { TemplateCatalogEntry } from '../agent-form/agent-templates';

/**
 * Wire shape of the public `GET /templates/` endpoint — mirrors the backend
 * `PublicTemplateListResponse` (`backend/src/apis/shared/agent_templates/models.py`).
 * Each entry is the client `TemplateCatalogEntry` (`{ draft, pitch }`), so the picker /
 * reconcile / form-population path is unchanged; only the *source* of the list moves
 * from the hardcoded `AGENT_TEMPLATES` array to this endpoint.
 */
export interface TemplateCatalogResponse {
  templates: TemplateCatalogEntry[];
  total: number;
}

/**
 * Loads the admin-managed agent-template catalog for the "Start from a template" picker.
 *
 * The catalog is now DATA served by the backend, not code baked into the client — this
 * is the forkability goal: no org-specific templates compiled into the SPA. Any signed-in
 * user who can create an agent may read the enabled templates.
 */
@Injectable({ providedIn: 'root' })
export class AgentTemplatesService {
  private readonly http = inject(HttpClient);
  private readonly config = inject(ConfigService);

  private readonly baseUrl = computed(() => `${this.config.appApiUrl()}/templates`);

  /**
   * Fetch the enabled templates as picker catalog entries, in display order.
   *
   * The backend returns `{ templates: [], total: 0 }` (never an error) when the catalog
   * is empty, so an empty array is a normal, expected result — a fork that has authored
   * no templates degrades to a clean "no templates yet" state. Rejects only on a
   * transport / HTTP error, which the caller renders as an error state with retry.
   *
   * The picker consumes `entry.draft` (the `TemplateDraft` written to localStorage and
   * fed to the form) and `entry.pitch` (the card's one-line blurb).
   */
  async loadTemplates(): Promise<TemplateCatalogEntry[]> {
    const res = await firstValueFrom(
      this.http.get<TemplateCatalogResponse>(`${this.baseUrl()}/`),
    );
    return res?.templates ?? [];
  }
}
