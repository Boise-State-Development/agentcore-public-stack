import * as cdk from 'aws-cdk-lib';
import * as bedrock from 'aws-cdk-lib/aws-bedrockagentcore';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ssm from 'aws-cdk-lib/aws-ssm';
import { Construct } from 'constructs';

import { AppConfig, getResourceName } from '../../config';

export interface AgentCoreMemoryConstructProps {
  config: AppConfig;
}

/**
 * AgentCoreMemoryConstruct — Bedrock AgentCore Memory + execution
 * role + observability (vended log delivery for both APPLICATION_LOGS
 * and TRACES).
 *
 * Hoisted from a sibling construct's InferenceAgentCoreConstruct as part of
 * + strategies), takes 5-15 minutes to create, and is a once-ever
 * resource. Belongs in the rarely-deployed Platform layer alongside
 * the other data-tier resources.
 *
 * SSM publications (consumed by InferenceAgentCoreConstruct in
 * PlatformStack as typed refs):
 *   /{prefix}/inference-api/memory-arn
 *   /{prefix}/inference-api/memory-id
 *
 * Public properties exposed for typed cross-stack consumption:
 *   memory: bedrock.CfnMemory
 *   memoryArn: string  (= memory.attrMemoryArn)
 *   memoryId: string   (= memory.attrMemoryId)
 */
export class AgentCoreMemoryConstruct extends Construct {
  public readonly memory: bedrock.CfnMemory;
  public readonly memoryArn: string;
  public readonly memoryId: string;
  public readonly executionRole: iam.Role;

  constructor(scope: Construct, id: string, props: AgentCoreMemoryConstructProps) {
    super(scope, id);

    const { config } = props;

    // ── IAM execution role ──
    // IMPORTANT: keep an explicit, stable roleName. This role's ARN is
    // consumed by the Memory `memoryExecutionRoleArn` — renaming the role
    // (auto-gen) replaces it, risking Memory replacement and loss of
    // stored conversation history. See browser-construct.ts.
    this.executionRole = new iam.Role(this, 'MemoryExecutionRole', {
      roleName: getResourceName(config, 'agentcore-memory-role'),
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com'),
      description: 'Execution role for AgentCore Memory',
    });
    this.executionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['bedrock:InvokeModel'],
      resources: [`arn:aws:bedrock:*::foundation-model/*`],
    }));
    // Additional model access for memory processing (Claude + Nova)
    this.executionRole.addToPolicy(new iam.PolicyStatement({
      effect: iam.Effect.ALLOW,
      actions: ['bedrock:InvokeModel'],
      resources: [
        `arn:aws:bedrock:${config.awsRegion}::foundation-model/anthropic.claude-*`,
        `arn:aws:bedrock:${config.awsRegion}::foundation-model/amazon.nova-*`,
      ],
    }));

    // ── Memory resource ──
    this.memory = new bedrock.CfnMemory(this, 'AgentCoreMemory', {
      name: getResourceName(config, 'agentcore_memory').replace(/-/g, '_'),
      eventExpiryDuration: 90, // days; max 365, min 7
      memoryExecutionRoleArn: this.executionRole.roleArn,
      description:
        'AgentCore Memory for maintaining conversation context, user preferences, and semantic facts',
      memoryStrategies: [
        {
          semanticMemoryStrategy: {
            name: 'SemanticFactExtraction',
            description: 'Extracts and stores semantic facts from conversations',
          },
        },
        {
          summaryMemoryStrategy: {
            name: 'ConversationSummary',
            description: 'Generates and stores conversation summaries',
          },
        },
        {
          userPreferenceMemoryStrategy: {
            name: 'UserPreferenceExtraction',
            description: 'Identifies and stores user preferences',
          },
        },
      ],
    });
    this.memory.node.addDependency(this.executionRole);

    this.memoryArn = this.memory.attrMemoryArn;
    this.memoryId = this.memory.attrMemoryId;

    // ── Observability: vended log delivery ──
    // Memory APPLICATION_LOGS → CloudWatch Logs.
    // Log group name uses the AWS-convention vendedlogs path
    // (`/aws/vendedlogs/bedrock-agentcore/memory/...`) because
    // CloudWatch Logs vended-logs delivery requires log groups under
    // that prefix. This is a service constraint, not a naming choice.
    const memoryLogsLogGroup = new logs.LogGroup(this, 'MemoryLogsLogGroup', {
      logGroupName: `/aws/vendedlogs/bedrock-agentcore/memory/${config.projectPrefix}`,
      retention: logs.RetentionDays.ONE_MONTH,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const memoryLogsSource = new logs.CfnDeliverySource(this, 'MemoryLogsSource', {
      name: `${config.projectPrefix}-memory-logs`,
      logType: 'APPLICATION_LOGS',
      resourceArn: this.memoryArn,
    });
    memoryLogsSource.node.addDependency(this.memory);
    const memoryLogsDestination = new logs.CfnDeliveryDestination(this, 'MemoryLogsDestination', {
      name: `${config.projectPrefix}-memory-logs-dest`,
      deliveryDestinationType: 'CWL',
      destinationResourceArn: memoryLogsLogGroup.logGroupArn,
    });
    const memoryLogsDelivery = new logs.CfnDelivery(this, 'MemoryLogsDelivery', {
      deliverySourceName: memoryLogsSource.name,
      deliveryDestinationArn: memoryLogsDestination.attrArn,
    });
    memoryLogsDelivery.node.addDependency(memoryLogsSource);
    memoryLogsDelivery.node.addDependency(memoryLogsDestination);

    // Memory TRACES → X-Ray.
    const memoryTracesSource = new logs.CfnDeliverySource(this, 'MemoryTracesSource', {
      name: `${config.projectPrefix}-memory-traces`,
      logType: 'TRACES',
      resourceArn: this.memoryArn,
    });
    memoryTracesSource.node.addDependency(this.memory);
    const memoryTracesDestination = new logs.CfnDeliveryDestination(this, 'MemoryTracesDestination', {
      name: `${config.projectPrefix}-memory-traces-dest`,
      deliveryDestinationType: 'XRAY',
    });
    const memoryTracesDelivery = new logs.CfnDelivery(this, 'MemoryTracesDelivery', {
      deliverySourceName: memoryTracesSource.name,
      deliveryDestinationArn: memoryTracesDestination.attrArn,
    });
    memoryTracesDelivery.node.addDependency(memoryTracesSource);
    memoryTracesDelivery.node.addDependency(memoryTracesDestination);

    // ── SSM publications (consumed by restore tooling, runtime container env) ──
    new ssm.StringParameter(this, 'MemoryIdParameter', {
      parameterName: `/${config.projectPrefix}/inference-api/memory-id`,
      stringValue: this.memoryId,
      description: 'AgentCore Memory ID',
      tier: ssm.ParameterTier.STANDARD,
    });
  }
}
