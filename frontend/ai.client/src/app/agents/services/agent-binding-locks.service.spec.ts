import { TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ModelService } from '../../session/services/model/model.service';
import { SkillService } from '../../services/skill/skill.service';
import { ToolService } from '../../services/tool/tool.service';
import { Agent } from '../models/agent.model';
import { AgentBindingLocksService } from './agent-binding-locks.service';
import { AgentService } from './agent.service';

function agent(partial: Partial<Agent>): Agent {
  return { agentId: 'agent-1', bindings: [], ...partial } as Agent;
}

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

describe('AgentBindingLocksService', () => {
  let service: AgentBindingLocksService;
  let models: { lockToAgentModel: ReturnType<typeof vi.fn>; clearAgentModelLock: ReturnType<typeof vi.fn> };
  let tools: { lockToAgentTools: ReturnType<typeof vi.fn>; clearAgentLock: ReturnType<typeof vi.fn> };
  let skills: {
    lockToAgentSkillIds: ReturnType<typeof vi.fn>;
    lockToAgentSkills: ReturnType<typeof vi.fn>;
    settleAgentLock: ReturnType<typeof vi.fn>;
    clearAgentLock: ReturnType<typeof vi.fn>;
    loadSkills: ReturnType<typeof vi.fn>;
    agentLocked: ReturnType<typeof vi.fn>;
    initialized: ReturnType<typeof vi.fn>;
  };
  let agents: { getBoundSkills: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    models = { lockToAgentModel: vi.fn(), clearAgentModelLock: vi.fn() };
    tools = { lockToAgentTools: vi.fn(), clearAgentLock: vi.fn() };
    skills = {
      lockToAgentSkillIds: vi.fn(),
      lockToAgentSkills: vi.fn(),
      settleAgentLock: vi.fn(),
      clearAgentLock: vi.fn(),
      loadSkills: vi.fn().mockResolvedValue(undefined),
      agentLocked: vi.fn().mockReturnValue(true),
      initialized: vi.fn().mockReturnValue(false),
    };
    agents = { getBoundSkills: vi.fn().mockResolvedValue([{ skillId: 's1' }]) };
    TestBed.configureTestingModule({
      providers: [
        { provide: ModelService, useValue: models },
        { provide: ToolService, useValue: tools },
        { provide: SkillService, useValue: skills },
        { provide: AgentService, useValue: agents },
      ],
    });
    service = TestBed.inject(AgentBindingLocksService);
  });

  it('locks the model, tools and skills to what the Agent binds', async () => {
    service.apply(
      agent({
        modelConfig: { modelId: 'model-x' } as Agent['modelConfig'],
        bindings: [
          { kind: 'tool', ref: 't1' },
          { kind: 'skill', ref: 's1' },
        ] as Agent['bindings'],
      }),
      () => true,
    );
    await flush();
    expect(models.lockToAgentModel).toHaveBeenCalledWith('model-x');
    expect(tools.lockToAgentTools).toHaveBeenCalledWith(['t1']);
    expect(skills.lockToAgentSkillIds).toHaveBeenCalledWith(['s1'], { resolving: true });
    expect(agents.getBoundSkills).toHaveBeenCalledWith('agent-1');
    expect(skills.lockToAgentSkills).toHaveBeenCalledWith([{ skillId: 's1' }]);
  });

  it('an Agent that binds nothing frees the model and keeps only the always-on tools', () => {
    service.apply(agent({}), () => true);
    expect(models.clearAgentModelLock).toHaveBeenCalled();
    expect(tools.lockToAgentTools).toHaveBeenCalledWith([], { keepAlwaysOn: true });
    expect(skills.lockToAgentSkillIds).toHaveBeenCalledWith([], { resolving: false });
    expect(agents.getBoundSkills).not.toHaveBeenCalled();
  });

  it('drops a skill resolve that lands after the Agent stopped being current', async () => {
    let current = true;
    service.apply(agent({ bindings: [{ kind: 'skill', ref: 's1' }] as Agent['bindings'] }), () => current);
    current = false;
    await flush();
    expect(skills.lockToAgentSkills).not.toHaveBeenCalled();
  });

  it('a failed resolve keeps the id lock and loads the viewer’s own skills', async () => {
    agents.getBoundSkills.mockRejectedValue(new Error('404'));
    service.apply(agent({ bindings: [{ kind: 'skill', ref: 's1' }] as Agent['bindings'] }), () => true);
    await flush();
    expect(skills.settleAgentLock).toHaveBeenCalled();
    expect(skills.loadSkills).toHaveBeenCalled();
  });

  it('clear releases every lock', () => {
    service.clear();
    expect(models.clearAgentModelLock).toHaveBeenCalled();
    expect(tools.clearAgentLock).toHaveBeenCalled();
    expect(skills.clearAgentLock).toHaveBeenCalled();
  });
});
