/**
 * Every tool's code runs in one Code Interpreter with no network path out.
 *
 * `SANDBOX` network mode is not that, since AWS documents it as allowing
 * Amazon S3 access. So the interpreter is in `VPC` mode, in its own VPC, and
 * this file pins what makes that VPC a dead end:
 *   - exactly one interpreter, in VPC mode, on the logs-only role;
 *   - its subnets are isolated: no internet gateway, NAT gateway, VPC endpoint
 *     or route other than the VPC's own local route;
 *   - its security group has no ingress and no real egress rule;
 *   - DNS support is off (security groups do not filter the Amazon DNS
 *     resolver, and with it on, public names resolved from the sandbox);
 *   - the skill-script SSM parameter names the same interpreter;
 *   - the Runtime's grants name that interpreter alone, and its environment
 *     variable names are unchanged.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { PlatformStack } from '../lib/platform-stack';
import {
  CODE_SANDBOX_VPC_CIDR,
  SKILL_SCRIPT_INTERPRETER_SSM_SUFFIX,
} from '../lib/constructs/agentcore/code-interpreter-construct';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';
import { runtimeEnvironment, synthWorstCasePlatformStack } from './helpers/runtime-worst-case';

interface Statement {
  Sid?: string;
  Effect?: string;
  Action?: string | string[];
  Resource?: unknown;
}

const EXECUTION_ROLE_LOGICAL_ID = 'AgentCoreCodeInterpreterCodeInterpreterExecutionRole5A00171B';
const INTERPRETER_LOGICAL_ID_PREFIX = 'AgentCoreCodeInterpreterCodeSandboxInterpreter';
const SANDBOX_PREFIX = 'AgentCoreCodeInterpreterSandbox';
const PARAMETER_LOGICAL_ID_PREFIX = 'AgentCoreCodeInterpreterSkillScriptInterpreterIdParameter';

const SESSION_ACTIONS = [
  'bedrock-agentcore:StartCodeInterpreterSession',
  'bedrock-agentcore:InvokeCodeInterpreter',
  'bedrock-agentcore:StopCodeInterpreterSession',
  'bedrock-agentcore:GetCodeInterpreter',
  'bedrock-agentcore:GetCodeInterpreterSession',
  'bedrock-agentcore:ListCodeInterpreterSessions',
];

function synthWired(prefix?: string): Template {
  const config = createMockConfig(prefix ? { projectPrefix: prefix } : {});
  const app = new cdk.App();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'TestPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  stack.wireCompute();
  return Template.fromStack(stack);
}

function oneResource(template: Template, type: string, idPrefix: string): [string, any] {
  const found = Object.entries(template.findResources(type)).filter(([id]) => id.startsWith(idPrefix));
  expect(found).toHaveLength(1);
  return found[0];
}

/** Resources of `type` whose logical id puts them in the sandbox's VPC. */
function sandboxResources(template: Template, type: string): [string, any][] {
  return Object.entries(template.findResources(type)).filter(([id]) => id.startsWith(SANDBOX_PREFIX));
}

function runtimeStatements(template: Template): Statement[] {
  // The role's own policy and CDK's overflow managed policies both count.
  const policies = [
    ...Object.values(template.findResources('AWS::IAM::Policy')),
    ...Object.values(template.findResources('AWS::IAM::ManagedPolicy')),
  ].filter((p: any) =>
    JSON.stringify(p.Properties.Roles ?? []).includes('AgentCoreRuntimeExecutionRole'),
  );
  expect(policies.length).toBeGreaterThan(0);
  return policies.flatMap((p: any) => p.Properties.PolicyDocument.Statement as Statement[]);
}

function actions(st: Statement): string[] {
  return Array.isArray(st.Action) ? st.Action : [st.Action ?? ''];
}

describe('Code Interpreter with no network path out', () => {
  let template: Template;
  let vpcId: string;

  beforeAll(() => {
    template = synthWired();
    [vpcId] = oneResource(template, 'AWS::EC2::VPC', SANDBOX_PREFIX);
  });

  it('is the only interpreter, in VPC mode, on the logs-only role', () => {
    expect(Object.keys(template.findResources('AWS::BedrockAgentCore::CodeInterpreterCustom'))).toHaveLength(1);
    const [, ci] = oneResource(template, 'AWS::BedrockAgentCore::CodeInterpreterCustom', INTERPRETER_LOGICAL_ID_PREFIX);
    expect(ci.Properties.NetworkConfiguration.NetworkMode).toBe('VPC');
    expect(ci.Properties.Name).toBe('test_project_code_sandbox');
    expect(ci.Properties.ExecutionRoleArn).toEqual({ 'Fn::GetAtt': [EXECUTION_ROLE_LOGICAL_ID, 'Arn'] });
  });

  it('sits in the isolated VPC’s subnets, behind its security group only', () => {
    const [, ci] = oneResource(template, 'AWS::BedrockAgentCore::CodeInterpreterCustom', INTERPRETER_LOGICAL_ID_PREFIX);
    const { Subnets, SecurityGroups } = ci.Properties.NetworkConfiguration.VpcConfig;
    const subnetIds = sandboxResources(template, 'AWS::EC2::Subnet').map(([id]) => id);
    expect(subnetIds).toHaveLength(2);
    expect(Subnets).toEqual(subnetIds.map((id) => ({ Ref: id })));
    const [sgId] = oneResource(template, 'AWS::EC2::SecurityGroup', `${SANDBOX_PREFIX}SecurityGroup`);
    expect(SecurityGroups).toEqual([{ 'Fn::GetAtt': [sgId, 'GroupId'] }]);
  });

  it('has its own VPC with DNS support off', () => {
    const vpc = template.findResources('AWS::EC2::VPC')[vpcId];
    expect(vpc.Properties.CidrBlock).toBe(CODE_SANDBOX_VPC_CIDR);
    expect(vpc.Properties.EnableDnsSupport).toBe(false);
    expect(vpc.Properties.EnableDnsHostnames).toBe(false);
  });

  it('has no gateway, endpoint, peering or route out of the VPC', () => {
    const inVpc = (r: any) => JSON.stringify(r.Properties ?? {}).includes(vpcId);
    for (const type of [
      'AWS::EC2::InternetGateway',
      'AWS::EC2::EgressOnlyInternetGateway',
      'AWS::EC2::NatGateway',
      'AWS::EC2::VPCEndpoint',
      'AWS::EC2::VPCPeeringConnection',
      'AWS::EC2::TransitGatewayAttachment',
      'AWS::EC2::VPNGateway',
    ]) {
      expect([type, sandboxResources(template, type)]).toEqual([type, []]);
    }
    for (const type of ['AWS::EC2::VPCGatewayAttachment', 'AWS::EC2::VPCEndpoint', 'AWS::EC2::VPCPeeringConnection']) {
      expect([type, Object.values(template.findResources(type)).filter(inVpc)]).toEqual([type, []]);
    }
    // A route table's local route is implicit; any AWS::EC2::Route is a way out.
    const routeTables = sandboxResources(template, 'AWS::EC2::RouteTable').map(([id]) => id);
    expect(routeTables.length).toBeGreaterThan(0);
    const routes = Object.values(template.findResources('AWS::EC2::Route')).filter((r: any) =>
      routeTables.some((rt) => JSON.stringify(r.Properties.RouteTableId).includes(rt)),
    );
    expect(routes).toEqual([]);
  });

  it('has a security group with no ingress and only CDK’s deny-all egress placeholder', () => {
    const [, sg] = oneResource(template, 'AWS::EC2::SecurityGroup', `${SANDBOX_PREFIX}SecurityGroup`);
    expect(sg.Properties.SecurityGroupIngress ?? []).toEqual([]);
    // allowAllOutbound: false makes CDK write one rule that matches nothing.
    expect(sg.Properties.SecurityGroupEgress).toEqual([
      expect.objectContaining({ CidrIp: '255.255.255.255/32', IpProtocol: 'icmp', FromPort: 252, ToPort: 86 }),
    ]);
    const sgRef = JSON.stringify({ 'Fn::GetAtt': [oneResource(template, 'AWS::EC2::SecurityGroup', `${SANDBOX_PREFIX}SecurityGroup`)[0], 'GroupId'] });
    for (const type of ['AWS::EC2::SecurityGroupEgress', 'AWS::EC2::SecurityGroupIngress']) {
      const extra = Object.values(template.findResources(type)).filter((r: any) =>
        JSON.stringify(r.Properties.GroupId) === sgRef,
      );
      expect([type, extra]).toEqual([type, []]);
    }
  });

  it('retains the network on delete, since AgentCore’s interfaces outlive the interpreter', () => {
    const json = template.toJSON().Resources as Record<string, any>;
    const network = Object.entries(json).filter(
      ([id, r]) => id.startsWith(SANDBOX_PREFIX) && String(r.Type).startsWith('AWS::EC2::'),
    );
    expect(network.map(([, r]) => r.Type)).toEqual(
      expect.arrayContaining(['AWS::EC2::VPC', 'AWS::EC2::Subnet', 'AWS::EC2::RouteTable', 'AWS::EC2::SecurityGroup']),
    );
    for (const [id, r] of network) {
      expect([id, r.DeletionPolicy, r.UpdateReplacePolicy]).toEqual([id, 'Retain', 'Retain']);
    }
  });

  it('fits AgentCore name rules at the longest allowed prefix', () => {
    const longest = 'a2345678901234567890b'; // validateConfig allows 21 characters
    const [, ci] = oneResource(
      synthWired(longest),
      'AWS::BedrockAgentCore::CodeInterpreterCustom',
      INTERPRETER_LOGICAL_ID_PREFIX,
    );
    expect(ci.Properties.Name).toMatch(/^[a-zA-Z][a-zA-Z0-9_]{0,47}$/);
  });

  it('publishes the same interpreter’s id to the skill-script parameter', () => {
    expect(SKILL_SCRIPT_INTERPRETER_SSM_SUFFIX).toBe('agentcore/skill-script-interpreter-id');
    const [, param] = oneResource(template, 'AWS::SSM::Parameter', PARAMETER_LOGICAL_ID_PREFIX);
    expect(param.Properties.Name).toBe(`/${MOCK_PREFIX}/agentcore/skill-script-interpreter-id`);
    expect(param.Properties.Type).toBe('String');
    const [ciId] = oneResource(template, 'AWS::BedrockAgentCore::CodeInterpreterCustom', INTERPRETER_LOGICAL_ID_PREFIX);
    expect(param.Properties.Value).toEqual({ 'Fn::GetAtt': [ciId, 'CodeInterpreterId'] });
  });

  it('gives the Runtime session access to that interpreter alone', () => {
    const [ciId] = oneResource(template, 'AWS::BedrockAgentCore::CodeInterpreterCustom', INTERPRETER_LOGICAL_ID_PREFIX);
    const statements = runtimeStatements(template);
    const st = statements.find((s) => s.Sid === 'CodeInterpreterAccess');
    expect(st).toBeDefined();
    expect(st!.Effect).toBe('Allow');
    expect(actions(st!)).toEqual(SESSION_ACTIONS);
    expect(st!.Resource).toEqual({ 'Fn::GetAtt': [ciId, 'CodeInterpreterArn'] });
    expect(statements.find((s) => s.Sid === 'SkillScriptInterpreterAccess')).toBeUndefined();
  });

  it('gives the Runtime ssm:GetParameter on exactly the skill-script parameter', () => {
    const [paramId] = oneResource(template, 'AWS::SSM::Parameter', PARAMETER_LOGICAL_ID_PREFIX);
    const st = runtimeStatements(template).find((s) => s.Sid === 'SkillScriptInterpreterIdRead');
    expect(st).toBeDefined();
    expect(st!.Effect).toBe('Allow');
    expect(actions(st!)).toEqual(['ssm:GetParameter']);
    const resource = JSON.stringify(st!.Resource);
    expect(resource).toContain(paramId);
    expect(resource).not.toContain('*');
  });

  it('points the Runtime’s AGENTCORE_CODE_INTERPRETER_ID at that interpreter', () => {
    const [ciId] = oneResource(template, 'AWS::BedrockAgentCore::CodeInterpreterCustom', INTERPRETER_LOGICAL_ID_PREFIX);
    expect(runtimeEnvironment(template).AGENTCORE_CODE_INTERPRETER_ID).toEqual({
      'Fn::GetAtt': [ciId, 'CodeInterpreterId'],
    });
  });
});

describe('Runtime environment with the code sandbox', () => {
  // The exact variable names before this change. The skill-script id must not
  // become one: it is read from SSM. A new variable is a deliberate edit here.
  const DEFAULT_NAMES = [
    'AGENTCORE_CODE_INTERPRETER_ID',
    'AGENTCORE_GATEWAY_INBOUND_AUTH',
    'AGENTCORE_LOCAL_OAUTH_CALLBACK_URL',
    'AGENTCORE_MCP_APPS_SANDBOX_ORIGIN',
    'AGENTCORE_MEMORY_ID',
    'AGENTS_API_ENABLED',
    'AUTH_PROVIDER_SECRETS_ARN',
    'AWS_ACCOUNT_ID',
    'AWS_DEFAULT_REGION',
    'BROWSER_ID',
    'BROWSER_POLICY_S3',
    'CONVERSATION_INDEX_ENABLED',
    'CORS_ORIGINS',
    'FOLDER_ATTACHMENTS_ENABLED',
    'FRONTEND_URL',
    'LOG_LEVEL',
    'MEMORY_LINT',
    'MEMORY_SPACES_ENABLED',
    'PLATFORM_SELF_SERVICE_ENABLED',
    'PROJECTS_ENABLED',
    'PROJECT_PREFIX',
    'SESSION_FORK_ENABLED',
    'SESSION_REWIND_ENABLED',
    'SKILLS_ENABLED',
    'SKILL_AUTHORING_ENABLED',
    'WORD_TRACKED_CHANGES_ENABLED',
  ];

  it('has the same variable names as before on the default config', () => {
    expect(Object.keys(runtimeEnvironment(synthWired())).sort()).toEqual(DEFAULT_NAMES);
  });

  it('carries no skill-script interpreter variable in the worst case either', () => {
    const names = Object.keys(runtimeEnvironment(synthWorstCasePlatformStack().template));
    expect(names.filter((n) => /SKILL_SCRIPT/.test(n))).toEqual([]);
    // PROJECT_PREFIX is what the parameter name is derived from.
    expect(names).toContain('PROJECT_PREFIX');
  });
});
