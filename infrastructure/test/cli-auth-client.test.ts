/**
 * CLI auth client wiring.
 *
 * The terminal client authenticates with a Cognito access token from its own
 * public PKCE app client. For that token to be accepted, the client id has to
 * reach two places besides the client itself:
 *
 *   1. the AgentCore Runtime's JWT authorizer allow-list, or /invocations 401s
 *      before the handler runs;
 *   2. app-api's environment, so the backend can validate Bearer tokens minted
 *      by that client.
 *
 * Both are easy to omit silently: `cliAppClient` is optional on
 * PlatformComputeRefs, so leaving it out of the refs literal type-checks
 * cleanly and the authorizer just quietly keeps one entry. That exact bug
 * shipped during development, which is why these assertions exist.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import {
  createMockConfig,
  mockSsmContext,
  MOCK_ACCOUNT,
  MOCK_REGION,
} from './helpers/mock-config';

function wiredTemplate(): Template {
  const cert = 'arn:aws:acm:us-east-1:123456789012:certificate/test';
  const config = createMockConfig({
    domainName: 'example.com',
    infrastructureHostedZoneDomain: 'example.com',
    certificateArn: cert,
    frontend: { cloudFrontPriceClass: 'PriceClass_100', certificateArn: cert },
    artifacts: { retentionDays: 90, extraFrameAncestors: [], certificateArn: cert },
    mcpSandbox: { extraFrameAncestors: [], certificateArn: cert },
    fineTuning: {},
  });
  const app = new cdk.App();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'TestPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  stack.wireCompute();
  return Template.fromStack(stack);
}

describe('CLI auth client wiring', () => {
  let template: Template;

  beforeAll(() => {
    template = wiredTemplate();
  });

  function clientLogicalIds(): { bff: string; cli: string } {
    const clients = template.findResources('AWS::Cognito::UserPoolClient');
    let bff = '';
    let cli = '';
    for (const [logicalId, resource] of Object.entries(clients) as [string, any][]) {
      const name: string = resource.Properties.ClientName ?? '';
      if (name.endsWith('-bff-app-client')) bff = logicalId;
      if (name.endsWith('-cli-app-client')) cli = logicalId;
    }
    return { bff, cli };
  }

  it('creates exactly two app clients: the confidential BFF one and the public CLI one', () => {
    template.resourceCountIs('AWS::Cognito::UserPoolClient', 2);
    const { bff, cli } = clientLogicalIds();
    expect(bff).toBeTruthy();
    expect(cli).toBeTruthy();
  });

  it('the runtime authorizer allows BOTH client ids', () => {
    const runtimes = Object.values(
      template.findResources('AWS::BedrockAgentCore::Runtime'),
    ) as any[];
    expect(runtimes).toHaveLength(1);

    const allowed =
      runtimes[0].Properties.AuthorizerConfiguration.CustomJWTAuthorizer
        .AllowedClients;
    expect(allowed).toHaveLength(2);

    const { bff, cli } = clientLogicalIds();
    const refs = allowed.map((entry: any) => entry.Ref);
    expect(refs).toContain(bff);
    expect(refs).toContain(cli);
  });

  it('the runtime authorizer uses AllowedClients, never AllowedAudience', () => {
    // Cognito access tokens carry `client_id` and no `aud`, so an audience
    // check could never match and would 401 every call.
    const runtime = Object.values(
      template.findResources('AWS::BedrockAgentCore::Runtime'),
    )[0] as any;
    const authorizer =
      runtime.Properties.AuthorizerConfiguration.CustomJWTAuthorizer;
    expect(authorizer.AllowedAudience).toBeUndefined();
  });

  it('app-api receives the CLI client id as an environment variable', () => {
    const taskDefs = Object.values(
      template.findResources('AWS::ECS::TaskDefinition'),
    ) as any[];
    const env = taskDefs
      .flatMap((td) => td.Properties.ContainerDefinitions ?? [])
      .flatMap((c: any) => c.Environment ?? []);
    const cliVar = env.find((e: any) => e.Name === 'COGNITO_CLI_APP_CLIENT_ID');
    expect(cliVar).toBeDefined();

    const { cli } = clientLogicalIds();
    expect(cliVar.Value).toEqual({ Ref: cli });
  });
});
