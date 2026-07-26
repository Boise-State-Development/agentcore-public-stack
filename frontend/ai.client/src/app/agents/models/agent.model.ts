import { ShareEntry, UserPermission } from '../../assistants/models/assistant.model';
import { AgentListingBlock, ListingPublisher } from './store.model';

// `AgentListingBlock` lives with the other marketplace types in `store.model.ts` and is
// re-exported here for the many callers that reach for it alongside `Agent`.
export type { AgentListingBlock };

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

/**
 * One thing an Agent can reach, as the detail page names it (Marketplace Phase 3).
 * Names, never refs — the backend resolves binding refs to display names so this
 * payload can be rendered to anyone who may see the Agent.
 */
export interface AgentCapability {
  label: string;
  kind: string;
}

export interface Agent {
  agentId: string;
  ownerName: string;
  name: string;
  description: string;
  /**
   * The system prompt. **Absent unless you are the owner or an editor** (Marketplace
   * Phase 3): under a store, a PUBLIC agent is browsable by the whole institution, so
   * viewers get `capabilities` instead of behaviour.
   */
  instructions?: string;
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

  // Marketplace listing (Phase 1) + the detail read (Phase 3). All are absent on the
  // list route and on an agent that was never submitted.
  tagline?: string;
  /** S3 object key for the uploaded square icon (D5); the bytes are never on the record. */
  iconKey?: string;
  /**
   * Where to render the icon from (Phase 4) — a relative API path carrying the key's
   * digest as `?v=`. Absent → `app-agent-icon` draws the generated gradient.
   */
  iconUrl?: string;
  listing?: AgentListingBlock;
  /** Resolved on `GET /agents/{id}` only. */
  capabilities?: AgentCapability[];
  modelLabel?: string;
  /** Attribution as the page renders it; `listing.publisherId` is an id and never shown. */
  publisher?: ListingPublisher | null;
  categoryLabel?: string;

  // Share metadata (present for shared agents)
  firstInteracted?: boolean;
  isSharedWithMe?: boolean;
  userPermission?: UserPermission;
}

/**
 * D6's answer to "will this run for me?" — two states, not three.
 *
 * A middle `'limits'` (runs, but degraded) was specced and removed in #747: it required a
 * binding to declare `config.optional`, which nothing ever wrote, and it contradicted the
 * Designer spec's D5 — "No downgrade on missing capability (block-only v1)" — which is the
 * rule the runtime resolver actually implements. Any gap blocks.
 */
export type RunnabilityState = 'ready' | 'blocked';

export interface MissingCapability {
  label: string;
  kind: string;
  optional: boolean;
}

export interface AgentRunnability {
  agentId: string;
  state: RunnabilityState;
  missing: MissingCapability[];
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
