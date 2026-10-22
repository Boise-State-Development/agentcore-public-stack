import { Injectable, computed, effect, inject, signal, untracked } from '@angular/core';
import { toSignal } from '@angular/core/rxjs-interop';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { firstValueFrom, of } from 'rxjs';
import { Project } from '../../../projects/models/project.model';
import { ProjectApiService } from '../../../projects/services/project-api.service';
import { ProjectsService, projectErrorMessage } from '../../../projects/services/projects.service';
import { FEATURES } from '../../../services/features';

/**
 * The query params that bind a conversation. Both keys are always present and the
 * unused one is null, so a `merge` navigation drops a stale key rather than leaving
 * `?project=` and `?assistantId=` side by side. `routerLink` and a plain `navigate`
 * drop null params, so the URL only ever shows one of them.
 */
export interface BindingQueryParams {
  project: string | null;
  assistantId: string | null;
}

/**
 * Query params for a binding known by its ids, such as a session's stored
 * preferences: `?project=` for a project task (whose Agent is the project's
 * hidden harness), else `?assistantId=`. Needs no service, so a list of links
 * can build its params without the router.
 */
export function bindingQueryParams(
  assistantId: string | null | undefined,
  projectId: string | null | undefined,
  projectsOn: boolean,
): BindingQueryParams {
  if (projectsOn && projectId) return { project: projectId, assistantId: null };
  return { project: null, assistantId: assistantId ?? null };
}

/** A `?project=` the project list did not hold, fetched on its own. */
type FetchedProject = Project | { error: string };

/**
 * What the current conversation is bound to, as its URL names it.
 *
 * A Shared Project conversation runs on the project's hidden harness Agent. The chat
 * request carries the harness as `assistant_id` and the backend derives the project
 * from it (`turn_project_id` in the inference route), so that does not change. The
 * URL names the **project** instead (`?project=<projectId>`): the harness id stays
 * internal, and "is this a project?" is a read rather than a reverse lookup over the
 * project list. Any other Agent conversation keeps `?assistantId=<agentId>`.
 *
 * A link that puts a harness in `assistantId` (bookmarks and links from before
 * `?project=`) still resolves: the project is found by its `harnessAgentId`, and the
 * session page rewrites the URL to `?project=`.
 *
 * Query params belong to the whole router state, so the root route's are the page's.
 */
@Injectable({ providedIn: 'root' })
export class ConversationBindingService {
  private readonly features = inject(FEATURES);
  private readonly projectsService = inject(ProjectsService);
  private readonly projectApi = inject(ProjectApiService);

  /**
   * Optional: outside a routed app (a service under test, an embedded preview's
   * own injector) there is no URL, and so no binding to read from it.
   */
  private readonly params = toSignal(
    inject(ActivatedRoute, { optional: true })?.queryParamMap ?? of(convertToParamMap({})),
    { initialValue: convertToParamMap({}) },
  );

  /** The URL's `?project=`, while projects are on in this build. */
  readonly queryProjectId = computed(() =>
    this.features.projects ? this.params().get('project') : null,
  );

  /** The URL's `?assistantId=`: an ordinary Agent, or a harness from an older link. */
  readonly queryAssistantId = computed(() => this.params().get('assistantId'));

  private readonly fetched = signal<ReadonlyMap<string, FetchedProject>>(new Map());
  private readonly inflight = new Map<string, Promise<void>>();

  /**
   * The project the URL binds, or null. Read from the project list first, so the
   * name is the project's current one; a `?project=` the list does not hold (it has
   * not loaded, or the link arrived before it did) is fetched on its own.
   */
  readonly boundProject = computed<Project | null>(() => {
    if (!this.features.projects) return null;
    const projects = this.projectsService.projects$();
    const projectId = this.queryProjectId();
    if (projectId) {
      const listed = projects.find(p => p.projectId === projectId);
      if (listed) return listed;
      const fetched = this.fetched().get(projectId);
      return fetched && !('error' in fetched) ? fetched : null;
    }
    const agentId = this.queryAssistantId();
    return agentId ? (projects.find(p => p.harnessAgentId === agentId) ?? null) : null;
  });

  /** The Agent a turn goes to: the URL's Agent, else the bound project's harness. */
  readonly boundAgentId = computed<string | null>(
    () => this.queryAssistantId() ?? this.boundProject()?.harnessAgentId ?? null,
  );

  /** Whether the URL names a binding at all, resolved or not. */
  readonly hasUrlBinding = computed(() => !!this.queryAssistantId() || !!this.queryProjectId());

  /**
   * A `?project=` still being resolved. A message sent now must wait for it, or it
   * would go out unbound and start a plain chat.
   */
  readonly projectPending = computed(() => {
    const projectId = this.queryProjectId();
    if (!projectId || this.queryAssistantId()) return false;
    return !this.boundProject() && !this.projectError();
  });

  /** Why a `?project=` could not be resolved, for the empty state's error line. */
  readonly projectError = computed<string | null>(() => {
    const projectId = this.queryProjectId();
    if (!projectId || this.boundProject()) return null;
    const fetched = this.fetched().get(projectId);
    return fetched && 'error' in fetched ? fetched.error : null;
  });

  constructor() {
    // Fetch a `?project=` the list does not hold. One GET, not the whole list: a
    // direct link should not wait on every project the caller can see.
    effect(() => {
      const projectId = this.queryProjectId();
      if (!projectId) return;
      if (this.projectsService.projects$().some(p => p.projectId === projectId)) return;
      if (this.fetched().has(projectId)) return;
      untracked(() => void this.fetch(projectId));
    });
  }

  /**
   * The Agent to send a turn to, waiting first for a `?project=` still resolving.
   * Null when the conversation is unbound, or its project could not be resolved.
   */
  async resolveBoundAgentId(): Promise<string | null> {
    const projectId = this.queryProjectId();
    if (projectId && this.projectPending()) await this.fetch(projectId);
    return this.boundAgentId();
  }

  /**
   * Query params binding a conversation to an Agent: `?project=` when the Agent is a
   * project's harness (named directly, or known from the bound project or the list),
   * else `?assistantId=`. Both null for an unbound conversation.
   */
  queryParamsFor(agentId: string | null | undefined, projectId?: string | null): BindingQueryParams {
    const project = projectId ?? (agentId ? this.projectIdForAgent(agentId) : null);
    return bindingQueryParams(agentId, project, this.features.projects);
  }

  /**
   * Tell the binding about a project a caller already holds, so a conversation
   * started on its harness from outside the URL (the project page's own
   * composer) still lands on `?project=`, and resolves without a fetch.
   */
  knowProject(project: Project): void {
    this.remember(project.projectId, project);
  }

  private projectIdForAgent(agentId: string): string | null {
    const bound = this.boundProject();
    if (bound?.harnessAgentId === agentId) return bound.projectId;
    const listed = this.projectsService.projects$().find(p => p.harnessAgentId === agentId);
    if (listed) return listed.projectId;
    for (const known of this.fetched().values()) {
      if (!('error' in known) && known.harnessAgentId === agentId) return known.projectId;
    }
    return null;
  }

  private fetch(projectId: string): Promise<void> {
    const running = this.inflight.get(projectId);
    if (running) return running;
    const request = firstValueFrom(this.projectApi.get(projectId))
      .then(
        project => this.remember(projectId, project),
        err => this.remember(projectId, { error: projectErrorMessage(err, 'This project could not be loaded.') }),
      )
      .finally(() => this.inflight.delete(projectId));
    this.inflight.set(projectId, request);
    return request;
  }

  private remember(projectId: string, result: FetchedProject): void {
    this.fetched.update(map => new Map(map).set(projectId, result));
  }
}
