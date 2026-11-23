/**
 * Checklist steps (docs/specs/workflow-designer.md §22) run on prompt steps' Runtime
 * invocation. Pinned: the Runtime gets CHECKLIST_STEPS_ENABLED only while it, prompt
 * steps and every flag they need are on, so the default spends none of the V2
 * environment budget.
 */
import { runtimeEnvironment, synthWorstCasePlatformStack } from './helpers/runtime-worst-case';

describe('checklist steps on the AgentCore Runtime', () => {
  it('writes the flag while the whole chain is on', () => {
    const env = runtimeEnvironment(synthWorstCasePlatformStack().template);
    expect(env.CHECKLIST_STEPS_ENABLED).toBe('true');
    expect(env.PROMPT_STEPS_ENABLED).toBe('true');
  });

  it('writes nothing while it is off', () => {
    const { template } = synthWorstCasePlatformStack({
      workflows: { enabled: true, promptStepsEnabled: true, checklistStepsEnabled: false },
    });
    expect(runtimeEnvironment(template)).not.toHaveProperty('CHECKLIST_STEPS_ENABLED');
  });

  it('writes nothing while prompt steps are off', () => {
    const { template } = synthWorstCasePlatformStack({
      workflows: { enabled: true, promptStepsEnabled: false, checklistStepsEnabled: true },
    });
    const env = runtimeEnvironment(template);
    expect(env).not.toHaveProperty('CHECKLIST_STEPS_ENABLED');
    expect(env).not.toHaveProperty('PROMPT_STEPS_ENABLED');
  });
});
