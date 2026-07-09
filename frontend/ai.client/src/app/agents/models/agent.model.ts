import { ShareEntry, UserPermission } from '../../assistants/models/assistant.model';

/**
 * Agent Designer contract (Phase 4). Mirrors the backend `/agents` surface
 * (`AgentResponse` / `AgentBinding` / `AgentModelConfig` in
 * `apis/shared/assistants/models.py`). An Agent evolves the Assistant: same
 * underlying record (`agentId == assistantId`, legacy ids stay valid) but the
 * Agent shape carries the governed `modelConfig` + uniform `bindings[]`.
 */

export type BindingKind = 'knowledge_base' | 'tool' | 'skill' | 'memory_space';

/** Value stored in a `memory_space` binding's `config`. */
export interface MemorySpaceBindingConfig {
  /** Read-only injection vs. read + `memory_*` write tools (D5 gate). */
  access: 'read' | 'readwrite';
  /** Entry slugs (or `MEMORY.md`) always hydrated into the prompt. */
  alwaysLoad?: string[];
}

/** Governed single-select model (D3). `modelId` is the Bedrock/provider id. */
export interface AgentModelConfig {
  modelId: string;
  provider?: string;
  params?: Record<string, unknown>;
}

/**
 * Capability + bounds for one inference param, mirrored from the backend
 * `ModelParamSpec`. Carried on a model `BindableItem` under
 * `meta.supportedParams.params[<canonicalKey>]`; drives the Designer's
 * per-model params controls (numeric bounds / enum options / locked).
 */
export interface ModelParamSpec {
  supported: boolean;
  min?: number | null;
  max?: number | null;
  allowed?: (string | number)[] | null;
  default?: string | number | null;
  locked?: boolean;
}

/** `meta.supportedParams` shape on a model `BindableItem`. */
export interface SupportedParams {
  params: Record<string, ModelParamSpec>;
}

/** A single primitive binding on an Agent (D3). `config` is kind-specific. */
export interface AgentBinding {
  kind: BindingKind;
  ref: string;
  config?: Record<string, unknown>;
}

export interface Agent {
  agentId: string;
  ownerName: string;
  name: string;
  description: string;
  instructions: string;
  modelConfig?: AgentModelConfig;
  bindings: AgentBinding[];
  visibility: 'PRIVATE' | 'PUBLIC' | 'SHARED';
  tags: string[];
  starters: string[];
  emoji?: string;
  imageUrl?: string;
  usageCount: number;
  status: 'DRAFT' | 'COMPLETE';
  createdAt: string;
  updatedAt: string;

  // Share metadata (present for shared agents)
  firstInteracted?: boolean;
  isSharedWithMe?: boolean;
  userPermission?: UserPermission;
}

export interface CreateAgentDraftRequest {
  name?: string;
}

export interface CreateAgentRequest {
  name: string;
  description: string;
  instructions: string;
  visibility?: 'PRIVATE' | 'PUBLIC' | 'SHARED';
  tags?: string[];
  starters?: string[];
  emoji?: string;
  imageUrl?: string;
  modelConfig?: AgentModelConfig;
  bindings?: AgentBinding[];
}

export interface UpdateAgentRequest {
  name?: string;
  description?: string;
  instructions?: string;
  visibility?: 'PRIVATE' | 'PUBLIC' | 'SHARED';
  tags?: string[];
  starters?: string[];
  emoji?: string;
  status?: 'DRAFT' | 'COMPLETE';
  imageUrl?: string;
  modelConfig?: AgentModelConfig;
  bindings?: AgentBinding[];
}

export interface AgentsListResponse {
  agents: Agent[];
  nextToken?: string;
}

export interface AgentSharesResponse {
  agentId: string;
  sharedWith: ShareEntry[];
}

/**
 * Bindable-primitives catalog (Phase 2). `GET /agents/bindable?kind=…` returns
 * the RBAC-filtered palette for the caller — a uniform item every picker
 * consumes. `ref` is what the UI stores: `modelConfig.modelId` for
 * `kind === 'model'`, otherwise a `binding.ref`.
 */
export type BindableKind = 'model' | BindingKind;

export interface BindableItem {
  kind: string;
  ref: string;
  label: string;
  description: string;
  meta: Record<string, unknown>;
}

export interface BindableListResponse {
  kind: string;
  items: BindableItem[];
}
