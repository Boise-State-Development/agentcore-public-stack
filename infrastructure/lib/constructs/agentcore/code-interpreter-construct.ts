import * as bedrock from 'aws-cdk-lib/aws-bedrockagentcore';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

export interface AgentCoreCodeInterpreterConstructProps {
  config: AppConfig;
}

/**
 * AgentCoreCodeInterpreterConstruct — Bedrock AgentCore Code
 * Interpreter Custom + execution role + SSM publications.
 *
 * Hoisted from a sibling construct's InferenceAgentCoreConstruct as part of
 * no out-of-band updates needed. Belongs in the rarely-deployed
 * Platform layer.
 *
 * Public properties:
 *   codeInterpreter: bedrock.CfnCodeInterpreterCustom
 *   codeInterpreterArn: string
 *   codeInterpreterId: string
 */
export class AgentCoreCodeInterpreterConstruct extends Construct {
  public readonly codeInterpreter: bedrock.CfnCodeInterpreterCustom;
  public readonly codeInterpreterArn: string;
  public readonly codeInterpreterId: string;
  public readonly executionRole: iam.Role;

  constructor(scope: Construct, id: string, props: AgentCoreCodeInterpreterConstructProps) {
    super(scope, id);

    const { config } = props;

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

    // ── Code Interpreter Custom resource ──
    this.codeInterpreter = new bedrock.CfnCodeInterpreterCustom(this, 'CodeInterpreterCustom', {
      name: getResourceName(config, 'code_interpreter').replace(/-/g, '_'),
      description: 'Custom Code Interpreter for Python code execution with advanced configuration',
      networkConfiguration: { networkMode: 'PUBLIC' },
      executionRoleArn: this.executionRole.roleArn,
    });
    this.codeInterpreter.node.addDependency(this.executionRole);

    this.codeInterpreterArn = this.codeInterpreter.attrCodeInterpreterArn;
    this.codeInterpreterId = this.codeInterpreter.attrCodeInterpreterId;

    // ── SSM publications ──
  }
}
