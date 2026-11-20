import * as bedrock from 'aws-cdk-lib/aws-bedrockagentcore';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

/**
 * SSM parameter (under `/<prefix>/`) holding the skill-script interpreter's id.
 * The Runtime derives the name from PROJECT_PREFIX, so it costs no environment
 * bytes. Must match `SKILL_SCRIPT_INTERPRETER_SSM_SUFFIX` in
 * backend/src/agents/main_agent/skills/skill_scripts.py.
 */
export const SKILL_SCRIPT_INTERPRETER_SSM_SUFFIX = 'agentcore/skill-script-interpreter-id';

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
 * Two interpreters share the logs-only execution role:
 *   - `codeInterpreter` (PUBLIC network) runs model-written code.
 *   - `skillScriptInterpreter` (SANDBOX network: no public internet) runs
 *     skill `scripts/*.py` files, which an author other than the invoking
 *     user may have written. Its id is published to SSM rather than the
 *     Runtime's environment (2,560-byte V2 budget) and read on first use.
 *
 * Public properties:
 *   codeInterpreter: bedrock.CfnCodeInterpreterCustom
 *   codeInterpreterArn: string
 *   codeInterpreterId: string
 *   skillScriptInterpreter: bedrock.CfnCodeInterpreterCustom
 *   skillScriptInterpreterArn: string
 *   skillScriptInterpreterIdParameter: ssm.StringParameter
 */
export class AgentCoreCodeInterpreterConstruct extends Construct {
  public readonly codeInterpreter: bedrock.CfnCodeInterpreterCustom;
  public readonly codeInterpreterArn: string;
  public readonly codeInterpreterId: string;
  public readonly executionRole: iam.Role;
  public readonly skillScriptInterpreter: bedrock.CfnCodeInterpreterCustom;
  public readonly skillScriptInterpreterArn: string;
  public readonly skillScriptInterpreterIdParameter: ssm.StringParameter;

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

    // ── Skill-script Code Interpreter (no public internet) ──
    // A separate resource, not a mode switch on the one above: model-written
    // code keeps PUBLIC, and changing `networkConfiguration` on the existing
    // interpreter would replace it. Same logs-only role (the log-group grant
    // above covers it); explicit, stable name for the same create-only reason.
    this.skillScriptInterpreter = new bedrock.CfnCodeInterpreterCustom(this, 'SkillScriptInterpreterCustom', {
      name: getResourceName(config, 'skill_script_interpreter').replace(/-/g, '_'),
      description: 'Code Interpreter for skill scripts, with no public network access',
      networkConfiguration: { networkMode: 'SANDBOX' },
      executionRoleArn: this.executionRole.roleArn,
    });
    this.skillScriptInterpreter.node.addDependency(this.executionRole);
    this.skillScriptInterpreterArn = this.skillScriptInterpreter.attrCodeInterpreterArn;

    // ── SSM publications ──
    // The skill-script interpreter's id is service-generated, so it cannot be
    // derived like a `{prefix}-{suffix}` name; the Runtime reads this parameter
    // on the first skill-script run instead of spending V2 environment bytes.
    this.skillScriptInterpreterIdParameter = new ssm.StringParameter(this, 'SkillScriptInterpreterIdParameter', {
      parameterName: `/${config.projectPrefix}/${SKILL_SCRIPT_INTERPRETER_SSM_SUFFIX}`,
      stringValue: this.skillScriptInterpreter.attrCodeInterpreterId,
      description: 'AgentCore Code Interpreter id for skill scripts (no public network)',
      tier: ssm.ParameterTier.STANDARD,
    });
  }
}
