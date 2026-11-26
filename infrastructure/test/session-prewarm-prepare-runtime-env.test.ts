/**
 * Prewarm prepare (docs/specs/turn-path-ttft.md) is app-api only: the request
 * field is the trigger and app-api is the gate, so the AgentCore Runtime never
 * receives either prewarm variable and spends none of its environment budget.
 */
import { runtimeEnvironment, synthWorstCasePlatformStack } from './helpers/runtime-worst-case';

describe('session prewarm prepare on the AgentCore Runtime', () => {
  it('writes neither prewarm variable, even with both switches on', () => {
    const { template } = synthWorstCasePlatformStack({
      sessionPrewarm: { enabled: true, prepareEnabled: true, prepareHoldoutPercent: 20 },
    });
    const env = runtimeEnvironment(template);
    expect(env).not.toHaveProperty('SESSION_PREWARM_PREPARE_ENABLED');
    expect(env).not.toHaveProperty('SESSION_PREWARM_ENABLED');
    expect(env).not.toHaveProperty('SESSION_PREWARM_PREPARE_HOLDOUT_PERCENT');
  });
});
