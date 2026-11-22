/**
 * Prompt steps (docs/specs/workflow-designer.md §21) are the first workflow flag
 * on the AgentCore Runtime. Pinned: the Runtime gets PROMPT_STEPS_ENABLED only
 * while it and every flag it needs are on, so the default spends none of the V2
 * environment budget, and the Runtime never gets a copy of the workflow flags
 * themselves (its own copy carries the chain).
 */
import { runtimeEnvironment, synthWorstCasePlatformStack } from './helpers/runtime-worst-case';

describe('prompt steps on the AgentCore Runtime', () => {
  it('writes the flag while the whole chain is on', () => {
    const env = runtimeEnvironment(synthWorstCasePlatformStack().template);
    expect(env.PROMPT_STEPS_ENABLED).toBe('true');
    expect(env).not.toHaveProperty('WORKFLOWS_ENABLED');
    expect(env).not.toHaveProperty('WORKFLOW_RUNS_ENABLED');
  });

  it('writes nothing while it is off', () => {
    const { template } = synthWorstCasePlatformStack({
      workflows: { enabled: true, promptStepsEnabled: false, checklistStepsEnabled: false },
    });
    expect(runtimeEnvironment(template)).not.toHaveProperty('PROMPT_STEPS_ENABLED');
  });

  it('writes nothing while saved workflows are off', () => {
    const { template } = synthWorstCasePlatformStack({
      workflows: { enabled: false, promptStepsEnabled: true, checklistStepsEnabled: false },
    });
    expect(runtimeEnvironment(template)).not.toHaveProperty('PROMPT_STEPS_ENABLED');
  });
});
