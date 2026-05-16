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
}

/** Which artifact the side panel is currently showing. */
export interface OpenArtifactRef {
  artifactId: string;
  version: number;
  title: string;
}
