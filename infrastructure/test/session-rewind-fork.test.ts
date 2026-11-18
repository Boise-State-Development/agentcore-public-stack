/**
 * Conversation rewind and fork (docs/specs/conversation-rewind-and-fork.md) are
 * default-on feature switches. Pinned: the AgentCore Runtime gets
 * SESSION_REWIND_ENABLED / SESSION_FORK_ENABLED only while OFF (as "false"), so
 * an environment on the default spends none of the V2 environment budget, and
 * app-api always gets its copy.
 */
import { runtimeEnvironment, synthWorstCasePlatformStack } from './helpers/runtime-worst-case';

describe('rewind and fork on the AgentCore Runtime', () => {
  it('writes nothing while both are on (the default)', () => {
    const { template } = synthWorstCasePlatformStack({
      sessionRewind: { enabled: true },
      sessionFork: { enabled: true },
    });
    const env = runtimeEnvironment(template);
    expect(env).not.toHaveProperty('SESSION_REWIND_ENABLED');
    expect(env).not.toHaveProperty('SESSION_FORK_ENABLED');
  });

  it('writes "false" while each is off', () => {
    const { template } = synthWorstCasePlatformStack({
      sessionRewind: { enabled: false },
      sessionFork: { enabled: false },
    });
    const env = runtimeEnvironment(template);
    expect(env.SESSION_REWIND_ENABLED).toBe('false');
    expect(env.SESSION_FORK_ENABLED).toBe('false');
  });
});
