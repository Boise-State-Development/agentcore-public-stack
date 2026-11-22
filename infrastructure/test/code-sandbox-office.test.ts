/**
 * LibreOffice for run_code: a read-only EFS mount in the code sandbox, and the
 * DNS Firewall that keeps the sandbox closed once its DNS is on
 * (docs/specs/skill-code-sandbox.md D9).
 *
 * Pins:
 *   - with the code sandbox on, a firewall answering no name sits
 *     on the sandbox VPC while DNS stays off, so it is in place before DNS is;
 *   - with the office mount on, DNS support and hostnames are on, and the
 *     firewall allows exactly the file system's own name, never a wildcard;
 *   - the sandbox's only egress is NFS to the mount targets' security group;
 *   - the run-time role may only ClientMount, through the read-only access
 *     point; only the seeding role may write, and the file-system policy
 *     denies writes and root access to everyone else and requires TLS;
 *   - the Runtime gets the flag and the parameter read, nothing more.
 */
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { AppConfig } from '../lib/config';
import { PlatformStack } from '../lib/platform-stack';
import {
  NFS_PORT,
  OFFICE_MOUNT_PATH,
  OFFICE_MOUNT_SSM_SUFFIX,
  OFFICE_READER_UID,
  OFFICE_WRITER_UID,
} from '../lib/constructs/agentcore/code-sandbox-office-construct';
import { createMockConfig, mockSsmContext, MOCK_ACCOUNT, MOCK_PREFIX, MOCK_REGION } from './helpers/mock-config';
import { runtimeEnvironment } from './helpers/runtime-worst-case';

const PREFIX = 'AgentCoreCodeInterpreter';
const RUNTIME_INTERPRETER_ROLE = 'AgentCoreCodeInterpreterCodeInterpreterExecutionRole5A00171B';

function synth(overrides: Partial<AppConfig>): Template {
  const config = createMockConfig(overrides);
  const app = new cdk.App();
  mockSsmContext(app, config);
  const stack = new PlatformStack(app, 'TestPlatformStack', {
    config,
    env: { account: MOCK_ACCOUNT, region: MOCK_REGION },
  });
  stack.wireCompute();
  return Template.fromStack(stack);
}

function resources(template: Template, type: string): [string, any][] {
  return Object.entries(template.findResources(type)).filter(([id]) => id.startsWith(PREFIX));
}

function one(template: Template, type: string, idPart = ''): [string, any] {
  const found = resources(template, type).filter(([id]) => id.includes(idPart));
  expect([type, idPart, found.length]).toEqual([type, idPart, 1]);
  return found[0];
}

function sandboxVpc(template: Template): [string, any] {
  return one(template, 'AWS::EC2::VPC', 'SandboxVpc');
}

function statementsOfRole(template: Template, roleLogicalId: string): any[] {
  return [
    ...Object.values(template.findResources('AWS::IAM::Policy')),
    ...Object.values(template.findResources('AWS::IAM::ManagedPolicy')),
  ]
    .filter((p: any) => JSON.stringify(p.Properties.Roles ?? []).includes(roleLogicalId))
    .flatMap((p: any) => p.Properties.PolicyDocument.Statement);
}

function asList(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [value];
}

const SANDBOX_ON: Partial<AppConfig> = { codeSandbox: { enabled: true } };
const OFFICE_ON: Partial<AppConfig> = { codeSandbox: { enabled: true }, codeSandboxOffice: { enabled: true } };

describe('code sandbox DNS Firewall', () => {
  it('is absent while the code sandbox is off', () => {
    const template = synth({});
    expect(Object.keys(template.findResources('AWS::Route53Resolver::FirewallRuleGroupAssociation'))).toEqual([]);
  });

  describe('with the code sandbox on and the office mount off', () => {
    let template: Template;
    beforeAll(() => {
      template = synth(SANDBOX_ON);
    });

    it('leaves DNS off and adds no file system or second interpreter', () => {
      const [, vpc] = sandboxVpc(template);
      expect(vpc.Properties.EnableDnsSupport).toBe(false);
      expect(vpc.Properties.EnableDnsHostnames).toBe(false);
      expect(Object.keys(template.findResources('AWS::EFS::FileSystem'))).toEqual([]);
      expect(Object.keys(template.findResources('AWS::BedrockAgentCore::CodeInterpreterCustom'))).toHaveLength(1);
    });

    it('associates a rule group that blocks every name and allows none', () => {
      const [vpcId] = sandboxVpc(template);
      const [, assoc] = one(template, 'AWS::Route53Resolver::FirewallRuleGroupAssociation');
      expect(assoc.Properties.VpcId).toEqual({ Ref: vpcId });
      const [groupId, group] = one(template, 'AWS::Route53Resolver::FirewallRuleGroup');
      expect(assoc.Properties.FirewallRuleGroupId).toEqual({ 'Fn::GetAtt': [groupId, 'Id'] });
      const rules = group.Properties.FirewallRules;
      expect(rules).toHaveLength(1);
      expect(rules[0]).toMatchObject({ Action: 'BLOCK', BlockResponse: 'NXDOMAIN' });
      const [listId, list] = one(template, 'AWS::Route53Resolver::FirewallDomainList');
      expect(list.Properties.Domains).toEqual(['*']);
      expect(rules[0].FirewallDomainListId).toEqual({ 'Fn::GetAtt': [listId, 'Id'] });
    });

    it('is on the VPC before the interpreter, so no session starts without it', () => {
      const [assocId] = one(template, 'AWS::Route53Resolver::FirewallRuleGroupAssociation');
      const [, ci] = one(template, 'AWS::BedrockAgentCore::CodeInterpreterCustom', 'CodeSandboxInterpreter');
      expect(asList(ci.DependsOn)).toContain(assocId);
    });

    it('writes no office variable to the Runtime', () => {
      expect(runtimeEnvironment(template).CODE_SANDBOX_OFFICE_ENABLED).toBeUndefined();
    });
  });
});

describe('code sandbox office mount', () => {
  let template: Template;
  let fsId: string;
  let roId: string;
  let rwId: string;
  let seederRoleId: string;

  beforeAll(() => {
    template = synth(OFFICE_ON);
    [fsId] = one(template, 'AWS::EFS::FileSystem');
    [roId] = one(template, 'AWS::EFS::AccessPoint', 'ReadOnly');
    [rwId] = one(template, 'AWS::EFS::AccessPoint', 'Write');
    [seederRoleId] = one(template, 'AWS::IAM::Role', 'SeederRole');
  });

  it('turns DNS support and hostnames on', () => {
    const [, vpc] = sandboxVpc(template);
    expect(vpc.Properties.EnableDnsSupport).toBe(true);
    expect(vpc.Properties.EnableDnsHostnames).toBe(true);
  });

  it('lets the firewall answer the file system’s exact name and nothing else', () => {
    const [, group] = one(template, 'AWS::Route53Resolver::FirewallRuleGroup');
    const rules = [...group.Properties.FirewallRules].sort((a: any, b: any) => a.Priority - b.Priority);
    expect(rules.map((r: any) => r.Action)).toEqual(['ALLOW', 'BLOCK']);
    const lists = Object.fromEntries(resources(template, 'AWS::Route53Resolver::FirewallDomainList'));
    const allowId = rules[0].FirewallDomainListId['Fn::GetAtt'][0];
    const blockId = rules[1].FirewallDomainListId['Fn::GetAtt'][0];
    expect(lists[blockId].Properties.Domains).toEqual(['*']);
    expect(lists[allowId].Properties.Domains).toEqual([
      { 'Fn::Join': ['', [{ 'Fn::GetAtt': [fsId, 'FileSystemId'] }, `.efs.${MOCK_REGION}.amazonaws.com`]] },
    ]);
    expect(JSON.stringify(lists[allowId].Properties.Domains)).not.toContain('*');
  });

  it('gives the sandbox NFS egress to the mount targets only', () => {
    const [sandboxSgId] = one(template, 'AWS::EC2::SecurityGroup', 'SandboxSecurityGroup');
    const [mtSgId, mtSg] = one(template, 'AWS::EC2::SecurityGroup', 'MountTargetSecurityGroup');
    const sgRef = (id: string) => JSON.stringify({ 'Fn::GetAtt': [id, 'GroupId'] });
    const egress = Object.values(template.findResources('AWS::EC2::SecurityGroupEgress'))
      .filter((r: any) => JSON.stringify(r.Properties.GroupId) === sgRef(sandboxSgId));
    expect(egress).toHaveLength(1);
    expect((egress[0] as any).Properties).toMatchObject({
      IpProtocol: 'tcp', FromPort: NFS_PORT, ToPort: NFS_PORT,
      DestinationSecurityGroupId: { 'Fn::GetAtt': [mtSgId, 'GroupId'] },
    });
    // The mount targets accept NFS from the sandbox and nothing else.
    const ingress = [
      ...(mtSg.Properties.SecurityGroupIngress ?? []),
      ...Object.values(template.findResources('AWS::EC2::SecurityGroupIngress'))
        .filter((r: any) => JSON.stringify(r.Properties.GroupId) === sgRef(mtSgId))
        .map((r: any) => r.Properties),
    ];
    expect(ingress).toHaveLength(1);
    expect(ingress[0]).toMatchObject({
      IpProtocol: 'tcp', FromPort: NFS_PORT, ToPort: NFS_PORT,
      SourceSecurityGroupId: { 'Fn::GetAtt': [sandboxSgId, 'GroupId'] },
    });
    // Still no route, gateway or endpoint: the new egress reaches the VPC only.
    expect(Object.keys(template.findResources('AWS::EC2::VPCEndpoint'))).toEqual([]);
  });

  it('puts a mount target in each sandbox subnet, after the VPC', () => {
    const [vpcId] = sandboxVpc(template);
    const targets = resources(template, 'AWS::EFS::MountTarget');
    const subnets = resources(template, 'AWS::EC2::Subnet').filter(([id]) => id.includes('Sandbox'));
    expect(targets).toHaveLength(subnets.length);
    expect(targets.map(([, t]) => t.Properties.SubnetId).sort((a: any, b: any) => a.Ref.localeCompare(b.Ref)))
      .toEqual(subnets.map(([id]) => ({ Ref: id })).sort((a, b) => a.Ref.localeCompare(b.Ref)));
    for (const [, t] of targets) {
      expect(asList(t.DependsOn)).toContain(vpcId);
    }
  });

  it('encrypts the file system and requires TLS', () => {
    const [, fs] = one(template, 'AWS::EFS::FileSystem');
    expect(fs.Properties.Encrypted).toBe(true);
    const tls = fs.Properties.FileSystemPolicy.Statement.find((s: any) => s.Sid === 'TlsOnly');
    expect(tls).toMatchObject({ Effect: 'Deny', Principal: { AWS: '*' }, Action: '*', Condition: { Bool: { 'aws:SecureTransport': 'false' } } });
  });

  it('denies writes and root access to every principal but the seeding role', () => {
    const [, fs] = one(template, 'AWS::EFS::FileSystem');
    const statements = fs.Properties.FileSystemPolicy.Statement;
    const deny = statements.find((s: any) => s.Sid === 'OnlyTheSeederWrites');
    expect(deny).toMatchObject({
      Effect: 'Deny',
      Principal: { AWS: '*' },
      Action: ['elasticfilesystem:ClientWrite', 'elasticfilesystem:ClientRootAccess'],
      Condition: { ArnNotEquals: { 'aws:PrincipalArn': { 'Fn::GetAtt': [seederRoleId, 'Arn'] } } },
    });
    const allowsWrite = statements.filter((s: any) =>
      s.Effect === 'Allow' && asList(s.Action).some((a: any) => /ClientWrite|ClientRootAccess|\*/.test(a)));
    expect(allowsWrite).toHaveLength(1);
    expect(allowsWrite[0].Principal).toEqual({ AWS: { 'Fn::GetAtt': [seederRoleId, 'Arn'] } });
  });

  it('lets the run-time role mount only through the read-only access point, never write', () => {
    const statements = statementsOfRole(template, RUNTIME_INTERPRETER_ROLE);
    const efsStatements = statements.filter((s) => asList(s.Action).some((a: any) => String(a).startsWith('elasticfilesystem:')));
    expect(efsStatements).toHaveLength(1);
    expect(efsStatements[0]).toMatchObject({
      Effect: 'Allow',
      Action: 'elasticfilesystem:ClientMount',
      Resource: { 'Fn::GetAtt': [fsId, 'Arn'] },
      Condition: { ArnEquals: { 'elasticfilesystem:AccessPointArn': { 'Fn::GetAtt': [roId, 'Arn'] } } },
    });
  });

  it('lets the seeding role write only through the write access point', () => {
    const statements = statementsOfRole(template, seederRoleId);
    const efsStatements = statements.filter((s) => asList(s.Action).some((a: any) => String(a).startsWith('elasticfilesystem:')));
    expect(efsStatements).toHaveLength(1);
    expect(efsStatements[0].Condition).toEqual({
      ArnEquals: { 'elasticfilesystem:AccessPointArn': { 'Fn::GetAtt': [rwId, 'Arn'] } },
    });
  });

  it('acts as a reader that does not own the tree on the read-only access point', () => {
    const [, ro] = one(template, 'AWS::EFS::AccessPoint', 'ReadOnly');
    const [, rw] = one(template, 'AWS::EFS::AccessPoint', 'Write');
    expect(ro.Properties.PosixUser).toEqual({ Uid: String(OFFICE_READER_UID), Gid: String(OFFICE_READER_UID) });
    expect(rw.Properties.PosixUser).toEqual({ Uid: String(OFFICE_WRITER_UID), Gid: String(OFFICE_WRITER_UID) });
    expect(ro.Properties.RootDirectory.CreationInfo.OwnerUid).toBe(String(OFFICE_WRITER_UID));
    expect(OFFICE_READER_UID).not.toBe(OFFICE_WRITER_UID);
  });

  it('runs the seeding interpreter in the same isolated network, on its own role', () => {
    const interpreters = resources(template, 'AWS::BedrockAgentCore::CodeInterpreterCustom');
    expect(interpreters).toHaveLength(2);
    const [, seeder] = one(template, 'AWS::BedrockAgentCore::CodeInterpreterCustom', 'SeederInterpreter');
    const [, runtime] = one(template, 'AWS::BedrockAgentCore::CodeInterpreterCustom', 'CodeSandboxInterpreter');
    expect(seeder.Properties.NetworkConfiguration).toEqual(runtime.Properties.NetworkConfiguration);
    expect(seeder.Properties.ExecutionRoleArn).toEqual({ 'Fn::GetAtt': [seederRoleId, 'Arn'] });
    expect(runtime.Properties.ExecutionRoleArn).toEqual({ 'Fn::GetAtt': [RUNTIME_INTERPRETER_ROLE, 'Arn'] });
    expect(seeder.Properties.Name).toMatch(/^[a-zA-Z][a-zA-Z0-9_]{0,47}$/);
  });

  it('never gives the Runtime role a file-system or seeder grant', () => {
    const statements = statementsOfRole(template, 'AgentCoreRuntimeExecutionRole');
    expect(statements.filter((s) => asList(s.Action).some((a: any) => String(a).startsWith('elasticfilesystem:')))).toEqual([]);
    const access = statements.find((s) => s.Sid === 'CodeInterpreterAccess');
    expect(JSON.stringify(access.Resource)).not.toContain('SeederInterpreter');
  });

  it('publishes the mount to SSM and lets the Runtime read it', () => {
    const [paramId, param] = one(template, 'AWS::SSM::Parameter', 'MountParameter');
    expect(param.Properties.Name).toBe(`/${MOCK_PREFIX}/${OFFICE_MOUNT_SSM_SUFFIX}`);
    const value = JSON.stringify(param.Properties.Value);
    for (const key of ['fileSystemArn', 'readOnlyAccessPointArn', 'writeAccessPointArn', 'seederInterpreterId', OFFICE_MOUNT_PATH]) {
      expect(value).toContain(key);
    }
    const read = statementsOfRole(template, 'AgentCoreRuntimeExecutionRole').find((s) => s.Sid === 'SkillScriptInterpreterIdRead');
    expect(JSON.stringify(read.Resource)).toContain(paramId);
    expect(runtimeEnvironment(template).CODE_SANDBOX_OFFICE_ENABLED).toBe('true');
  });
});
