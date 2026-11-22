import * as cdk from 'aws-cdk-lib';
import * as bedrock from 'aws-cdk-lib/aws-bedrockagentcore';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as efs from 'aws-cdk-lib/aws-efs';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as route53resolver from 'aws-cdk-lib/aws-route53resolver';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

/**
 * SSM parameter (under `/<prefix>/`) holding the office mount's ARNs and the
 * seeding interpreter's id, as JSON. The ids are service-generated, so they
 * are read from here rather than the Runtime's environment (2,560-byte V2
 * budget). Must match `OFFICE_MOUNT_SSM_SUFFIX` in
 * backend/src/agents/builtin_tools/code_sandbox/office_mount.py.
 */
export const OFFICE_MOUNT_SSM_SUFFIX = 'agentcore/code-sandbox-office-mount';

/** Where a session sees the file system. Must match the backend's MOUNT_PATH. */
export const OFFICE_MOUNT_PATH = '/mnt/office';

/** The access points' root on the file system. */
export const OFFICE_ROOT_DIRECTORY = '/office';

/**
 * POSIX identities the access points act as. The write one owns the tree; the
 * read one does not, so even a write that IAM let through would fail on the
 * file modes.
 */
export const OFFICE_WRITER_UID = 1000;
export const OFFICE_READER_UID = 2000;

/** NFS. The only port the sandbox may reach, and only on the mount targets. */
export const NFS_PORT = 2049;

export interface CodeSandboxDnsFirewallProps {
  config: AppConfig;
  vpc: ec2.IVpc;
  /**
   * Exact names the resolver may answer. Empty while the office mount is off:
   * DNS support is off then too, so the firewall only stands ready.
   */
  allowedDomains: string[];
}

/**
 * A Route 53 Resolver DNS Firewall on the code sandbox's VPC that answers
 * only `allowedDomains` and NXDOMAIN for everything else.
 *
 * D8 turned the VPC's DNS support off because security groups do not filter
 * the Amazon resolver, so a lookup of a name an attacker's server answers
 * could carry data out. The office mount needs DNS back on (AgentCore mounts
 * EFS by its DNS name), and this is what keeps lookups closed then:
 *   - the allow list holds exact names only, never a wildcard, so no
 *     `<data>.allowed-domain` name can be used as a channel;
 *   - a blocked query is answered at the resolver and never forwarded;
 *   - the VPC's firewall config is left at its default, fail closed
 *     (`FirewallFailOpen: DISABLED`), so a firewall outage stops lookups
 *     rather than letting them through. `AWS::Route53Resolver::FirewallConfig`
 *     could pin it, but CDK 2.274 has no L1 for it and its template validator
 *     warns on the type on every synth.
 *
 * It exists wherever the code sandbox does, DNS on or off, so it is already
 * associated by the time a later deploy turns DNS on: CloudFormation updates
 * the VPC before it creates anything that references it, so a firewall
 * created in the same deploy that turns DNS on would leave a window.
 *
 * Mutation protection is left off: with it on, CloudFormation cannot remove
 * the association on a rollback or teardown, which would leave the stack in
 * UPDATE_ROLLBACK_FAILED. The CDK test pins the association instead.
 */
export class CodeSandboxDnsFirewall extends Construct {
  public readonly association: route53resolver.CfnFirewallRuleGroupAssociation;

  constructor(scope: Construct, id: string, props: CodeSandboxDnsFirewallProps) {
    super(scope, id);
    const { config, vpc, allowedDomains } = props;

    const blockAll = new route53resolver.CfnFirewallDomainList(this, 'BlockAllDomains', {
      name: getResourceName(config, 'code-sandbox-dns-block-all'),
      domains: ['*'],
    });

    const rules: route53resolver.CfnFirewallRuleGroup.FirewallRuleProperty[] = [];
    if (allowedDomains.length > 0) {
      const allow = new route53resolver.CfnFirewallDomainList(this, 'AllowedDomains', {
        name: getResourceName(config, 'code-sandbox-dns-allowed'),
        domains: allowedDomains,
      });
      rules.push({ priority: 100, action: 'ALLOW', firewallDomainListId: allow.attrId });
    }
    rules.push({
      priority: 200,
      action: 'BLOCK',
      blockResponse: 'NXDOMAIN',
      firewallDomainListId: blockAll.attrId,
    });

    const ruleGroup = new route53resolver.CfnFirewallRuleGroup(this, 'RuleGroup', {
      name: getResourceName(config, 'code-sandbox-dns'),
      firewallRules: rules,
    });

    this.association = new route53resolver.CfnFirewallRuleGroupAssociation(this, 'Association', {
      name: getResourceName(config, 'code-sandbox-dns'),
      firewallRuleGroupId: ruleGroup.attrId,
      vpcId: vpc.vpcId,
      // 101 is the lowest priority a customer rule group may take.
      priority: 101,
      mutationProtection: 'DISABLED',
    });

  }
}

export interface CodeSandboxOfficeMountProps {
  config: AppConfig;
  vpc: ec2.Vpc;
  /** The sandbox's security group: it gets egress to the mount targets only. */
  sandboxSecurityGroup: ec2.SecurityGroup;
  /** The run-time interpreter's role: it may mount read-only, nothing more. */
  runtimeInterpreterRole: iam.Role;
}

/**
 * An EFS file system holding a LibreOffice build, mounted read-only into the
 * code sandbox's sessions at `/mnt/office` (docs/specs/skill-code-sandbox.md
 * D9).
 *
 * The file system is shared by every user's sessions, so it must never be
 * writable from one: a writable tree would let one user's script replace
 * `soffice` with a trojan for the next user's run. Three layers:
 *   - the run-time interpreter's role may `ClientMount`, only through the
 *     read-only access point, and has no `ClientWrite` at all;
 *   - the file-system policy denies `ClientWrite` and `ClientRootAccess` to
 *     every principal but the seeding interpreter's role (an explicit deny no
 *     identity policy can override), requires TLS, and allows mounts only
 *     through a mount target by the two interpreter roles;
 *   - the read-only access point acts as a POSIX user that does not own the
 *     tree.
 *
 * The seeding interpreter is a second Code Interpreter in the same isolated
 * network, on its own role, used only by
 * `scripts/code-sandbox/seed-office.py` to install or upgrade LibreOffice.
 * Nothing on the turn path starts sessions on it.
 */
export class CodeSandboxOfficeMount extends Construct {
  public readonly fileSystem: efs.CfnFileSystem;
  public readonly fileSystemArn: string;
  public readonly readOnlyAccessPoint: efs.CfnAccessPoint;
  public readonly writeAccessPoint: efs.CfnAccessPoint;
  public readonly seederRole: iam.Role;
  public readonly seederInterpreter: bedrock.CfnCodeInterpreterCustom;
  public readonly mountParameter: ssm.StringParameter;
  /** The one name the DNS Firewall answers: the file system's own. */
  public readonly dnsName: string;

  constructor(scope: Construct, id: string, props: CodeSandboxOfficeMountProps) {
    super(scope, id);
    const { config, vpc, sandboxSecurityGroup, runtimeInterpreterRole } = props;

    // Explicit, stable name: the seeding interpreter's executionRoleArn is
    // create-only (see the run-time role in code-interpreter-construct.ts).
    this.seederRole = new iam.Role(this, 'SeederRole', {
      roleName: getResourceName(config, 'code-sandbox-seeder-role'),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for the code sandbox seeding interpreter: the only writer of the office file system',
    });
    this.seederRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [
        `arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:/aws/bedrock/agentcore/${config.projectPrefix}/code-interpreter/*`,
      ],
    }));

    const mountTargetSecurityGroup = new ec2.SecurityGroup(this, 'MountTargetSecurityGroup', {
      vpc,
      securityGroupName: getResourceName(config, 'code-sandbox-efs-sg'),
      description: 'Office file system mount targets: NFS from the code sandbox only',
      allowAllOutbound: false,
      allowAllIpv6Outbound: false,
    });
    mountTargetSecurityGroup.addIngressRule(sandboxSecurityGroup, ec2.Port.tcp(NFS_PORT), 'NFS from the code sandbox');
    sandboxSecurityGroup.addEgressRule(mountTargetSecurityGroup, ec2.Port.tcp(NFS_PORT), 'NFS to the office file system only');

    const fileSystemPolicy = new iam.PolicyDocument({
      statements: [
        new iam.PolicyStatement({
          sid: 'TlsOnly',
          effect: iam.Effect.DENY,
          principals: [new iam.AnyPrincipal()],
          actions: ['*'],
          resources: ['*'],
          conditions: { Bool: { 'aws:SecureTransport': 'false' } },
        }),
        new iam.PolicyStatement({
          sid: 'OnlyTheSeederWrites',
          effect: iam.Effect.DENY,
          principals: [new iam.AnyPrincipal()],
          actions: ['elasticfilesystem:ClientWrite', 'elasticfilesystem:ClientRootAccess'],
          resources: ['*'],
          conditions: { ArnNotEquals: { 'aws:PrincipalArn': this.seederRole.roleArn } },
        }),
        new iam.PolicyStatement({
          sid: 'MountThroughAMountTarget',
          effect: iam.Effect.ALLOW,
          principals: [
            new iam.ArnPrincipal(runtimeInterpreterRole.roleArn),
            new iam.ArnPrincipal(this.seederRole.roleArn),
          ],
          actions: ['elasticfilesystem:ClientMount'],
          resources: ['*'],
          conditions: { Bool: { 'elasticfilesystem:AccessedViaMountTarget': 'true' } },
        }),
        new iam.PolicyStatement({
          sid: 'SeederWrites',
          effect: iam.Effect.ALLOW,
          principals: [new iam.ArnPrincipal(this.seederRole.roleArn)],
          actions: ['elasticfilesystem:ClientWrite'],
          resources: ['*'],
          conditions: { Bool: { 'elasticfilesystem:AccessedViaMountTarget': 'true' } },
        }),
      ],
    });

    this.fileSystem = new efs.CfnFileSystem(this, 'FileSystem', {
      encrypted: true,
      performanceMode: 'generalPurpose',
      throughputMode: 'elastic',
      fileSystemPolicy: fileSystemPolicy.toJSON(),
      // LibreOffice only, re-seeded from a pinned build: nothing to back up.
      backupPolicy: { status: 'DISABLED' },
      fileSystemTags: [{ key: 'Name', value: getResourceName(config, 'code-sandbox-office') }],
    });
    // The policy's role ARNs already make the file system wait for both roles.
    // No construct-level dependency: it would include the roles' policies,
    // which reference the file system, and make a cycle.
    this.fileSystemArn = this.fileSystem.attrArn;
    this.dnsName = `${this.fileSystem.attrFileSystemId}.efs.${config.awsRegion}.amazonaws.com`;

    // One mount target per sandbox subnet (one per AZ), so a session in either
    // AZ mounts locally. A mount target gets its DNS name only if the VPC's
    // hostnames are already on when it is created, so it waits for the VPC.
    const vpcResource = vpc.node.defaultChild as ec2.CfnVPC;
    vpc.isolatedSubnets.forEach((subnet, index) => {
      const target = new efs.CfnMountTarget(this, `MountTarget${index + 1}`, {
        fileSystemId: this.fileSystem.attrFileSystemId,
        subnetId: subnet.subnetId,
        securityGroups: [mountTargetSecurityGroup.securityGroupId],
      });
      target.addDependency(vpcResource);
    });

    const accessPoint = (logicalId: string, uid: number, name: string) =>
      new efs.CfnAccessPoint(this, logicalId, {
        fileSystemId: this.fileSystem.attrFileSystemId,
        posixUser: { uid: String(uid), gid: String(uid) },
        rootDirectory: {
          path: OFFICE_ROOT_DIRECTORY,
          creationInfo: { ownerUid: String(OFFICE_WRITER_UID), ownerGid: String(OFFICE_WRITER_UID), permissions: '0755' },
        },
        accessPointTags: [{ key: 'Name', value: getResourceName(config, name) }],
      });
    this.writeAccessPoint = accessPoint('WriteAccessPoint', OFFICE_WRITER_UID, 'code-sandbox-office-write');
    this.readOnlyAccessPoint = accessPoint('ReadOnlyAccessPoint', OFFICE_READER_UID, 'code-sandbox-office-read');

    // The run-time interpreter mounts the read-only access point, and only it.
    runtimeInterpreterRole.addToPolicy(new iam.PolicyStatement({
      sid: 'OfficeMountReadOnly',
      effect: iam.Effect.ALLOW,
      actions: ['elasticfilesystem:ClientMount'],
      resources: [this.fileSystemArn],
      conditions: { ArnEquals: { 'elasticfilesystem:AccessPointArn': this.readOnlyAccessPoint.attrArn } },
    }));
    this.seederRole.addToPolicy(new iam.PolicyStatement({
      sid: 'OfficeMountWrite',
      effect: iam.Effect.ALLOW,
      actions: ['elasticfilesystem:ClientMount', 'elasticfilesystem:ClientWrite'],
      resources: [this.fileSystemArn],
      conditions: { ArnEquals: { 'elasticfilesystem:AccessPointArn': this.writeAccessPoint.attrArn } },
    }));

    this.seederInterpreter = new bedrock.CfnCodeInterpreterCustom(this, 'SeederInterpreter', {
      name: getResourceName(config, 'code_sandbox_seeder').replace(/-/g, '_'),
      description: 'Installs LibreOffice onto the code sandbox office file system; no network path out',
      networkConfiguration: {
        networkMode: 'VPC',
        vpcConfig: {
          subnets: vpc.isolatedSubnets.map((s) => s.subnetId),
          securityGroups: [sandboxSecurityGroup.securityGroupId],
        },
      },
      executionRoleArn: this.seederRole.roleArn,
    });
    this.seederInterpreter.node.addDependency(this.seederRole, vpc);

    this.mountParameter = new ssm.StringParameter(this, 'MountParameter', {
      parameterName: `/${config.projectPrefix}/${OFFICE_MOUNT_SSM_SUFFIX}`,
      stringValue: cdk.Stack.of(this).toJsonString({
        fileSystemArn: this.fileSystemArn,
        readOnlyAccessPointArn: this.readOnlyAccessPoint.attrArn,
        writeAccessPointArn: this.writeAccessPoint.attrArn,
        seederInterpreterId: this.seederInterpreter.attrCodeInterpreterId,
        mountPath: OFFICE_MOUNT_PATH,
      }),
      description: 'Code sandbox office mount: file system, access points and seeding interpreter',
      tier: ssm.ParameterTier.STANDARD,
    });
  }
}
