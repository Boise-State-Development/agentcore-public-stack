import * as cdk from 'aws-cdk-lib';
import * as bedrock from 'aws-cdk-lib/aws-bedrockagentcore';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';
import { CodeSandboxDnsFirewall, CodeSandboxOfficeMount } from './code-sandbox-office-construct';

/**
 * SSM parameter (under `/<prefix>/`) holding the interpreter id skill scripts
 * use. The Runtime derives the name from PROJECT_PREFIX, so it costs no
 * environment bytes. Must match `SKILL_SCRIPT_INTERPRETER_SSM_SUFFIX` in
 * backend/src/agents/main_agent/skills/skill_scripts.py.
 */
export const SKILL_SCRIPT_INTERPRETER_SSM_SUFFIX = 'agentcore/skill-script-interpreter-id';

/**
 * The isolated VPC's address range. It is never peered, routed or attached to
 * anything, so it may overlap any other network without consequence; a fixed
 * value keeps it out of every deployment's config.
 */
export const CODE_SANDBOX_VPC_CIDR = '10.255.0.0/24';

export interface AgentCoreCodeInterpreterConstructProps {
  config: AppConfig;
}

/**
 * AgentCoreCodeInterpreterConstruct — the one Bedrock AgentCore Code
 * Interpreter every tool uses, with no network path out.
 *
 * Every caller runs code against files the Runtime copies in and reads back
 * through the Code Interpreter API: model-written python-docx / openpyxl /
 * python-pptx / matplotlib / pandas code, and skill `scripts/*.py`, which an
 * author other than the invoking user may have written. None of it needs a
 * network, and any network is a way to send a user's document somewhere.
 *
 * Why `VPC` mode and not `SANDBOX`: AWS documents SANDBOX as allowing Amazon
 * S3 access, and this interpreter should reach nothing at all. In VPC mode
 * the sandbox's only interface is in the VPC below, which has:
 *   - isolated subnets: no internet gateway, no NAT, no endpoints, so no route
 *     to anything;
 *   - a security group with no egress rules;
 *   - DNS support off. Security groups do not filter the VPC's Amazon DNS
 *     resolver, and with it on, public names resolved from the sandbox, so a
 *     lookup of a name an attacker's server answers could carry data out.
 *     Wherever the code sandbox is on, a DNS Firewall answering no name also
 *     sits on the VPC, so it is in place before DNS is ever turned on.
 *
 * With the office mount on (`codeSandboxOffice`, spec D9), DNS support and
 * hostnames are on, because AgentCore mounts EFS by its DNS name; the
 * firewall then answers that one name and nothing else, and the security
 * group's only egress is NFS to the file system's mount targets
 * (code-sandbox-office-construct.ts).
 *
 * Replaces the PUBLIC interpreter (model-written code) and the SANDBOX one
 * (skill scripts). The new interpreter has its own name because both old
 * names are released only when CloudFormation deletes them, after the update.
 *
 * Public properties:
 *   codeInterpreter: bedrock.CfnCodeInterpreterCustom
 *   codeInterpreterArn: string
 *   codeInterpreterId: string
 *   skillScriptInterpreterIdParameter: ssm.StringParameter (holds the same id)
 *   vpc: ec2.Vpc (the isolated VPC)
 */
export class AgentCoreCodeInterpreterConstruct extends Construct {
  public readonly codeInterpreter: bedrock.CfnCodeInterpreterCustom;
  public readonly codeInterpreterArn: string;
  public readonly codeInterpreterId: string;
  public readonly executionRole: iam.Role;
  public readonly skillScriptInterpreterIdParameter: ssm.StringParameter;
  public readonly vpc: ec2.Vpc;
  public readonly securityGroup: ec2.SecurityGroup;
  public readonly dnsFirewall?: CodeSandboxDnsFirewall;
  public readonly officeMount?: CodeSandboxOfficeMount;

  constructor(scope: Construct, id: string, props: AgentCoreCodeInterpreterConstructProps) {
    super(scope, id);

    const { config } = props;
    // AgentCore mounts EFS by its DNS name, which needs both switches.
    const officeOn = config.codeSandboxOffice.enabled;

    // ── IAM execution role ──
    // IMPORTANT: keep an explicit, stable roleName. This role's ARN is
    // consumed by the CodeInterpreterCustom `executionRoleArn`, a
    // CREATE-ONLY property — renaming the role replaces it, which forces
    // CodeInterpreterCustom replacement and a same-Name "already exists"
    // collision on already-deployed stacks. See browser-construct.ts.
    this.executionRole = new iam.Role(this, 'CodeInterpreterExecutionRole', {
      roleName: getResourceName(config, 'code-interpreter-role'),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for AgentCore Code Interpreter',
    });
    this.executionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [
        `arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:/aws/bedrock/agentcore/${config.projectPrefix}/code-interpreter/*`,
      ],
    }));

    // ── Isolated network ──
    // Its own VPC rather than subnets in the platform VPC: DNS support is a
    // VPC-wide switch, and app-api's tasks in the platform VPC need DNS.
    this.vpc = new ec2.Vpc(this, 'SandboxVpc', {
      vpcName: getResourceName(config, 'code-sandbox-vpc'),
      ipAddresses: ec2.IpAddresses.cidr(CODE_SANDBOX_VPC_CIDR),
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [
        { cidrMask: 26, name: 'Isolated', subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      ],
      enableDnsSupport: officeOn,
      enableDnsHostnames: officeOn,
    });

    this.securityGroup = new ec2.SecurityGroup(this, 'SandboxSecurityGroup', {
      vpc: this.vpc,
      securityGroupName: getResourceName(config, 'code-sandbox-sg'),
      description: 'Code Interpreter sandbox: no ingress, no egress',
      allowAllOutbound: false,
      allowAllIpv6Outbound: false,
    });

    // Retain the network on delete. AgentCore's own interfaces in these
    // subnets (InterfaceType `agentic_ai`, not deletable by us) outlived a
    // deleted spike interpreter on dev by about 100 minutes, so a delete of
    // the VPC right after the interpreter's fails with DependencyViolation. On a
    // rollback of the deploy that creates them, that failure would leave the
    // stack in UPDATE_ROLLBACK_FAILED; retained, the VPC, subnets, route tables
    // and security group (all free) are simply left behind to delete by hand.
    for (const child of [...this.vpc.node.findAll(), ...this.securityGroup.node.findAll()]) {
      if (child instanceof cdk.CfnResource) {
        child.applyRemovalPolicy(cdk.RemovalPolicy.RETAIN);
      }
    }

    // ── LibreOffice mount and DNS Firewall ──
    // Built after the retain loop above, so the mount's security group and its
    // rules are not retained: they hold no AgentCore interfaces, and turning
    // the mount off should delete them with it.
    if (officeOn) {
      this.officeMount = new CodeSandboxOfficeMount(this, 'OfficeMount', {
        config,
        vpc: this.vpc,
        sandboxSecurityGroup: this.securityGroup,
        runtimeInterpreterRole: this.executionRole,
      });
    }
    if (config.codeSandbox.enabled) {
      this.dnsFirewall = new CodeSandboxDnsFirewall(this, 'DnsFirewall', {
        config,
        vpc: this.vpc,
        allowedDomains: this.officeMount ? [this.officeMount.dnsName] : [],
      });
    }

    // ── Code Interpreter Custom resource ──
    this.codeInterpreter = new bedrock.CfnCodeInterpreterCustom(this, 'CodeSandboxInterpreter', {
      name: getResourceName(config, 'code_sandbox').replace(/-/g, '_'),
      description: 'Code Interpreter for all tool code, with no network path out',
      networkConfiguration: {
        networkMode: 'VPC',
        vpcConfig: {
          subnets: this.vpc.isolatedSubnets.map((s) => s.subnetId),
          securityGroups: [this.securityGroup.securityGroupId],
        },
      },
      executionRoleArn: this.executionRole.roleArn,
    });
    this.codeInterpreter.node.addDependency(this.executionRole);
    // The service creates its interfaces in these subnets; they must exist
    // first, and must outlive the interpreter on delete.
    this.codeInterpreter.node.addDependency(this.vpc);
    // On a new stack, no session can start before the firewall is on the VPC.
    if (this.dnsFirewall) {
      this.codeInterpreter.node.addDependency(this.dnsFirewall.association);
    }

    this.codeInterpreterArn = this.codeInterpreter.attrCodeInterpreterArn;
    this.codeInterpreterId = this.codeInterpreter.attrCodeInterpreterId;

    // ── SSM publications ──
    // Skill scripts resolve their interpreter from this parameter, not the
    // Runtime's environment (2,560-byte V2 budget). It names the same
    // interpreter now; the parameter stays so the backend needs no change.
    this.skillScriptInterpreterIdParameter = new ssm.StringParameter(this, 'SkillScriptInterpreterIdParameter', {
      parameterName: `/${config.projectPrefix}/${SKILL_SCRIPT_INTERPRETER_SSM_SUFFIX}`,
      stringValue: this.codeInterpreterId,
      description: 'AgentCore Code Interpreter id for skill scripts (no network path out)',
      tier: ssm.ParameterTier.STANDARD,
    });
  }
}
