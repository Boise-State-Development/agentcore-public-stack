import * as cdk from 'aws-cdk-lib';
import * as agentcore from 'aws-cdk-lib/aws-bedrockagentcore';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

export interface AgentCoreGatewayConstructProps {
  config: AppConfig;
}

/**
 * AgentCoreGatewayConstruct — AWS Bedrock AgentCore Gateway with MCP
 * protocol and AWS_IAM (SigV4) authorization.
 *
 * Provides:
 *   - Gateway IAM execution role with NO standing Lambda-invoke grant. The
 *     permission to invoke an `mcpServer` target's Lambda Function URL is
 *     granted *per target* at registration time by app-api
 *     (`lambda:AddPermission` on the function's resource policy, naming this
 *     role), and revoked on delete — so an admin can register any same-account
 *     MCP server through the form with no infra change, and the role can invoke
 *     only the functions explicitly registered (no naming-convention wildcard).
 *     See `apis/shared/tools/gateway_lambda_grant.py`.
 *   - CloudWatch Logs publish rights for gateway-scoped log groups
 *   - `agentcore.CfnGateway` configured for MCP protocol with AWS_IAM
 *     authorizer and SEMANTIC search type
 *
 * SSM publications:
 *   /{prefix}/gateway/id    — gateway identifier, read at runtime by app-api's
 *                             GatewayTargetService (issue #419) to manage MCP
 *                             targets. Reading from SSM at runtime (not at CFN
 *                             deploy time) sidesteps the same-stack ordering
 *                             deadlock that forces sibling refs (e.g. Memory id)
 *                             to be threaded as explicit props.
 *
 * Also emits CloudFormation outputs for deploy-time visibility:
 *   GatewayArn, GatewayUrl, GatewayId, GatewayStatus, UsageInstructions
 */
export class AgentCoreGatewayConstruct extends Construct {
  public readonly gateway: agentcore.CfnGateway;
  public readonly gatewayRole: iam.Role;

  constructor(
    scope: Construct,
    id: string,
    props: AgentCoreGatewayConstructProps,
  ) {
    super(scope, id);

    const { config } = props;
    const stack = cdk.Stack.of(this);

    // IMPORTANT: keep an explicit, stable roleName. This role's ARN is
    // consumed by the Gateway `roleArn` — renaming the role (auto-gen)
    // replaces it and risks Gateway replacement on deployed stacks.
    // See browser-construct.ts.
    this.gatewayRole = new iam.Role(this, 'GatewayExecutionRole', {
      roleName: getResourceName(config, 'gateway-role'),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for AgentCore Gateway',
    });

    // NOTE: no standing `lambda:Invoke*` grant. Invoke permission for each
    // mcpServer target's Lambda Function URL is granted per-target at
    // registration by app-api (lambda:AddPermission naming this role) and
    // revoked on delete — least-privilege, and admins add servers with no infra
    // change. See AgentCoreGatewayTargetAccess on the app-api role.

    this.gatewayRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'GatewayLogsAccess',
        effect: iam.Effect.ALLOW,
        actions: [
          'logs:CreateLogGroup',
          'logs:CreateLogStream',
          'logs:PutLogEvents',
        ],
        resources: [
          `arn:aws:logs:${stack.region}:${stack.account}:log-group:/aws/bedrock-agentcore/gateways/*`,
        ],
      }),
    );

    this.gateway = new agentcore.CfnGateway(this, 'MCPGateway', {
      name: getResourceName(config, 'mcp-gateway'),
      description: 'MCP Gateway for external tools',
      roleArn: this.gatewayRole.roleArn,
      authorizerType: 'AWS_IAM',
      protocolType: 'MCP',
      exceptionLevel: 'DEBUG', // Only DEBUG is supported
      protocolConfiguration: {
        mcp: {
          supportedVersions: ['2025-11-25'],
          searchType: 'SEMANTIC',
        },
      },
    });

    const gatewayArn = `arn:aws:bedrock-agentcore:${stack.region}:${stack.account}:gateway/${this.gateway.attrGatewayIdentifier}`;
    const gatewayUrl = this.gateway.attrGatewayUrl;
    const gatewayId = this.gateway.attrGatewayIdentifier;

    // Publish the gateway id so app-api (a sibling in this stack) can resolve
    // it at runtime via SSM to manage MCP targets (issue #419). app-api reads
    // this with `ssm:GetParameter` from the running container — never at CFN
    // synth/deploy time — so there is no same-stack publish/consume ordering
    // problem.
    new ssm.StringParameter(this, 'GatewayIdParam', {
      parameterName: `/${config.projectPrefix}/gateway/id`,
      stringValue: gatewayId,
      description: 'AgentCore Gateway identifier (consumed by app-api GatewayTargetService)',
    });

    new cdk.CfnOutput(this, 'GatewayArn', {
      value: gatewayArn,
      description: 'AgentCore Gateway ARN',
      exportName: getResourceName(config, 'gateway-arn'),
    });

    new cdk.CfnOutput(this, 'GatewayUrl', {
      value: gatewayUrl,
      description: 'AgentCore Gateway URL (requires SigV4 authentication)',
      exportName: getResourceName(config, 'gateway-url'),
    });

    new cdk.CfnOutput(this, 'GatewayId', {
      value: gatewayId,
      description: 'AgentCore Gateway Identifier',
      exportName: getResourceName(config, 'gateway-id'),
    });

    new cdk.CfnOutput(this, 'GatewayStatus', {
      value: this.gateway.attrStatus,
      description: 'Gateway Status',
    });

    new cdk.CfnOutput(this, 'UsageInstructions', {
      value: `
Gateway URL: ${gatewayUrl}
Authentication: AWS_IAM (SigV4)

To test Gateway connectivity:
  aws bedrock-agentcore invoke-gateway \\
    --gateway-identifier ${gatewayId} \\
    --region ${stack.region}
      `.trim(),
      description: 'Usage instructions for Gateway',
    });
  }
}
