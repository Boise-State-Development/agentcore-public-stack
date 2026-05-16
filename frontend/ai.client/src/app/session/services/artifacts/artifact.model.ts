/**
 * Normalized client model for an agent-authored artifact.
 *
 * Both delivery paths converge here: the live `artifact` SSE event
 * (camelCase wire shape) and the app-api session-list endpoint
 * (snake_case REST shape, normalized by ArtifactHttpService). The panel
 * never holds artifact HTML — it mints a short-lived render token and
 * loads the content in a sandboxed iframe on the artifact origin.
 */
export interface Artifact {
  artifactId: string;
  version: number;
  title: string;
  contentType: string;
  updatedAt: string;
  createdAt?: string;
  /**
   * 0-based index of the assistant message that produced (or last
   * updated) this artifact, in AgentCore Memory ordering — matches the
   * messages endpoint's `msg-{sessionId}-{index}` id on **reload**, when
   * the SPA enumerates the same memory messages the backend indexed.
   *
   * Not reliable for live placement: a tool-using turn is one folded
   * assistant message client-side, but this index counts the interleaved
   * tool_use/tool_result memory messages, so it drifts. Live placement
   * uses `producedByMessageId` instead. Null/undefined for artifacts
   * written before linkage existed — those fall back to the strip.
   */
  producedByMessageIndex?: number | null;

  /**
   * Concrete client message id of the assistant turn that produced this
   * artifact, resolved at live-event time (same way oauth/tool-approval
   * prompts anchor). Set only on the live path; absent on reload
   * hydration (the numeric index governs there). Stable as later turns
   * append, so the card stays put without a refresh.
   */
  producedByMessageId?: string | null;
}

/** Which artifact the side panel is currently showing. */
export interface OpenArtifactRef {
  artifactId: string;
  version: number;
  title: string;
}
