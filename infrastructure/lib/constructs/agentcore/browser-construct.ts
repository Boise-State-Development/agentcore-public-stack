import * as bedrock from 'aws-cdk-lib/aws-bedrockagentcore';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

export interface AgentCoreBrowserConstructProps {
  config: AppConfig;
}

/**
 * AgentCoreBrowserConstruct — Bedrock AgentCore Browser Custom +
 * execution role + SSM publications.
 *
 * Hoisted from a sibling construct's InferenceAgentCoreConstruct as part of
 * no out-of-band updates needed. Belongs in the rarely-deployed
 * Platform layer.
 *
 * Public properties:
 *   browser: bedrock.CfnBrowserCustom
 *   browserArn: string
 *   browserId: string
 */
export class AgentCoreBrowserConstruct extends Construct {
  public readonly browser: bedrock.CfnBrowserCustom;
  public readonly browserArn: string;
  public readonly browserId: string;
  public readonly executionRole: iam.Role;

  constructor(scope: Construct, id: string, props: AgentCoreBrowserConstructProps) {
    super(scope, id);

    const { config } = props;

    // ── IAM execution role ──
    this.executionRole = new iam.Role(this, 'BrowserExecutionRole', {
      roleName: getResourceName(config, 'browser-role'),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for AgentCore Browser',
    });
    this.executionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents'],
      resources: [
        `arn:aws:logs:${config.awsRegion}:${config.awsAccount}:log-group:/aws/bedrock/agentcore/${config.projectPrefix}/browser/*`,
      ],
    }));

    // ── Browser Custom resource ──
    this.browser = new bedrock.CfnBrowserCustom(this, 'BrowserCustom', {
      name: getResourceName(config, 'browser').replace(/-/g, '_'),
      description: 'Custom Browser for secure web interaction and data extraction',
      networkConfiguration: { networkMode: 'PUBLIC' },
      executionRoleArn: this.executionRole.roleArn,
    });
    this.browser.node.addDependency(this.executionRole);

    this.browserArn = this.browser.attrBrowserArn;
    this.browserId = this.browser.attrBrowserId;

    // ── SSM publications ──
  }
}
