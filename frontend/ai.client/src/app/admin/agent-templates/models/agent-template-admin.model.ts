/**
 * Client types for the admin-managed agent templates catalog.
 *
 * These mirror the backend wire shapes in
 * `apis/shared/agent_templates/models.py` on the `feat/agent-templates-backend`
 * branch:
 *
 * - The **admin** response (`AgentTemplateAdminResponse`) is snake_case for the
 *   catalog/audit fields (`template_id`, `sort_order`, `created_at`, ...) and
 *   camelCase only for `modelConfig` (its `serialization_alias`).
 * - Create / update payloads accept the snake_case field names (`template_id`,
 *   `sort_order`) plus `modelConfig` (the alias for `model_cfg`), because the
 *   backend models set `populate_by_name=True`.
 *
 * A template is a subset of the `Agent` shape (§2.2 of the spec) plus catalog
 * management fields (`pitch`, `status`, `sort_order`, audit). The public picker
 * endpoint projects only the `TemplateDraft` subset + `pitch`; this file is the
 * admin side and carries the whole record.
 */

export type TemplateStatus = 'enabled' | 'disabled';

/** Model selection on a template. `modelId === null` ⇒ platform default. */
export interface TemplateModelConfig {
  modelId: string | null;
  params: Record<string, unknown>;
}

/** One binding on a template — same shape as an `AgentBinding`. */
export interface TemplateBinding {
  kind: string;
  ref: string;
  config: Record<string, unknown>;
}

/** Full admin record for one template. */
export interface AgentTemplateAdmin {
  template_id: string;
  name: string;
  description: string;
  emoji: string;
  instructions: string;
  tags: string[];
  starters: string[];
  modelConfig: TemplateModelConfig;
  bindings: TemplateBinding[];
  pitch: string;
  status: TemplateStatus;
  sort_order: number;
  created_at: string;
  updated_at: string;
  created_by?: string | null;
  updated_by?: string | null;
}

export interface AgentTemplateAdminListResponse {
  templates: AgentTemplateAdmin[];
  total: number;
}

/**
 * Create payload. `template_id` is optional — the backend slugifies it from the
 * name when absent, and returns 409 on a slug collision.
 */
export interface AgentTemplateCreate {
  template_id?: string;
  name: string;
  description: string;
  emoji: string;
  instructions: string;
  tags: string[];
  starters: string[];
  modelConfig: TemplateModelConfig;
  bindings: TemplateBinding[];
  pitch: string;
  status: TemplateStatus;
  sort_order: number;
}

/** Partial update — every field optional (backend PATCH). */
export type AgentTemplateUpdate = Partial<AgentTemplateCreate>;
