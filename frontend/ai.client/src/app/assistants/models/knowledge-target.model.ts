/**
 * Whose knowledge a source call adds to: an agent (its id) or a project.
 *
 * A project's files are its harness agent's documents, but a project member is
 * authorized by the project, so its calls go through the project's
 * `/projects/{id}/knowledge` routes instead of the agent's own (shared-projects §5).
 */
export type KnowledgeTarget = string | { projectId: string };

/** The app-api paths (below the API base URL) a target's knowledge routes live under. */
export interface KnowledgePaths {
  /** `…/import`, `…/{documentId}/chunks` */
  documents: string;
  /** `…/crawl`, `…/crawls` */
  webSources: string;
  /** `…`, `…/{policyId}`, `…/{policyId}/run-now` */
  syncPolicies: string;
}

export function knowledgePaths(target: KnowledgeTarget): KnowledgePaths {
  if (typeof target === 'string') {
    const base = `/assistants/${encodeURIComponent(target)}`;
    return { documents: `${base}/documents`, webSources: `${base}/web-sources`, syncPolicies: `${base}/sync-policies` };
  }
  const base = `/projects/${encodeURIComponent(target.projectId)}/knowledge`;
  return { documents: base, webSources: base, syncPolicies: `${base}/sync-policies` };
}
