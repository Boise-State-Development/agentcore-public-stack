/**
 * Agent version pinning (docs/specs/workflow-designer.md §23): a saved workflow's Agent
 * step runs the version its file records. Pinned: the Runtime gets
 * AGENT_VERSION_PINNING_ENABLED only while it and every workflow flag it needs are on,
 * since the Runtime has no copy of those flags, so the default spends none of the V2
 * environment budget.
 */
import { runtimeEnvironment, synthWorstCasePlatformStack } from './helpers/runtime-worst-case';

describe('agent version pinning on the AgentCore Runtime', () => {
  it('writes the flag while the whole chain is on', () => {
    const env = runtimeEnvironment(synthWorstCasePlatformStack().template);
    expect(env.AGENT_VERSION_PINNING_ENABLED).toBe('true');
  });

  it('writes nothing while it is off', () => {
    const { template } = synthWorstCasePlatformStack({ agentVersionPinning: { enabled: false } });
    expect(runtimeEnvironment(template)).not.toHaveProperty('AGENT_VERSION_PINNING_ENABLED');
  });

  it('writes nothing while saved workflows are off', () => {
    const { template } = synthWorstCasePlatformStack({
      workflows: { enabled: false, promptStepsEnabled: false, checklistStepsEnabled: false },
    });
    expect(runtimeEnvironment(template)).not.toHaveProperty('AGENT_VERSION_PINNING_ENABLED');
  });

  it('writes nothing while handoffs are off', () => {
    const { template } = synthWorstCasePlatformStack({
      handoffs: { enabled: false, briefEnabled: false, suggestionsEnabled: false },
    });
    expect(runtimeEnvironment(template)).not.toHaveProperty('AGENT_VERSION_PINNING_ENABLED');
  });
});
