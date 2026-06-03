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
 *   - Gateway IAM execution role with `lambda:InvokeFunction` against
 *     `arn:aws:lambda:.../function:{prefix}-mcp-*` (MCP Lambda
 *     functions are owned by the external mcp-servers repo, not this
 *     CDK app — the role grants invoke rights against the naming
 *     convention)
 *   - CloudWatch Logs publish rights for gateway-scoped log groups
 *   - `agentcore.CfnGateway` configured for MCP protocol with AWS_IAM
 *     authorizer and SEMANTIC search type
 *
 * SSM publications:
 *   /{prefix}/gateway/url   — for SigV4-authenticated remote invocation
 *   /{prefix}/gateway/id    — gateway identifier
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

    this.gatewayRole = new iam.Role(this, 'GatewayExecutionRole', {
      roleName: getResourceName(config, 'gateway-role'),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for AgentCore Gateway',
    });

    this.gatewayRole.addToPolicy(
      new iam.PolicyStatement({
        sid: 'LambdaInvokeAccess',
        effect: iam.Effect.ALLOW,
        actions: ['lambda:InvokeFunction'],
        resources: [
          `arn:aws:lambda:${stack.region}:${stack.account}:function:${config.projectPrefix}-mcp-*`,
        ],
      }),
    );

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
