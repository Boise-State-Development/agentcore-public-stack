/**
 * MCP Lambda Function URL invoke grants.
 *
 * Two roles need to reach an IAM-protected MCP server's Lambda Function URL,
 * and both were scoped wrongly at some point:
 *
 *   1. app-api signs the admin "Discover from server" request
 *      (POST /admin/tools/discover) with its OWN task role — the gateway role
 *      named in the form's credential picker only signs at runtime. With no
 *      InvokeFunctionUrl on the task role, discovery 403s for every
 *      AuthType=AWS_IAM server and the admin has to type each tool name.
 *
 *   2. inference-api invokes a direct external MCP server at runtime. Its
 *      grant was scoped to `<prefix>-mcp-*` only, which never matches an MCP
 *      server deployed from its own repo as `mcp-<server>-<env>`.
 *
 * Both must cover BOTH naming conventions.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION, MOCK_PREFIX } from './helpers/mock-config';

interface PolicyStatement {
  Effect?: string;
  Action?: string | string[];
  Resource?: string | string[];
  Sid?: string;
}

const UNPREFIXED_MCP_ARN = `arn:aws:lambda:${MOCK_REGION}:${MOCK_ACCOUNT}:function:mcp-*`;
const PREFIXED_MCP_ARN = `arn:aws:lambda:${MOCK_REGION}:${MOCK_ACCOUNT}:function:${MOCK_PREFIX}-mcp-*`;

function asArray(v: string | string[] | undefined): string[] {
  if (v === undefined) return [];
  return Array.isArray(v) ? v : [v];
}

describe('MCP Lambda Function URL invoke grants', () => {
  let statementsBySid: Map<string, PolicyStatement[]>;

  beforeAll(() => {
    const config = createMockConfig();
    const app = new cdk.App();
    mockSsmContext(app, config);
    const stack = new PlatformStack(app, 'TestPlatformStack', {
      config,
      env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
    });
    stack.wireCompute();
    const template = Template.fromStack(stack);

    // CDK splits an oversized inline policy into managed overflow policies
    // attached to the same role, so both resource types must be scanned.
    statementsBySid = new Map();
    for (const type of ['AWS::IAM::Policy', 'AWS::IAM::ManagedPolicy']) {
      for (const [, r] of Object.entries(template.findResources(type))) {
        const stmts = ((r.Properties as { PolicyDocument?: { Statement?: PolicyStatement[] } })?.PolicyDocument?.Statement) ?? [];
        for (const s of stmts) {
          if (!s.Sid) continue;
          statementsBySid.set(s.Sid, [...(statementsBySid.get(s.Sid) ?? []), s]);
        }
      }
    }
  });

  it('app-api can invoke an MCP function URL for admin discovery', () => {
    const matches = statementsBySid.get('McpDiscoveryLambdaInvoke') ?? [];
    expect(matches).toHaveLength(1);

    const [statement] = matches;
    expect(statement.Effect).toBe('Allow');
    expect(asArray(statement.Action)).toContain('lambda:InvokeFunctionUrl');
    expect(asArray(statement.Resource)).toEqual(
      expect.arrayContaining([UNPREFIXED_MCP_ARN, PREFIXED_MCP_ARN]),
    );
  });

  it('inference-api external-MCP invoke covers both MCP function naming conventions', () => {
    const matches = statementsBySid.get('ExternalMCPLambdaAccess') ?? [];
    expect(matches).toHaveLength(1);

    const [statement] = matches;
    expect(asArray(statement.Action)).toContain('lambda:InvokeFunctionUrl');
    expect(asArray(statement.Resource)).toEqual(
      expect.arrayContaining([UNPREFIXED_MCP_ARN, PREFIXED_MCP_ARN]),
    );
  });
});
