/**
 * `AWS::BedrockAgentCore::Runtime` env-var ceiling guard.
 *
 * The Runtime resource accepts at most **50** EnvironmentVariables. That limit
 * is enforced by CloudFormation when it validates the changeset — *not* by
 * `cdk synth`, and not by anything else that runs before a deploy. So a PR that
 * adds the 51st variable passes `tsc`, passes `jest`, passes CI, merges, and
 * then fails the Platform Stack deploy with:
 *
 *     🛑 #/EnvironmentVariables: maximum size: [50], found: [51]
 *
 * That is exactly what happened on 2026-08-05: #833 PR-5 added
 * `QUOTA_RUNWAY_ENABLED` to the inference construct and broke the dev platform
 * deploy. Nothing caught it earlier because nothing looked.
 *
 * This test looks. It is deliberately a *ceiling* assertion rather than an
 * exact-count snapshot: removing a variable should never fail a build, and
 * pinning the exact number would turn every legitimate removal into a test
 * edit. The failure message is the important part — it has to tell the next
 * person what to do, because "add an env var" is an obvious-looking change with
 * a non-obvious limit.
 *
 * Freeing a slot means retiring a dead variable or folding several booleans
 * into one delimited value. A code-level feature flag that reads `os.environ`
 * and defaults ON does not need an entry here at all.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

/** Hard AWS limit on AWS::BedrockAgentCore::Runtime EnvironmentVariables. */
const RUNTIME_ENV_VAR_LIMIT = 50;

describe('AgentCore Runtime environment variable ceiling', () => {
  let template: Template;

  beforeAll(() => {
    const cert = 'arn:aws:acm:us-east-1:123456789012:certificate/test';
    // WORST CASE, on purpose. Some variables are spread in conditionally, so a
    // minimal config under-counts and the guard would report headroom that no
    // real environment has. `tokenExchange` alone adds three
    // (TOKEN_EXCHANGE_URL / _CLIENT_ID / _SECRET_ID) — without it this test
    // counts 47 while prod and dev deploy 50. Any future conditional block
    // must be enabled here too, or this guard quietly stops guarding.
    const config = createMockConfig({
      domainName: 'example.com',
      infrastructureHostedZoneDomain: 'example.com',
      certificateArn: cert,
      frontend: { cloudFrontPriceClass: 'PriceClass_100', certificateArn: cert },
      artifacts: {
        shareInboxEnabled: false, retentionDays: 90, extraFrameAncestors: [], certificateArn: cert },
      mcpSandbox: { extraFrameAncestors: [], certificateArn: cert },
      fineTuning: {
        enabled: true,
        defaultQuotaHours: 0,
      },
      tokenExchange: {
        url: 'https://tokens.example.com/v2/oauth/token',
        clientId: 'test-client',
      },
    });
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    template = Template.fromStack(stack);
  });

  it('stays at or under the 50-variable limit CloudFormation enforces', () => {
    const runtimes = template.findResources('AWS::BedrockAgentCore::Runtime');
    const entries = Object.entries(runtimes);
    expect(entries.length).toBeGreaterThan(0);

    for (const [logicalId, resource] of entries) {
      const env = (resource.Properties?.EnvironmentVariables ?? {}) as Record<string, unknown>;
      const count = Object.keys(env).length;

      // Thrown rather than asserted so the guidance travels with the failure —
      // Jest's expect() carries no message argument.
      if (count > RUNTIME_ENV_VAR_LIMIT) {
        throw new Error(
          `${logicalId} declares ${count} environment variables; ` +
            `AWS::BedrockAgentCore::Runtime allows ${RUNTIME_ENV_VAR_LIMIT}. ` +
            `CloudFormation rejects the changeset at deploy time, so synth and CI both look ` +
            `green while the Platform Stack deploy fails. Free a slot before adding another: ` +
            `retire a dead variable, or fold booleans into one delimited FEATURE_FLAGS value. ` +
            `A flag that reads os.environ and defaults ON needs no entry here at all.`,
        );
      }
      expect(count).toBeLessThanOrEqual(RUNTIME_ENV_VAR_LIMIT);
    }
  });

  it('reports the remaining headroom, so the next PR knows what it has', () => {
    const runtimes = template.findResources('AWS::BedrockAgentCore::Runtime');
    for (const [logicalId, resource] of Object.entries(runtimes)) {
      const env = (resource.Properties?.EnvironmentVariables ?? {}) as Record<string, unknown>;
      const count = Object.keys(env).length;
      // Not an assertion — a visible number in the test output. The ceiling
      // above is the gate; this is the early warning.
      console.log(
        `[env-var headroom] ${logicalId}: ${count}/${RUNTIME_ENV_VAR_LIMIT} used, ` +
          `${RUNTIME_ENV_VAR_LIMIT - count} free`,
      );
      expect(count).toBeGreaterThan(0);
    }
  });
});
