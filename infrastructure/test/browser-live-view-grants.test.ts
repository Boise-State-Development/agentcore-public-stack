/**
 * app-api's AgentCore Browser grant for the Live View route.
 *
 * A Live View URL is SigV4 query-signed and lives at most 300 seconds, so it
 * cannot be minted once by the agent and reused — app-api signs a fresh one
 * per request (`docs/specs/authenticated-web-assessment.md` D2), which is why
 * the task role needs browser permissions at all.
 *
 * The thing these tests exist to hold down is the *narrowness* of that grant.
 * The obvious lazy fix — copy the Runtime's `BrowserAccess` statement onto the
 * task role — would let app-api start, stop and **drive** browser sessions.
 * app-api is the surface reachable with a user's session cookie; the agent
 * owns the browser lifecycle, and app-api must only ever be able to look.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_REGION } from './helpers/mock-config';

interface PolicyStatement {
  Effect?: string;
  Action?: string | string[];
  Resource?: string | string[];
  Sid?: string;
}

function asArray(v: string | string[] | undefined): string[] {
  if (v === undefined) return [];
  return Array.isArray(v) ? v : [v];
}

describe('app-api browser Live View grant', () => {
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
        const stmts =
          (r.Properties as { PolicyDocument?: { Statement?: PolicyStatement[] } })
            ?.PolicyDocument?.Statement ?? [];
        for (const s of stmts) {
          if (!s.Sid) continue;
          statementsBySid.set(s.Sid, [...(statementsBySid.get(s.Sid) ?? []), s]);
        }
      }
    }
  });

  it('grants app-api exactly what minting a live view needs', () => {
    const matches = statementsBySid.get('BrowserLiveViewAccess') ?? [];
    expect(matches).toHaveLength(1);

    const [statement] = matches;
    expect(statement.Effect).toBe('Allow');
    expect(asArray(statement.Action).sort()).toEqual([
      'bedrock-agentcore:GetBrowserSession',
      'bedrock-agentcore:UpdateBrowserStream',
    ]);
  });

  it('grants ConnectBrowserLiveViewStream on *, because it has no resource type', () => {
    // AWS's service reference lists NO resource types for this action, so a
    // resource-scoped statement never matches it and it is an implicit deny.
    // That is not a theoretical concern: it shipped that way, and because
    // `generate_live_view_url` signs locally without calling AWS, the URL
    // minted fine and the failure surfaced only as the browser's WebSocket
    // being closed — DCV auth code 10, which looks like a service fault.
    const matches = statementsBySid.get('BrowserLiveViewConnect') ?? [];
    expect(matches).toHaveLength(1);

    const [statement] = matches;
    expect(statement.Effect).toBe('Allow');
    expect(asArray(statement.Resource)).toEqual(['*']);

    // `*` is unavoidable here, so the statement must carry NOTHING else.
    expect(asArray(statement.Action)).toEqual([
      'bedrock-agentcore:ConnectBrowserLiveViewStream',
    ]);
  });

  it('does NOT let app-api start, stop or drive a browser', () => {
    // Both statements together: splitting the grant must not have opened a
    // side door in the one that had to widen to `*`.
    const actions = [
      ...asArray(statementsBySid.get('BrowserLiveViewAccess')?.[0]?.Action),
      ...asArray(statementsBySid.get('BrowserLiveViewConnect')?.[0]?.Action),
    ];

    // Each of these is on the Runtime's own BrowserAccess statement and must
    // stay off app-api's: the agent owns the session lifecycle, and
    // ConnectBrowserAutomationStream is the one that would let a route
    // reachable with a session cookie actually drive the browser.
    for (const forbidden of [
      'bedrock-agentcore:StartBrowserSession',
      'bedrock-agentcore:StopBrowserSession',
      'bedrock-agentcore:ConnectBrowserAutomationStream',
    ]) {
      expect(actions).not.toContain(forbidden);
    }
    expect(actions).not.toContain('bedrock-agentcore:*');
  });

  it('keeps the resource-typed actions scoped to the browser, not to *', () => {
    const [statement] = statementsBySid.get('BrowserLiveViewAccess') ?? [];

    const resources = asArray(statement?.Resource);
    expect(resources).toHaveLength(1);
    expect(resources[0]).not.toBe('*');
  });

  it('leaves the Runtime role its own, wider grant', () => {
    // The narrowing above must not have been achieved by editing the shared
    // statement the agent depends on to browse at all.
    const [runtime] = statementsBySid.get('BrowserAccess') ?? [];

    expect(asArray(runtime?.Action)).toEqual(
      expect.arrayContaining([
        'bedrock-agentcore:StartBrowserSession',
        'bedrock-agentcore:ConnectBrowserAutomationStream',
      ]),
    );
  });
});
