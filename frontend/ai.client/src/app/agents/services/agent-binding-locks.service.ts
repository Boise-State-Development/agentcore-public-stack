import { Injectable, inject } from '@angular/core';
import { ModelService } from '../../session/services/model/model.service';
import { SkillService } from '../../services/skill/skill.service';
import { ToolService } from '../../services/tool/tool.service';
import { Agent } from '../models/agent.model';
import { AgentService } from './agent.service';

/**
 * Lock the composer's pickers to an Agent's bindings, and release them.
 *
 * The backend runs every Agent (a project's harness included) on its own model,
 * tools and skills, never the viewer's picks, so a composer that sends to one
 * must show what will actually run: the model picker locked to the Agent's
 * model, the tool list to its tools, and the `/` menu to its skills. The locks
 * live in root singletons that outlive the page that set them, so every page
 * that applies them releases them when it stops sending to that Agent.
 *
 * One implementation for the conversation page and the project page's
 * composer; a second copy would drift the first time either was tuned.
 */
@Injectable({ providedIn: 'root' })
export class AgentBindingLocksService {
  private readonly agents = inject(AgentService);
  private readonly models = inject(ModelService);
  private readonly tools = inject(ToolService);
  private readonly skills = inject(SkillService);

  /**
   * Lock each picker to the Agent's binding. Tools and skills are always locked,
   * to none if the Agent binds none. An Agent that binds no tools still gets the
   * admin's always-on tools, so its lock keeps them; skills have no such floor.
   *
   * `isCurrent` says whether the Agent is still the one being sent to, so a
   * skill resolve that lands after a switch is dropped rather than applied.
   */
  apply(agent: Agent, isCurrent: () => boolean): void {
    this.lockModel(agent.modelConfig?.modelId ?? null);

    const toolIds = agent.bindings.filter(b => b.kind === 'tool').map(b => b.ref);
    if (toolIds.length > 0) {
      this.tools.lockToAgentTools(toolIds);
    } else {
      this.tools.lockToAgentTools([], { keepAlwaysOn: true });
    }

    const skillIds = agent.bindings.filter(b => b.kind === 'skill').map(b => b.ref);
    this.skills.lockToAgentSkillIds(skillIds, { resolving: skillIds.length > 0 });
    // Resolve the bound skills for this viewer. Only fires when an Agent actually
    // binds skills (a skills-enabled context), so there is no fetch in flag-off
    // environments.
    if (skillIds.length > 0) {
      void this.resolveSkills(agent.agentId, isCurrent);
    }
  }

  /**
   * Lock only the model picker: to `modelId`, or free when the Agent pins none.
   * For a page that learns of a model change without re-reading the Agent.
   */
  lockModel(modelId: string | null): void {
    if (modelId) {
      this.models.lockToAgentModel(modelId);
    } else {
      this.models.clearAgentModelLock();
    }
  }

  /** Release every Agent picker lock. Idempotent: a no-op when nothing is locked. */
  clear(): void {
    this.models.clearAgentModelLock();
    this.tools.clearAgentLock();
    this.skills.clearAgentLock();
  }

  /**
   * Replace the id lock with the Agent's skills as the backend resolved them for
   * this viewer (`GET /agents/{id}/skills`), which is what the `/` menu and the
   * picker rows are built from. The viewer's own `/skills/` list cannot do it: a
   * skill another project member authored is not in it, so for every member but
   * the author the locked set used to intersect to nothing and the menu never
   * opened.
   *
   * Best-effort. A failure (an older backend without the route) keeps the id lock
   * and loads the viewer's own list so the rows it can name still render. A
   * result for an Agent that is no longer current is dropped.
   */
  private async resolveSkills(agentId: string, isCurrent: () => boolean): Promise<void> {
    try {
      const skills = await this.agents.getBoundSkills(agentId);
      if (!isCurrent() || !this.skills.agentLocked()) return;
      this.skills.lockToAgentSkills(skills);
    } catch {
      if (!isCurrent() || !this.skills.agentLocked()) return;
      this.skills.settleAgentLock();
      if (!this.skills.initialized()) {
        void this.skills.loadSkills();
      }
    }
  }
}
